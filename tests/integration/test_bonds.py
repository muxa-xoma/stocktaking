"""Tests for the bonds module: CRUD service + EventBus publishing.

The schema is created with ``alembic upgrade head`` against a temporary
SQLite file (never ``Base.metadata.create_all``), mirroring ``test_db.py``.
The ``AsyncQueueEventBus`` is started in the fixture setup and stopped in
teardown; handlers record every published message per topic.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from bond_accounting.bonds import (
    BondCreate,
    BondDeletionBlockedError,
    BondIsinDuplicateError,
    BondService,
    BondUpdate,
)
from bond_accounting.config.settings import DatabaseConfig, EventBusConfig
from bond_accounting.db import (
    Bond,
    Transaction,
    User,
    create_engine_from_settings,
    create_session_factory,
)
from bond_accounting.db.models import Broker, BrokerAccount
from bond_accounting.event_bus import AsyncQueueEventBus, Message, Topic

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BOND_TOPICS = (Topic.BOND_CREATED, Topic.BOND_UPDATED, Topic.BOND_DELETED)


def _run_alembic(db_path: Path, *args: str) -> None:
    """Run ``uv run alembic <args>`` against a temp sqlite file, asserting success."""
    env = {
        **os.environ,
        "BOND_DATABASE__SQLITE_PATH": str(db_path),
        "BOND_AUTH__JWT_SECRET": "test-secret",
    }
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def migrated_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Path to a temp sqlite database with all migrations applied."""
    path = tmp_path_factory.mktemp("bonds_db") / "bonds.db"
    _run_alembic(path, "upgrade", "head")
    return path


@pytest.fixture
async def bonds_env(
    migrated_db_path: Path,
) -> AsyncIterator[
    tuple[
        BondService,
        async_sessionmaker[AsyncSession],
        dict[str, list[Message]],
        int,
    ]
]:
    """A ``(service, session_factory, events, owner_id)`` tuple with a running bus.

    ``events`` maps each ``bond.*`` topic to the list of messages delivered
    by the bus to the recording subscriber. ``owner_id`` is the id of the
    user seeded as the bond owner for this test (unique per test).
    """
    engine = create_engine_from_settings(
        DatabaseConfig(driver="sqlite", sqlite_path=str(migrated_db_path))
    )
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        owner = User(username=f"bond-owner-{uuid.uuid4().hex[:12]}", password_hash="x")
        session.add(owner)
        await session.commit()
        owner_id = owner.id

    bus = AsyncQueueEventBus(EventBusConfig())

    events: dict[str, list[Message]] = {topic: [] for topic in BOND_TOPICS}

    async def record(message: Message) -> None:
        events[message.topic].append(message)

    for topic in BOND_TOPICS:
        bus.subscribe(topic, record)

    await bus.start()
    try:
        yield BondService(session_factory, bus), session_factory, events, owner_id
    finally:
        await bus.stop()
        await engine.dispose()


async def _drain(events: dict[str, list[Message]], topic: str, count: int) -> list[Message]:
    """Wait until ``count`` messages were delivered on ``topic``.

    The bus delivers asynchronously via dispatcher tasks, so tests must yield
    control to the event loop until the queued messages are handled.
    """
    async with asyncio.timeout(2.0):
        while len(events[topic]) < count:
            await asyncio.sleep(0)
    return events[topic]


async def _seed_broker_account(
    session_factory: async_sessionmaker[AsyncSession], user_id: int
) -> int:
    """Insert a broker and an account for ``user_id``; return the account id.

    ``Transaction.broker_account_id`` is NOT NULL, so tests seeding
    transactions directly need an account first. The module-scoped DB is
    shared across the module's tests, so names are uuid-suffixed.
    """
    async with session_factory() as session:
        broker = Broker(name=f"bond-owner-broker-{uuid.uuid4().hex[:12]}", commission=0.3)
        session.add(broker)
        await session.flush()
        account = BrokerAccount(
            user_id=user_id, broker_id=broker.id, name="Основной", account_type="STANDARD"
        )
        session.add(account)
        await session.commit()
        return account.id


def _create_data(isin: str, **overrides: object) -> BondCreate:
    """Valid :class:`BondCreate` with per-test unique ISIN and optional overrides."""
    fields: dict[str, object] = {
        "isin": isin,
        "name": "Test bond",
        "nominal": 1000,
        "coupon_rate": 7.5,
        "coupon_period_days": 365,
        "maturity_date": datetime.date(2030, 1, 1),
        "issuer": "Test issuer",
    }
    fields.update(overrides)
    # Intentional: kwargs are built dynamically from a dict[str, object],
    # so mypy cannot verify individual argument types against BondCreate.
    return BondCreate(**fields)  # type: ignore[arg-type]


