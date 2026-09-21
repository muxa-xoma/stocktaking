"""Tests for the portfolio module: transactions, positions, event publishing."""

from __future__ import annotations

import asyncio
import datetime
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import ValidationError
from sqlalchemy import select

from bond_accounting.config.settings import DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.db.models import Bond, Transaction, User
from bond_accounting.event_bus import AsyncQueueEventBus, Topic
from bond_accounting.portfolio import (
    InsufficientPositionError,
    PortfolioService,
    TransactionCreate,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.event_bus import Message
    from bond_accounting.portfolio.dto import TransactionType

#: Project root (where ``alembic.ini`` lives).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: How long to wait for async event delivery before failing a test.
_EVENT_TIMEOUT = 2.0


def _txn(
    bond_id: int, type_: TransactionType, quantity: int, price: float, day: int
) -> TransactionCreate:
    """Build a TransactionCreate dated 2026-01-<day>."""
    return TransactionCreate(
        bond_id=bond_id,
        type=type_,
        quantity=quantity,
        price=price,
        date=datetime.date(2026, 1, day),
    )


async def _wait_until(predicate: Callable[[], bool], timeout: float = _EVENT_TIMEOUT) -> None:
    """Await until ``predicate()`` is true, failing after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Condition not met within {timeout}s (async event not delivered?)")


# --------------------------------------------------------------------- #
# fixtures


@pytest.fixture
def migrated_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create an empty migrated SQLite DB via alembic; return its file path.

    The URL is injected into alembic through the ``BOND_DATABASE__SQLITE_PATH``
    environment variable (``alembic/env.py`` builds the URL from
    ``load_settings()``). A JWT secret is set explicitly so the migration does
    not depend on a ``config.yaml`` in the working directory.
    """
    db_path = tmp_path / "portfolio_test.db"
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
def service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> PortfolioService:
    """The portfolio service under test."""
    return PortfolioService(session_factory, event_bus)


@pytest.fixture
async def user_and_bond_ids(session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """A test user and a test bond; returns ``(user_id, bond_id)``."""
    async with session_factory() as session:
        user = User(username="alice", password_hash="not-a-real-hash")
        session.add(user)
        await session.flush()
        bond = Bond(
            isin="RU000A0JX0J2",
            name="OFLZ 2030",
            nominal=1000,
            coupon_rate=7.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
            owner_id=user.id,
        )
        session.add(bond)
        await session.commit()
        return user.id, bond.id


async def _add_second_bond(session_factory: async_sessionmaker[AsyncSession], owner_id: int) -> int:
    """Insert a second bond and return its id (for filtering tests)."""
    async with session_factory() as session:
        bond = Bond(
            isin="RU000A0JX0K9",
            name="OFLZ 2031",
            nominal=1000,
            coupon_rate=6.5,
            coupon_frequency="SEMI_ANNUAL",
            maturity_date=datetime.date(2031, 1, 1),
            owner_id=owner_id,
        )
        session.add(bond)
        await session.commit()
        return bond.id


def _collector(events: list[Message]) -> Callable[[Message], Awaitable[None]]:
    """Build an event handler that appends every received message to ``events``."""

    async def handler(message: Message) -> None:
        events.append(message)

    return handler


# --------------------------------------------------------------------- #
# add_transaction


async def test_add_buy_creates_row_and_dto(
    service: PortfolioService,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids

    dto = await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))

    assert dto.user_id == user_id
    assert dto.bond_id == bond_id
    assert dto.type == "BUY"
    assert dto.quantity == 10
    assert dto.price == 1000.0
    assert dto.date == datetime.date(2026, 1, 10)
    assert dto.commission == 0.0
    assert dto.created_at is not None

    async with session_factory() as session:
        row = await session.get(Transaction, dto.id)
        assert row is not None
        assert row.quantity == 10
        assert row.type == "BUY"
        assert row.user_id == user_id
        assert row.bond_id == bond_id


async def test_sell_reduces_position(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 3, 1050.0, 15))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 7


async def test_mature_closes_position(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 3, 1050.0, 15))
    await service.add_transaction(user_id, _txn(bond_id, "MATURE", 7, 1000.0, 31))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 0


