"""Integration tests for the brokers module: CRUD service, guards, events.

Mirrors the fixture pattern of ``tests/integration/test_portfolio.py``: a
migrated temporary SQLite database (via alembic), a started
``AsyncQueueEventBus`` and the real ``BrokerService``.

Commission semantics: ``commission`` / ``min_commission`` are stored as raw
percent (``0.3`` means 0.3%); no ``/100`` conversions anywhere (fix-loop 01b).
"""

from __future__ import annotations

import asyncio
import datetime
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from bond_accounting.brokers import (
    BrokerAccountCreate,
    BrokerAccountHasTransactionsError,
    BrokerAccountNotFoundError,
    BrokerAccountUpdate,
    BrokerCreate,
    BrokerError,
    BrokerHasAccountsError,
    BrokerNotFoundError,
    BrokerService,
    BrokerUpdate,
)
from bond_accounting.config.settings import DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.db.models import Bond, Broker, Transaction, User
from bond_accounting.event_bus import AsyncQueueEventBus, Message, Topic

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Project root (where ``alembic.ini`` lives).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: How long to wait for async event delivery before failing a test.
_EVENT_TIMEOUT = 2.0


async def _wait_until(predicate: Callable[[], bool], timeout: float = _EVENT_TIMEOUT) -> None:
    """Await until ``predicate()`` is true, failing after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Condition not met within {timeout}s (async event not delivered?)")


def _collector(events: list[Message]) -> Callable[[Message], Awaitable[None]]:
    """Build an event handler that appends every received message to ``events``."""

    async def handler(message: Message) -> None:
        events.append(message)

    return handler


# --------------------------------------------------------------------- #
# fixtures


@pytest.fixture
def migrated_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create an empty migrated SQLite DB via alembic; return its file path."""
    db_path = tmp_path / "brokers_test.db"
    monkeypatch.setenv("BOND_DATABASE__SQLITE_PATH", str(db_path))
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", "test-secret")
    alembic_cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")
    return str(db_path)