async def test_create_returns_dto_and_persists_row(bonds_env) -> None:
    """``create`` returns a DTO with the given fields; the row is in the DB."""
    service, session_factory, _, owner_id = bonds_env

    dto = await service.create(_create_data("RU000A0JV4L2"), user_id=owner_id)

    assert dto.id is not None
    assert dto.owner_id == owner_id
    assert dto.isin == "RU000A0JV4L2"
    assert dto.name == "Test bond"
    assert dto.nominal == 1000
    assert dto.coupon_rate == 7.5
    assert dto.coupon_period_days == 365
    assert dto.maturity_date == datetime.date(2030, 1, 1)
    assert dto.issuer == "Test issuer"

    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(Bond).where(Bond.isin == "RU000A0JV4L2")
            )
        ).scalar_one()
    assert count == 1


async def test_create_duplicate_isin_raises(bonds_env) -> None:
    """Creating a bond with an existing ISIN raises ``BondIsinDuplicateError``."""
    service, _, _, owner_id = bonds_env
    data = _create_data("RU000A0JX0K8")

    await service.create(data, user_id=owner_id)
    with pytest.raises(BondIsinDuplicateError):
        await service.create(data, user_id=owner_id)


@pytest.mark.parametrize("isin", ["RU000A0JV4L", "ru000a0jv4l2", "RU000A0JV4L!", ""])
async def test_create_invalid_isin_raises(bonds_env, isin: str) -> None:
    """An invalid ISIN (wrong length / lowercase / bad characters) fails validation."""
    with pytest.raises(ValidationError):
        _create_data(isin)


async def test_get_by_id(bonds_env) -> None:
    """``get`` fetches by primary key and returns the correct DTO."""
    service, _, _, owner_id = bonds_env
    created = await service.create(_create_data("RU000A0JWXQ9"), user_id=owner_id)

    fetched = await service.get(created.id)

    assert fetched == created


async def test_get_by_isin(bonds_env) -> None:
    """``get_by_isin`` returns the correct DTO."""
    service, _, _, owner_id = bonds_env
    created = await service.create(_create_data("RU000A0JV8A3"), user_id=owner_id)

    fetched = await service.get_by_isin("RU000A0JV8A3")

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == created.name


async def test_get_nonexistent_returns_none(bonds_env) -> None:
    """``get`` and ``get_by_isin`` return ``None`` for unknown keys."""
    service, _, _, _ = bonds_env

    assert await service.get(10**9) is None
    assert await service.get_by_isin("ZZ0000000000") is None


async def test_list_all_ordered_by_id(bonds_env) -> None:
    """``list_all`` returns all bonds ordered by id."""
    service, _, _, owner_id = bonds_env
    isins = ["RU000A0JWL09", "RU000A0JWL10", "RU000A0JWL11"]
    created = [
        await service.create(_create_data(isin, name=f"Bond {index}"), user_id=owner_id)
        for index, isin in enumerate(isins)
    ]

    listed = await service.list_all()

    ids = [bond.id for bond in listed]
    assert ids == sorted(ids)
    created_ids = {bond.id for bond in created}
    assert created_ids <= set(ids)


async def test_update_changes_fields_and_publishes_event(bonds_env) -> None:
    """``update`` changes fields; ``bond.updated`` fans out per holder.

    The payload is the updated bond plus the holder's ``user_id``; bonds
    without holders publish nothing (covered by the lifecycle test).
    """
    service, session_factory, events, owner_id = bonds_env
    created = await service.create(_create_data("RU000A0JW2T9"), user_id=owner_id)

    # Seed the owner as the (only) holder so the update fans out to them.
    account_id = await _seed_broker_account(session_factory, owner_id)
    async with session_factory() as session:
        session.add(
            Transaction(
                user_id=owner_id,
                bond_id=created.id,
                broker_account_id=account_id,
                type="BUY",
                quantity=1,
                price=1000.0,
                date=datetime.date(2026, 1, 10),
            )
        )
        await session.commit()

    updated = await service.update(
        created.id, BondUpdate(name="Renamed", coupon_rate=9.25), user_id=owner_id
    )

    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.coupon_rate == 9.25
    assert updated.isin == created.isin
    assert updated.nominal == created.nominal  # untouched fields keep their values

    (message,) = await _drain(events, Topic.BOND_UPDATED, 1)
    assert message.sender == "bonds"
    assert message.payload["name"] == "Renamed"
    assert message.payload["coupon_rate"] == 9.25
    assert message.payload["id"] == created.id
    assert message.payload["user_id"] == owner_id


