"""Integration tests for the analytics module: DB + event bus + real services.

Reuses the fixture patterns from ``tests/test_portfolio.py``: a migrated
temporary SQLite database (via alembic), a started ``AsyncQueueEventBus``
and the real Bond/Portfolio/Analytics services.
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

from bond_accounting.analytics import AnalyticsService, attach_to_event_bus
from bond_accounting.config.settings import DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.db.models import Bond, BondCoupon, Broker, BrokerAccount, User
from bond_accounting.event_bus import AsyncQueueEventBus, Topic
from bond_accounting.portfolio import PortfolioService, TransactionCreate

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

#: Fixed valuation date used for deterministic coupon-grid arithmetic.
TODAY = datetime.date(2026, 2, 1)

#: Broker account used by ``_txn`` when none is passed explicitly. Set by the
#: ``broker_account_ids`` fixture (autouse); filtering tests pass an explicit
#: account.
_default_account_id: int | None = None


def _txn(
    bond_id: int,
    type_: TransactionType,
    quantity: int,
    price: float,
    day: int,
    commission: float = 0.0,
    account_id: int | None = None,
) -> TransactionCreate:
    """Build a TransactionCreate dated 2026-01-<day>."""
    resolved = account_id if account_id is not None else _default_account_id
    if resolved is None:
        raise RuntimeError("no broker_account_id: depend on the broker_account_ids fixture")
    return TransactionCreate(
        bond_id=bond_id,
        broker_account_id=resolved,
        type=type_,
        quantity=quantity,
        price=price,
        date=datetime.date(2026, 1, day),
        commission=commission,
    )


async def _wait_until(predicate: Callable[[], bool], timeout: float = _EVENT_TIMEOUT) -> None:
    """Await until ``predicate()`` is true, failing after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Condition not met within {timeout}s (async event not delivered?)")


def _collector(events: list[Message]) -> Callable[[Message], Awaitable[None]]:
    """Build an event handler appending every received message to ``events``."""

    async def handler(message: Message) -> None:
        events.append(message)

    return handler


# --------------------------------------------------------------------- #
# fixtures