async def test_sell_more_than_held_raises(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids

    with pytest.raises(InsufficientPositionError):
        await service.add_transaction(user_id, _txn(bond_id, "SELL", 1, 1000.0, 10))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 0


async def test_mature_more_than_held_raises(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 3, 1000.0, 10))

    with pytest.raises(InsufficientPositionError):
        await service.add_transaction(user_id, _txn(bond_id, "MATURE", 5, 1000.0, 20))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 3


@pytest.mark.parametrize("quantity", [0, -5])
async def test_non_positive_quantity_rejected(quantity: int) -> None:
    """quantity <= 0 fails validation before the service is ever involved."""
    with pytest.raises(ValidationError):
        _txn(1, "BUY", quantity, 1000.0, 10)


async def test_nonexistent_bond_rejected(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, _bond_id = user_and_bond_ids

    with pytest.raises(Exception, match=r"InvalidTransactionError|FOREIGN KEY|foreign key"):
        await service.add_transaction(user_id, _txn(999_999, "BUY", 1, 1000.0, 10))


# --------------------------------------------------------------------- #
# get_position


async def test_position_avg_price_and_invested(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 3, 1050.0, 15))

    position = await service.get_position(user_id, bond_id)
    assert position.user_id == user_id
    assert position.bond_id == bond_id
    assert position.quantity == 7
    # Only BUY prices count: the SELL at 1050 must not affect the average.
    assert position.avg_buy_price == pytest.approx(1000.0)
    assert position.total_invested == pytest.approx(7000.0)


async def test_position_after_all_sold(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 10, 1050.0, 15))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 0
    assert position.avg_buy_price is None
    assert position.total_invested == 0.0


async def test_weighted_average_of_multiple_buys(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 5, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 5, 1200.0, 12))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 5, 1100.0, 15))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 5
    assert position.avg_buy_price == pytest.approx(1100.0)
    assert position.total_invested == pytest.approx(5500.0)


async def test_avg_price_average_cost_worked_example(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    """Worked example: buy 10@1000, sell 5, buy 5@1100 -> open 10 @ 1050."""
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 5, 1050.0, 15))
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 5, 1100.0, 20))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 10
    assert position.avg_buy_price == pytest.approx(1050.0)
    assert position.total_invested == pytest.approx(10500.0)


async def test_avg_price_resets_after_full_close(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    """After a full close, only subsequent BUYs form the new average."""
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 10, 1050.0, 15))
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 5, 1100.0, 20))

    position = await service.get_position(user_id, bond_id)
    assert position.quantity == 5
    assert position.avg_buy_price == pytest.approx(1100.0)
    assert position.total_invested == pytest.approx(5500.0)


# --------------------------------------------------------------------- #
# get_all_positions


async def test_get_all_positions_returns_only_non_zero(
    service: PortfolioService,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    other_bond_id = await _add_second_bond(session_factory, user_id)

    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 10, 1050.0, 15))  # closed
    await service.add_transaction(user_id, _txn(other_bond_id, "BUY", 5, 900.0, 12))

    positions = await service.get_all_positions(user_id)
    assert [p.bond_id for p in positions] == [other_bond_id]
    assert positions[0].quantity == 5


# --------------------------------------------------------------------- #
# list_transactions


async def test_list_transactions_ordering_and_filter(
    service: PortfolioService,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    other_bond_id = await _add_second_bond(session_factory, user_id)

    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 5))
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 4, 1010.0, 3))  # earlier date
    await service.add_transaction(
        user_id, _txn(bond_id, "SELL", 2, 1050.0, 5)
    )  # same date, later id
    await service.add_transaction(user_id, _txn(other_bond_id, "BUY", 7, 900.0, 1))

    all_txns = await service.list_transactions(user_id)
    assert [(t.date, t.id) for t in all_txns] == sorted((t.date, t.id) for t in all_txns)
    assert len(all_txns) == 4

    bond_txns = await service.list_transactions(user_id, bond_id=bond_id)
    assert len(bond_txns) == 3
    assert all(t.bond_id == bond_id for t in bond_txns)
    # Ordered by date, then id: the 01-03 BUY (inserted second) first,
    # then the two 01-05 rows by id.
    assert [t.id for t in bond_txns] == [2, 1, 3]
    assert [t.date for t in bond_txns] == [
        datetime.date(2026, 1, 3),
        datetime.date(2026, 1, 5),
        datetime.date(2026, 1, 5),
    ]
    assert [t.type for t in bond_txns] == ["BUY", "BUY", "SELL"]