async def test_update_nonexistent_returns_none(bonds_env) -> None:
    """``update`` of a nonexistent id returns ``None`` without publishing."""
    service, _, events, owner_id = bonds_env

    assert await service.update(10**9, BondUpdate(name="Ghost"), user_id=owner_id) is None

    await asyncio.sleep(0)
    assert events[Topic.BOND_UPDATED] == []


async def test_delete_without_holders_publishes_nothing(bonds_env) -> None:
    """``delete`` removes the row; without holders no ``bond.deleted`` is published.

    Deletion is blocked while transactions exist, so a deleted bond never
    has holders and the per-holder fan-out has nobody to notify.
    """
    service, session_factory, events, owner_id = bonds_env
    created = await service.create(_create_data("RU000A0JY5B6"), user_id=owner_id)

    assert await service.delete(created.id, user_id=owner_id) is True

    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(Bond).where(Bond.id == created.id)
            )
        ).scalar_one()
    assert count == 0

    await asyncio.sleep(0)
    assert events[Topic.BOND_DELETED] == []


async def test_delete_nonexistent_returns_false(bonds_env) -> None:
    """``delete`` of a nonexistent id returns ``False`` without publishing."""
    service, _, events, owner_id = bonds_env

    assert await service.delete(10**9, user_id=owner_id) is False

    await asyncio.sleep(0)
    assert events[Topic.BOND_DELETED] == []


async def test_delete_with_transactions_raises_blocked(bonds_env) -> None:
    """``delete`` of a bond that has transactions raises ``BondDeletionBlockedError``.

    The bond must survive and no ``bond.deleted`` event may be published.
    """
    service, session_factory, events, owner_id = bonds_env
    created = await service.create(_create_data("RU000A0JZ9D4"), user_id=owner_id)

    # Seed a user and one transaction referencing the bond.
    async with session_factory() as session:
        user = User(
            username=f"svc-delete-user-{uuid.uuid4().hex[:12]}", password_hash="$2b$12$placeholder"
        )
        session.add(user)
        await session.flush()
        broker = Broker(name=f"svc-delete-broker-{uuid.uuid4().hex[:12]}", commission=0.3)
        session.add(broker)
        await session.flush()
        account = BrokerAccount(
            user_id=user.id, broker_id=broker.id, name="Основной", account_type="STANDARD"
        )
        session.add(account)
        await session.flush()
        session.add(
            Transaction(
                user_id=user.id,
                bond_id=created.id,
                broker_account_id=account.id,
                type="BUY",
                quantity=1,
                price=1000.0,
                date=datetime.date(2026, 1, 10),
            )
        )
        await session.commit()

    with pytest.raises(BondDeletionBlockedError):
        await service.delete(created.id, user_id=owner_id)

    # The bond survives the blocked deletion.
    assert await service.get(created.id) is not None

    await asyncio.sleep(0)
    assert events[Topic.BOND_DELETED] == []


async def test_event_payloads_for_full_lifecycle(bonds_env) -> None:
    """Subscribers on the ``bond.*`` topics see the expected payloads.

    ``bond.updated`` fans out per holder: the payload is the updated bond
    dump plus the holder's ``user_id``. ``bond.deleted`` is not reachable
    through the service while deletion is blocked for bonds with
    transactions (see ``test_delete_without_holders_publishes_nothing``).
    """
    service, session_factory, events, owner_id = bonds_env
    created = await service.create(_create_data("RU000A0JY7C1"), user_id=owner_id)

    # Seed the owner as the (only) holder so the update fans out to them.
    account_id = await _seed_broker_account(session_factory, owner_id)
    async with session_factory() as session:
        session.add(
            Transaction(
                user_id=owner_id,
                bond_id=created.id,
                broker_account_id=account_id,
                type="BUY",
                quantity=1,
                price=1000.0,
                date=datetime.date(2026, 1, 10),
            )
        )
        await session.commit()

    updated = await service.update(created.id, BondUpdate(coupon_rate=11.0), user_id=owner_id)

    assert updated is not None

    (created_message,) = await _drain(events, Topic.BOND_CREATED, 1)
    (updated_message,) = await _drain(events, Topic.BOND_UPDATED, 1)

    assert created_message.payload == created.model_dump()
    assert updated_message.payload == updated.model_dump() | {"user_id": owner_id}
    assert updated_message.payload["coupon_rate"] == 11.0

    for message in (created_message, updated_message):
        assert message.sender == "bonds"