@pytest.fixture
def migrated_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create an empty migrated SQLite DB via alembic; return its file path."""
    db_path = tmp_path / "analytics_test.db"
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
def portfolio_service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> PortfolioService:
    """The portfolio service wired to the temp DB and the running bus."""
    return PortfolioService(session_factory, event_bus)


@pytest.fixture
def analytics_service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> AnalyticsService:
    """The analytics service wired to the temp DB and the running bus."""
    return AnalyticsService(session_factory, event_bus)


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
            coupon_period_days=365,
            maturity_date=datetime.date(2030, 1, 1),
            owner_id=user.id,
        )
        session.add(bond)
        await session.commit()
        return user.id, bond.id


@pytest.fixture(autouse=True)
async def broker_account_ids(
    session_factory: async_sessionmaker[AsyncSession],
    user_and_bond_ids: tuple[int, int],
) -> AsyncGenerator[tuple[int, int]]:
    """Two broker accounts of the test user; also sets the ``_txn`` default.

    Autouse: ``TransactionCreate`` requires a ``broker_account_id`` owned by
    the user. The first account is the implicit default; the per-account
    filtering tests pass the second one explicitly.
    """
    global _default_account_id
    user_id, _ = user_and_bond_ids
    async with session_factory() as session:
        broker = Broker(name="Тестовый брокер", commission=0.3)
        session.add(broker)
        await session.flush()
        first = BrokerAccount(
            user_id=user_id, broker_id=broker.id, name="Основной", account_type="STANDARD"
        )
        second = BrokerAccount(
            user_id=user_id, broker_id=broker.id, name="Дополнительный", account_type="IIS"
        )
        session.add_all([first, second])
        await session.commit()
        _default_account_id = first.id
        try:
            yield first.id, second.id
        finally:
            _default_account_id = None


# --------------------------------------------------------------------- #
# get_portfolio_summary


async def test_portfolio_summary_positions_and_realized_pnl(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await portfolio_service.add_transaction(
        user_id, _txn(bond_id, "SELL", 4, 1050.0, 15, commission=5.0)
    )

    summary = await analytics_service.get_portfolio_summary(user_id, today=TODAY)

    assert summary.user_id == user_id
    assert summary.generated_at is not None

    # One open position: 6 units left at the BUY-only average of 1000.
    assert len(summary.positions) == 1
    position = summary.positions[0]
    assert position.bond_id == bond_id
    assert position.quantity == 6
    assert position.avg_buy_price == pytest.approx(1000.0)
    assert position.total_invested == pytest.approx(6000.0)
    # Day-stepped grid anchored on maturity 2030-01-01 (risk R5): stepping
    # back 365 days drifts across the leap day, so coupon dates are
    # 2027-01-02, 2028-01-02, 2029-01-01, 2030-01-01 (not the month-anchored
    # Jan-1 dates of the legacy per-frequency model).
    assert position.next_coupon_date == datetime.date(2027, 1, 2)

    assert summary.total_invested == pytest.approx(6000.0)

    # Known-good realized PnL: sells=200, commissions=5, total=195.
    assert summary.realized_pnl.sells == pytest.approx(200.0)
    assert summary.realized_pnl.maturities == pytest.approx(0.0)
    assert summary.realized_pnl.commissions == pytest.approx(5.0)
    assert summary.realized_pnl.total == pytest.approx(195.0)

    # next_coupons: 7% of 1000 nominal annually on 6 units, ~24-month horizon
    # (TODAY=2026-02-01 → horizon end 2028-02-01, so the 2027 and 2028
    # day-stepped coupon dates only).
    assert [due.date for due in summary.next_coupons] == [
        datetime.date(2027, 1, 2),
        datetime.date(2028, 1, 2),
    ]
    assert all(due.amount == pytest.approx(70.0 * 6) for due in summary.next_coupons)
    assert all(due.isin == "RU000A0JX0J2" for due in summary.next_coupons)

    # upcoming_cashflows: every future coupon (no horizon) + the maturity
    # repayment of the nominal for the 6 remaining units.
    assert [(flow.date, flow.kind) for flow in summary.upcoming_cashflows] == [
        (datetime.date(2027, 1, 2), "COUPON"),
        (datetime.date(2028, 1, 2), "COUPON"),
        (datetime.date(2029, 1, 1), "COUPON"),
        (datetime.date(2030, 1, 1), "COUPON"),
        (datetime.date(2030, 1, 1), "MATURITY"),
    ]
    assert all(
        flow.amount == pytest.approx(420.0)
        for flow in summary.upcoming_cashflows
        if flow.kind == "COUPON"
    )
    maturity_flow = next(f for f in summary.upcoming_cashflows if f.kind == "MATURITY")
    assert maturity_flow.amount == pytest.approx(6000.0)


async def test_portfolio_summary_empty_portfolio(
    analytics_service: AnalyticsService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, _ = user_and_bond_ids

    summary = await analytics_service.get_portfolio_summary(user_id, today=TODAY)

    assert summary.positions == []
    assert summary.total_invested == 0.0
    assert summary.next_coupons == []
    assert summary.upcoming_cashflows == []
    assert summary.realized_pnl.total == 0.0


async def test_portfolio_summary_closed_position_excluded_but_pnl_kept(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await portfolio_service.add_transaction(
        user_id, _txn(bond_id, "SELL", 10, 1050.0, 15, commission=5.0)
    )

    summary = await analytics_service.get_portfolio_summary(user_id, today=TODAY)

    assert summary.positions == []
    assert summary.total_invested == 0.0
    assert summary.realized_pnl.sells == pytest.approx(500.0)
    assert summary.realized_pnl.commissions == pytest.approx(5.0)
    assert summary.realized_pnl.total == pytest.approx(495.0)


async def test_portfolio_summary_read_does_not_publish(
    analytics_service: AnalyticsService,
    event_bus: AsyncQueueEventBus,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    recalculated: list[Message] = []
    event_bus.subscribe(Topic.PORTFOLIO_RECALCULATED, _collector(recalculated))

    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    summary = await analytics_service.get_portfolio_summary(user_id, today=TODAY)
    cached = await analytics_service.get_portfolio_summary(user_id)

    await asyncio.sleep(0.1)

    # Read paths never publish: recalculation is event-driven only
    # (the service is not attached to the bus here, so no handler ran).
    assert recalculated == []

    # The cached read (today=None) returns the same valuation-date-independent
    # numbers as the explicit-valuation read.
    assert cached.user_id == user_id
    assert len(cached.positions) == len(summary.positions)
    assert cached.positions[0].total_invested == pytest.approx(summary.positions[0].total_invested)


# --------------------------------------------------------------------- #
# get_position_analytics


async def test_get_position_analytics_open_position(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))

    analytics = await analytics_service.get_position_analytics(user_id, bond_id, today=TODAY)

    assert analytics is not None
    assert analytics.quantity == 10
    assert analytics.avg_buy_price == pytest.approx(1000.0)
    assert analytics.current_yield == pytest.approx(0.07)


async def test_get_position_analytics_no_history_returns_none(
    analytics_service: AnalyticsService, user_and_bond_ids: tuple[int, int]
) -> None:
    user_id, bond_id = user_and_bond_ids
    assert await analytics_service.get_position_analytics(user_id, bond_id, today=TODAY) is None


async def test_get_position_analytics_closed_position_returns_none(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await portfolio_service.add_transaction(user_id, _txn(bond_id, "SELL", 10, 1050.0, 15))

    assert await analytics_service.get_position_analytics(user_id, bond_id, today=TODAY) is None


async def test_get_position_analytics_actual_coupon_schedule_changes_ytm(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    session_factory: async_sessionmaker[AsyncSession],
    user_and_bond_ids: tuple[int, int],
) -> None:
    """A populated ``bond_coupons`` schedule is authoritative for YTM.

    The actual dates/amounts replace the grid derived from
    ``coupon_period_days`` inside ``calculate_ytm`` (see the
    ``BondCoupon`` docstring), so a materially richer schedule raises
    the yield. The grid still governs ``next_coupon_date``.
    """
    user_id, bond_id = user_and_bond_ids
    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))

    baseline = await analytics_service.get_position_analytics(user_id, bond_id, today=TODAY)
    assert baseline is not None
    assert baseline.ytm is not None

    # Actual schedule: 100 per bond per payment (10% on the 1000 nominal)
    # instead of the derived 7% annual grid.
    async with session_factory() as session:
        session.add_all(
            [
                BondCoupon(
                    bond_id=bond_id, coupon_date=datetime.date(2026, 8, 1), coupon_amount=100.0
                ),
                BondCoupon(
                    bond_id=bond_id, coupon_date=datetime.date(2027, 8, 1), coupon_amount=100.0
                ),
                BondCoupon(
                    bond_id=bond_id, coupon_date=datetime.date(2028, 8, 1), coupon_amount=100.0
                ),
                BondCoupon(
                    bond_id=bond_id, coupon_date=datetime.date(2030, 1, 1), coupon_amount=100.0
                ),
            ]
        )
        await session.commit()

    with_schedule = await analytics_service.get_position_analytics(user_id, bond_id, today=TODAY)
    assert with_schedule is not None
    assert with_schedule.ytm is not None
    assert with_schedule.ytm > baseline.ytm
    # next_coupon_date stays anchored on the coupon_period_days grid.
    assert with_schedule.next_coupon_date == baseline.next_coupon_date


# --------------------------------------------------------------------- #
# attach_to_event_bus


async def test_attach_to_event_bus_recalculates_on_transaction_created(
    analytics_service: AnalyticsService,
    event_bus: AsyncQueueEventBus,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
) -> None:
    user_id, bond_id = user_and_bond_ids
    unsubscribers = attach_to_event_bus(analytics_service, event_bus)
    try:
        recalculated: list[Message] = []
        event_bus.subscribe(Topic.PORTFOLIO_RECALCULATED, _collector(recalculated))

        # A transaction.created event carrying user_id triggers a recalculation,
        # which itself publishes portfolio.recalculated.
        await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))

        await _wait_until(lambda: len(recalculated) >= 1)

        message = recalculated[-1]
        assert message.topic == "portfolio.recalculated"
        assert message.sender == "analytics"
        payload: dict[str, Any] = dict(message.payload)
        assert payload["user_id"] == user_id
        assert payload["total_invested"] == pytest.approx(10_000.0)
    finally:
        for unsubscribe in unsubscribers:
            unsubscribe()


async def test_attach_to_event_bus_ignores_events_without_user_id(
    analytics_service: AnalyticsService, event_bus: AsyncQueueEventBus
) -> None:
    unsubscribers = attach_to_event_bus(analytics_service, event_bus)
    try:
        recalculated: list[Message] = []
        event_bus.subscribe(Topic.PORTFOLIO_RECALCULATED, _collector(recalculated))

        # A malformed event without user_id: no recalculation must happen.
        await event_bus.publish(Topic.BOND_DELETED, {"bond_id": 1}, sender="bonds")
        await asyncio.sleep(0.1)

        assert recalculated == []
    finally:
        for unsubscribe in unsubscribers:
            unsubscribe()


# --------------------------------------------------------------------- #
# broker_account_id filtering


async def test_portfolio_summary_by_account(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
    broker_account_ids: tuple[int, int],
) -> None:
    """A per-account summary counts only that account's transactions."""
    user_id, bond_id = user_and_bond_ids
    first, second = broker_account_ids

    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await portfolio_service.add_transaction(
        user_id, _txn(bond_id, "BUY", 4, 1000.0, 12, account_id=second)
    )
    await portfolio_service.add_transaction(
        user_id, _txn(bond_id, "SELL", 4, 1050.0, 15, commission=5.0, account_id=second)
    )

    first_summary = await analytics_service.get_portfolio_summary(
        user_id, today=TODAY, broker_account_id=first
    )
    assert len(first_summary.positions) == 1
    assert first_summary.positions[0].quantity == 10
    assert first_summary.total_invested == pytest.approx(10_000.0)
    # No sells happened on the first account.
    assert first_summary.realized_pnl.total == 0.0

    second_summary = await analytics_service.get_portfolio_summary(
        user_id, today=TODAY, broker_account_id=second
    )
    # The second account bought 4 and sold them all: no open position, and
    # the realized PnL is that account's sells (200) minus commissions (5).
    assert second_summary.positions == []
    assert second_summary.realized_pnl.sells == pytest.approx(200.0)
    assert second_summary.realized_pnl.commissions == pytest.approx(5.0)
    assert second_summary.realized_pnl.total == pytest.approx(195.0)


async def test_position_analytics_by_account(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    user_and_bond_ids: tuple[int, int],
    broker_account_ids: tuple[int, int],
) -> None:
    """get_position_analytics is restricted to one account's transactions."""
    user_id, bond_id = user_and_bond_ids
    first, second = broker_account_ids

    await portfolio_service.add_transaction(user_id, _txn(bond_id, "BUY", 10, 1000.0, 10))
    await portfolio_service.add_transaction(
        user_id, _txn(bond_id, "BUY", 5, 1100.0, 11, account_id=second)
    )

    first_analytics = await analytics_service.get_position_analytics(
        user_id, bond_id, today=TODAY, broker_account_id=first
    )
    assert first_analytics is not None
    assert first_analytics.quantity == 10
    assert first_analytics.avg_buy_price == pytest.approx(1000.0)

    second_analytics = await analytics_service.get_position_analytics(
        user_id, bond_id, today=TODAY, broker_account_id=second
    )
    assert second_analytics is not None
    assert second_analytics.quantity == 5
    assert second_analytics.avg_buy_price == pytest.approx(1100.0)