# --------------------------------------------------------------------- #
# event bus


async def test_events_published_on_buy(
    service: PortfolioService,
    event_bus: AsyncQueueEventBus,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    created: list[Message] = []
    updated: list[Message] = []
    event_bus.subscribe(Topic.TRANSACTION_CREATED, _collector(created))
    event_bus.subscribe(Topic.POSITION_UPDATED, _collector(updated))

    dto = await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))

    await _wait_until(lambda: len(created) >= 1 and len(updated) >= 1)

    assert created[0].topic == "transaction.created"
    created_payload: dict[str, Any] = dict(created[0].payload)
    assert created_payload["id"] == dto.id
    assert created_payload["user_id"] == user_id
    assert created_payload["bond_id"] == bond_id
    assert created_payload["type"] == "BUY"
    assert created_payload["quantity"] == 10
    assert created_payload["price"] == 1000.0
    assert created_payload["date"] == datetime.date(2026, 1, 10)

    assert updated[0].topic == "position.updated"
    updated_payload: dict[str, Any] = dict(updated[0].payload)
    assert updated_payload["user_id"] == user_id
    assert updated_payload["bond_id"] == bond_id
    assert updated_payload["quantity"] == 10
    assert updated_payload["avg_buy_price"] == pytest.approx(1000.0)
    assert updated_payload["total_invested"] == pytest.approx(10000.0)


async def test_no_events_published_on_rejected_sell(
    service: PortfolioService,
    event_bus: AsyncQueueEventBus,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    created: list[Message] = []
    event_bus.subscribe(Topic.TRANSACTION_CREATED, _collector(created))

    with pytest.raises(InsufficientPositionError):
        await service.add_transaction(user_id, _txn(bond_id, "SELL", 1, 1000.0, 10))

    await asyncio.sleep(0.05)
    assert created == []

    # Nothing was written either.
    async with service._session_factory() as session:
        result = await session.execute(select(Transaction))
        assert result.scalars().all() == []


# --------------------------------------------------------------------- #
# get_position_value


async def test_get_position_value_uses_avg_buy_price(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    """Position value uses the average-cost basis, not the latest BUY price.

    BUY 10@1000 + BUY 2@1100 -> cost basis 12200 over 12 units (average
    12200/12), so the value of the open position is 12 * avg = 12200 — the
    total cost of the currently open position.
    """
    user_id, bond_id = user_and_bond_ids
    assert await service.get_position_value(user_id, bond_id) == 0.0

    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 2, 1100.0, 20))

    assert await service.get_position_value(user_id, bond_id) == pytest.approx(12200.0)


async def test_get_position_value_zero_after_close(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "MATURE", 10, 1000.0, 31))

    assert await service.get_position_value(user_id, bond_id) == 0.0


async def test_get_position_value_without_history_is_zero(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    """A pair with no transaction history at all values to 0.0."""
    user_id, bond_id = user_and_bond_ids
    assert await service.get_position_value(user_id, bond_id) == 0.0


async def test_get_position_value_uses_avg_buy_price_after_partial_close(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    """BUY 10@100, SELL 5, BUY 5@110 -> value is 10 * 105 = 1050, not 10 * 110."""
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 100.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 5, 105.0, 15))
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 5, 110.0, 20))

    assert await service.get_position_value(user_id, bond_id) == pytest.approx(1050.0)


async def test_get_position_value_matches_position_total_invested(
    service: PortfolioService, user_and_bond_ids: tuple[int, int]
) -> None:
    """get_position_value and PositionDTO.total_invested agree (same net_position)."""
    user_id, bond_id = user_and_bond_ids
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 100.0, 10))
    await service.add_transaction(user_id, _txn(bond_id, "SELL", 5, 105.0, 15))
    await service.add_transaction(user_id, _txn(bond_id, "BUY", 5, 110.0, 20))

    position = await service.get_position(user_id, bond_id)
    value = await service.get_position_value(user_id, bond_id)

    assert value == pytest.approx(position.total_invested)
    assert position.quantity == 10