@pytest.fixture
async def session_factory(migrated_db_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Async session factory bound to the migrated temp database."""
    engine = create_engine_from_settings(DatabaseConfig(sqlite_path=migrated_db_url))
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
async def event_bus() -> AsyncGenerator[AsyncQueueEventBus]:
    """A started event bus, stopped after the test."""
    bus = AsyncQueueEventBus(EventBusConfig(max_queue_size=100))
    await bus.start()
    yield bus
    await bus.stop()


@pytest.fixture
def broker_service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> BrokerService:
    """The broker service under test."""
    return BrokerService(session_factory, event_bus)


async def _seed_user(session_factory: async_sessionmaker[AsyncSession], username: str) -> int:
    """Insert a user and return its id."""
    async with session_factory() as session:
        user = User(username=username, password_hash="not-a-real-hash")
        session.add(user)
        await session.commit()
        return user.id


@pytest.fixture
async def user_id(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """The test user owning the fixture account."""
    return await _seed_user(session_factory, "alice")


@pytest.fixture
async def broker(broker_service: BrokerService) -> object:
    """A test broker with raw-percent commissions (0.3% / 5.0% minimum)."""
    return await broker_service.create_broker(
        BrokerCreate(
            name="Тестовый брокер", commission=0.3, min_commission=5.0, description="для тестов"
        )
    )


@pytest.fixture
async def broker_account(broker_service: BrokerService, broker, user_id: int):
    """A test broker account owned by the test user."""
    return await broker_service.create_account(
        BrokerAccountCreate(
            broker_id=broker.id,
            name="Основной",
            account_type="STANDARD",
        ),
        user_id=user_id,
    )


# --------------------------------------------------------------------- #
# broker CRUD


async def test_create_broker(broker_service: BrokerService) -> None:
    dto = await broker_service.create_broker(
        BrokerCreate(name="ВТБ", commission=0.3, min_commission=5.0, description="тест")
    )

    assert dto.id is not None
    assert dto.name == "ВТБ"
    # Commission is stored as raw percent: 0.3 == 0.3%, no /100 anywhere.
    assert dto.commission == pytest.approx(0.3)
    assert dto.min_commission == pytest.approx(5.0)
    assert dto.description == "тест"
    assert dto.created_at is not None


async def test_create_broker_duplicate_name(broker_service: BrokerService) -> None:
    data = BrokerCreate(name="Сбер", commission=0.3)

    await broker_service.create_broker(data)
    with pytest.raises(BrokerError):
        await broker_service.create_broker(BrokerCreate(name="Сбер", commission=1.0))


async def test_update_broker(broker_service: BrokerService, broker) -> None:
    updated = await broker_service.update_broker(broker.id, BrokerUpdate(commission=5.0))

    assert updated is not None
    assert updated.commission == pytest.approx(5.0)
    # Untouched fields keep their values.
    assert updated.name == broker.name
    assert updated.min_commission == broker.min_commission
    assert updated.description == broker.description


async def test_update_broker_rename_collision_raises(broker_service: BrokerService) -> None:
    await broker_service.create_broker(BrokerCreate(name="Первый"))
    second = await broker_service.create_broker(BrokerCreate(name="Второй"))

    with pytest.raises(BrokerError):
        await broker_service.update_broker(second.id, BrokerUpdate(name="Первый"))


async def test_update_broker_nonexistent_returns_none(broker_service: BrokerService) -> None:
    assert await broker_service.update_broker(999_999, BrokerUpdate(commission=1.0)) is None


async def test_delete_broker_success(broker_service: BrokerService, broker) -> None:
    assert await broker_service.delete_broker(broker.id) is True
    assert await broker_service.get_broker(broker.id) is None


async def test_delete_broker_nonexistent_returns_false(broker_service: BrokerService) -> None:
    assert await broker_service.delete_broker(999_999) is False


async def test_delete_broker_blocked_by_accounts(
    broker_service: BrokerService, broker_account
) -> None:
    with pytest.raises(BrokerHasAccountsError):
        await broker_service.delete_broker(broker_account.broker_id)

    # The broker survives the blocked deletion.
    assert await broker_service.get_broker(broker_account.broker_id) is not None


async def test_list_all_brokers(broker_service: BrokerService) -> None:
    first = await broker_service.create_broker(BrokerCreate(name="Первый"))
    second = await broker_service.create_broker(BrokerCreate(name="Второй"))

    listed = await broker_service.list_all_brokers()

    ids = [dto.id for dto in listed]
    assert {first.id, second.id} <= set(ids)
    assert ids == sorted(ids)


# --------------------------------------------------------------------- #
# broker-account CRUD


async def test_create_account(broker_service: BrokerService, broker, user_id: int) -> None:
    dto = await broker_service.create_account(
        BrokerAccountCreate(
            broker_id=broker.id,
            name="ИИС",
            account_number="AB123",
            account_type="IIS",
            opened_at=datetime.date(2026, 1, 1),
        ),
        user_id=user_id,
    )

    assert dto.id is not None
    assert dto.user_id == user_id
    assert dto.broker_id == broker.id
    assert dto.broker_name == broker.name
    assert dto.name == "ИИС"
    assert dto.account_number == "AB123"
    assert dto.account_type == "IIS"
    assert dto.opened_at == datetime.date(2026, 1, 1)
    assert dto.closed_at is None
    assert dto.created_at is not None


async def test_create_account_unknown_broker(broker_service: BrokerService, user_id: int) -> None:
    with pytest.raises(BrokerNotFoundError):
        await broker_service.create_account(
            BrokerAccountCreate(broker_id=999_999, name="Основной", account_type="STANDARD"),
            user_id=user_id,
        )


async def test_update_account_by_owner(broker_service: BrokerService, broker_account) -> None:
    updated = await broker_service.update_account(
        broker_account.id, BrokerAccountUpdate(name="Переименован"), user_id=broker_account.user_id
    )

    assert updated is not None
    assert updated.name == "Переименован"
    # Untouched fields keep their values (broker_name still resolved).
    assert updated.broker_name == broker_account.broker_name
    assert updated.account_type == broker_account.account_type


async def test_update_account_by_stranger_rejected(
    broker_service: BrokerService, session_factory: async_sessionmaker[AsyncSession], broker_account
) -> None:
    stranger_id = await _seed_user(session_factory, "mallory")

    with pytest.raises(BrokerAccountNotFoundError):
        await broker_service.update_account(
            broker_account.id, BrokerAccountUpdate(name="взлом"), user_id=stranger_id
        )

    # The account is unchanged.
    assert (
        await broker_service.get_account(broker_account.id, user_id=broker_account.user_id)
        is not None
    )


async def test_delete_account_success(broker_service: BrokerService, broker_account) -> None:
    assert (
        await broker_service.delete_account(broker_account.id, user_id=broker_account.user_id)
        is True
    )
    assert (
        await broker_service.get_account(broker_account.id, user_id=broker_account.user_id) is None
    )


async def test_delete_account_by_stranger_rejected(
    broker_service: BrokerService, session_factory: async_sessionmaker[AsyncSession], broker_account
) -> None:
    stranger_id = await _seed_user(session_factory, "mallory")

    with pytest.raises(BrokerAccountNotFoundError):
        await broker_service.delete_account(broker_account.id, user_id=stranger_id)

    assert (
        await broker_service.get_account(broker_account.id, user_id=broker_account.user_id)
        is not None
    )


async def test_delete_account_blocked_by_transactions(
    broker_service: BrokerService,
    session_factory: async_sessionmaker[AsyncSession],
    broker_account,
) -> None:
    """An account with at least one transaction cannot be deleted."""
    async with session_factory() as session:
        bond = Bond(
            isin="RU000A0JX0J2",
            name="OFLZ 2030",
            nominal=1000,
            coupon_rate=7.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
            owner_id=broker_account.user_id,
        )
        session.add(bond)
        await session.flush()
        session.add(
            Transaction(
                user_id=broker_account.user_id,
                bond_id=bond.id,
                broker_account_id=broker_account.id,
                type="BUY",
                quantity=1,
                price=1000.0,
                date=datetime.date(2026, 1, 10),
            )
        )
        await session.commit()

    with pytest.raises(BrokerAccountHasTransactionsError):
        await broker_service.delete_account(broker_account.id, user_id=broker_account.user_id)

    assert (
        await broker_service.get_account(broker_account.id, user_id=broker_account.user_id)
        is not None
    )


async def test_list_accounts_for_user(
    broker_service: BrokerService,
    session_factory: async_sessionmaker[AsyncSession],
    broker,
    user_id: int,
) -> None:
    other_user = await _seed_user(session_factory, "bob")
    mine = await broker_service.create_account(
        BrokerAccountCreate(broker_id=broker.id, name="Мой счёт", account_type="STANDARD"),
        user_id=user_id,
    )
    await broker_service.create_account(
        BrokerAccountCreate(broker_id=broker.id, name="Чужой счёт", account_type="IIS"),
        user_id=other_user,
    )

    listed = await broker_service.list_accounts_for_user(user_id)

    assert [dto.id for dto in listed] == [mine.id]
    assert listed[0].name == "Мой счёт"


async def test_list_accounts_for_broker(
    broker_service: BrokerService,
    session_factory: async_sessionmaker[AsyncSession],
    broker,
    user_id: int,
) -> None:
    other_broker = await broker_service.create_broker(BrokerCreate(name="Другой брокер"))
    here = await broker_service.create_account(
        BrokerAccountCreate(broker_id=broker.id, name="Счёт здесь", account_type="STANDARD"),
        user_id=user_id,
    )
    await broker_service.create_account(
        BrokerAccountCreate(broker_id=other_broker.id, name="Счёт там", account_type="STANDARD"),
        user_id=user_id,
    )

    listed = await broker_service.list_accounts_for_broker(broker.id)

    assert [dto.id for dto in listed] == [here.id]
    assert listed[0].broker_name == broker.name


# --------------------------------------------------------------------- #
# event publishing


async def test_broker_events_published(
    broker_service: BrokerService, event_bus: AsyncQueueEventBus
) -> None:
    created: list[Message] = []
    updated: list[Message] = []
    deleted: list[Message] = []
    event_bus.subscribe(Topic.BROKER_CREATED, _collector(created))
    event_bus.subscribe(Topic.BROKER_UPDATED, _collector(updated))
    event_bus.subscribe(Topic.BROKER_DELETED, _collector(deleted))

    dto = await broker_service.create_broker(BrokerCreate(name="Событийный", commission=0.3))
    await _wait_until(lambda: len(created) >= 1)

    assert created[0].topic == "broker.created"
    assert created[0].sender == "brokers"
    assert created[0].payload["id"] == dto.id
    assert created[0].payload["name"] == "Событийный"
    assert created[0].payload["commission"] == pytest.approx(0.3)

    dto = await broker_service.update_broker(dto.id, BrokerUpdate(commission=5.0))
    assert dto is not None
    await _wait_until(lambda: len(updated) >= 1)

    assert updated[0].topic == "broker.updated"
    assert updated[0].payload["commission"] == pytest.approx(5.0)

    assert await broker_service.delete_broker(dto.id) is True
    await _wait_until(lambda: len(deleted) >= 1)

    assert deleted[0].topic == "broker.deleted"
    assert deleted[0].payload == {"broker_id": dto.id}


async def test_account_events_published(
    broker_service: BrokerService,
    event_bus: AsyncQueueEventBus,
    broker,
    user_id: int,
) -> None:
    created: list[Message] = []
    updated: list[Message] = []
    deleted: list[Message] = []
    event_bus.subscribe(Topic.BROKER_ACCOUNT_CREATED, _collector(created))
    event_bus.subscribe(Topic.BROKER_ACCOUNT_UPDATED, _collector(updated))
    event_bus.subscribe(Topic.BROKER_ACCOUNT_DELETED, _collector(deleted))

    account = await broker_service.create_account(
        BrokerAccountCreate(broker_id=broker.id, name="Событийный счёт", account_type="STANDARD"),
        user_id=user_id,
    )
    await _wait_until(lambda: len(created) >= 1)

    assert created[0].topic == "broker_account.created"
    assert created[0].sender == "brokers"
    assert created[0].payload["id"] == account.id
    assert created[0].payload["user_id"] == user_id
    assert created[0].payload["broker_id"] == broker.id
    assert created[0].payload["broker_name"] == broker.name

    account = await broker_service.update_account(
        account.id, BrokerAccountUpdate(name="Переименован"), user_id=user_id
    )
    assert account is not None
    await _wait_until(lambda: len(updated) >= 1)

    assert updated[0].topic == "broker_account.updated"
    assert updated[0].payload["id"] == account.id
    assert updated[0].payload["name"] == "Переименован"

    assert await broker_service.delete_account(account.id, user_id=user_id) is True
    await _wait_until(lambda: len(deleted) >= 1)

    assert deleted[0].topic == "broker_account.deleted"
    assert deleted[0].payload == {"broker_account_id": account.id, "user_id": user_id}


# --------------------------------------------------------------------- #
# guards leave no residue in the DB


async def test_duplicate_broker_creation_leaves_no_residue(
    broker_service: BrokerService,
    session_factory: async_sessionmaker[AsyncSession],
    broker,
    user_id: int,
) -> None:
    """A rejected duplicate-name insert leaves the broker table unchanged."""
    with pytest.raises(BrokerError):
        await broker_service.create_broker(BrokerCreate(name=broker.name))

    async with session_factory() as session:
        brokers = (await session.scalars(select(Broker))).all()
        assert len(brokers) == 1
        assert brokers[0].name == broker.name
