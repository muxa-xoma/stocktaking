"""Wave 15 regression tests for the DB/service layer (Tasks 28, 33, 37, 44).

Split out of the former monolithic ``test_wave15.py``: service-level guards
and persistence on a migrated temporary SQLite database, plus engine-creation
pragma validation. No REST, no UI.

* T28 — SELL position guard (exact/insufficient).
* T33 — SQLite pragma whitelist.
* T37 — publish-before-commit rollback on a stopped bus.
* T44 — ``avg_buy_price`` open-position-only semantics end-to-end.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from bond_accounting.analytics import AnalyticsService
from bond_accounting.bonds import BondCreate, BondService, BondUpdate
from bond_accounting.bonds.service import BondNotOwnedError
from bond_accounting.config.settings import DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.db.models import Bond, Broker, BrokerAccount, Transaction, User
from bond_accounting.event_bus import AsyncQueueEventBus
from bond_accounting.portfolio import InsufficientPositionError, PortfolioService, TransactionCreate

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from bond_accounting.portfolio.dto import TransactionType

#: Project root (where ``alembic.ini`` lives).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Fake, but ≥32-byte JWT secret: shorter HMAC keys trigger PyJWT's
#: ``InsecureKeyLengthWarning`` (RFC 7518 Section 3.2).
JWT_SECRET = "wave15-test-jwt-secret-0123456789abcdef012345"


# --------------------------------------------------------------------------- #
# shared fixtures (mirrors test_analytics_integration.py)
# --------------------------------------------------------------------------- #


@pytest.fixture
def migrated_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create an empty migrated SQLite DB via alembic; return its file path."""
    db_path = tmp_path / "wave15_test.db"
    monkeypatch.setenv("BOND_DATABASE__SQLITE_PATH", str(db_path))
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", JWT_SECRET)
    alembic_cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")
    return str(db_path)


@pytest.fixture
async def session_factory(
    migrated_db_url: str,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
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
def bond_service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> BondService:
    return BondService(session_factory, event_bus)


@pytest.fixture
def portfolio_service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> PortfolioService:
    return PortfolioService(session_factory, event_bus)


@pytest.fixture
def analytics_service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> AnalyticsService:
    return AnalyticsService(session_factory, event_bus)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _seed_user(session_factory: async_sessionmaker[AsyncSession], username: str) -> int:
    async with session_factory() as session:
        user = User(username=username, password_hash="not-a-real-hash")
        session.add(user)
        await session.commit()
        return user.id


async def _seed_account(session_factory: async_sessionmaker[AsyncSession], user_id: int) -> int:
    """Insert a broker and an account for ``user_id``; return the account id.

    ``TransactionCreate`` requires a ``broker_account_id`` owned by the user.
    """
    async with session_factory() as session:
        broker = Broker(name=f"broker-{user_id}-{uuid.uuid4().hex[:8]}", commission=0.3)
        session.add(broker)
        await session.flush()
        account = BrokerAccount(
            user_id=user_id, broker_id=broker.id, name="Основной", account_type="STANDARD"
        )
        session.add(account)
        await session.commit()
        return account.id


# =========================================================================== #
# 1. Bond ownership service guard (Task 24)
# =========================================================================== #


async def test_bond_service_rejects_non_owner_mutation(
    bond_service: BondService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Service-level guard: ``BondNotOwnedError`` for a non-owner."""
    owner_id = await _seed_user(session_factory, "own-owner")
    stranger_id = await _seed_user(session_factory, "own-stranger")
    created = await bond_service.create(
        BondCreate(
            isin="RU000A0JX1H7",
            name="Guarded bond",
            nominal=1000,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
        ),
        user_id=owner_id,
    )

    with pytest.raises(BondNotOwnedError):
        await bond_service.update(created.id, BondUpdate(name="nope"), user_id=stranger_id)
    with pytest.raises(BondNotOwnedError):
        await bond_service.delete(created.id, user_id=stranger_id)

    assert await bond_service.get(created.id) is not None


# =========================================================================== #
# 2. SELL/MATURE position guard (Task 28)
# =========================================================================== #


async def test_sell_exact_position_succeeds_and_oversell_rejected(
    portfolio_service: PortfolioService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """SELL of exactly the held quantity succeeds; an oversell is rejected
    atomically (the position is left untouched)."""
    user_id = await _seed_user(session_factory, "sell-guard")
    account_id = await _seed_account(session_factory, user_id)
    async with session_factory() as session:
        bond = Bond(
            isin="RU000A0JX2J8",
            name="Sell guard bond",
            nominal=1000,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
            owner_id=user_id,
        )
        session.add(bond)
        await session.commit()
        bond_id = bond.id

    def txn(type_: TransactionType, quantity: int, day: int) -> TransactionCreate:
        return TransactionCreate(
            bond_id=bond_id,
            broker_account_id=account_id,
            type=type_,
            quantity=quantity,
            price=1000.0,
            date=datetime.date(2026, 1, day),
        )

    await portfolio_service.add_transaction(user_id, txn("BUY", 3, 1))

    # Selling exactly the position closes it.
    await portfolio_service.add_transaction(user_id, txn("SELL", 3, 10))
    assert (await portfolio_service.get_position(user_id, bond_id)).quantity == 0

    # A further SELL is rejected and changes nothing.
    with pytest.raises(InsufficientPositionError):
        await portfolio_service.add_transaction(user_id, txn("SELL", 1, 20))
    assert (await portfolio_service.get_position(user_id, bond_id)).quantity == 0

    # A partial oversell against an open position is also atomic.
    await portfolio_service.add_transaction(user_id, txn("BUY", 5, 25))
    with pytest.raises(InsufficientPositionError):
        await portfolio_service.add_transaction(user_id, txn("SELL", 6, 30))
    assert (await portfolio_service.get_position(user_id, bond_id)).quantity == 5


# =========================================================================== #
# 3. SQLite PRAGMA whitelist (Task 33)
# =========================================================================== #


def test_invalid_pragma_string_value_rejected_at_engine_creation(tmp_path: Path) -> None:
    config = DatabaseConfig(
        sqlite_path=str(tmp_path / "pragma.db"), connect_args={"journal_mode": "hacked"}
    )
    with pytest.raises(ValueError, match="journal_mode"):
        create_engine_from_settings(config)


def test_invalid_pragma_name_rejected_at_engine_creation(tmp_path: Path) -> None:
    config = DatabaseConfig(
        sqlite_path=str(tmp_path / "pragma.db"), connect_args={"bad name; DROP": 1}
    )
    with pytest.raises(ValueError, match="identifier"):
        create_engine_from_settings(config)


async def test_whitelisted_pragma_override_creates_engine(tmp_path: Path) -> None:
    """A whitelisted keyword override is accepted (defaults keep working)."""
    config = DatabaseConfig(
        sqlite_path=str(tmp_path / "pragma_ok.db"), connect_args={"journal_mode": "wal"}
    )
    engine = create_engine_from_settings(config)
    await engine.dispose()


# =========================================================================== #
# 4. Publish before commit (Task 37)
# =========================================================================== #


async def _fresh_env(migrated_db_url: str) -> tuple[BondService, AsyncQueueEventBus, AsyncEngine]:
    engine = create_engine_from_settings(DatabaseConfig(sqlite_path=migrated_db_url))
    bus = AsyncQueueEventBus(EventBusConfig())
    return BondService(create_session_factory(engine), bus), bus, engine


def _wave15_bond(isin: str) -> BondCreate:
    return BondCreate(
        isin=isin,
        name="Rollback bond",
        nominal=1000,
        coupon_rate=5.0,
        coupon_frequency="ANNUAL",
        maturity_date=datetime.date(2030, 1, 1),
    )


async def test_create_with_stopped_bus_rolls_back(migrated_db_url: str) -> None:
    service, _bus, engine = await _fresh_env(migrated_db_url)  # bus never started
    try:
        user_id = await _seed_user(service._session_factory, "rollback-create-owner")
        with pytest.raises(RuntimeError, match="not running"):
            await service.create(_wave15_bond("RU000A0JX9R5"), user_id=user_id)

        assert await service.get_by_isin("RU000A0JX9R5") is None
    finally:
        await engine.dispose()


async def test_update_with_stopped_bus_rolls_back(migrated_db_url: str) -> None:
    service, bus, engine = await _fresh_env(migrated_db_url)
    try:
        owner_id = await _seed_user(service._session_factory, "rollback-owner")
        await bus.start()
        created = await service.create(_wave15_bond("RU000A0JY0S6"), user_id=owner_id)
        # Make the owner a holder so the update actually publishes.
        async with service._session_factory() as session:
            broker = Broker(name=f"rollback-broker-{uuid.uuid4().hex[:8]}", commission=0.3)
            session.add(broker)
            await session.flush()
            account = BrokerAccount(
                user_id=owner_id, broker_id=broker.id, name="Основной", account_type="STANDARD"
            )
            session.add(account)
            await session.flush()
            txn = Transaction(
                user_id=owner_id,
                bond_id=created.id,
                broker_account_id=account.id,
                type="BUY",
                quantity=1,
                price=1000.0,
                date=datetime.date(2026, 1, 10),
            )
            session.add(txn)
            await session.commit()
        await bus.stop()

        with pytest.raises(RuntimeError, match="not running"):
            await service.update(created.id, BondUpdate(name="lost"), user_id=owner_id)

        fetched = await service.get(created.id)
        assert fetched is not None
        assert fetched.name == "Rollback bond"  # unchanged
    finally:
        await engine.dispose()


async def test_delete_with_stopped_bus_rolls_back(migrated_db_url: str) -> None:
    """Delete publishes before commit; a stopped bus aborts the deletion.

    Deletion is blocked while transactions exist, so the per-holder fan-out is
    normally unreachable. A test-only subclass simulates a holder to exercise
    the publish-before-commit ordering of the delete path.
    """

    class ServiceWithHolder(BondService):
        @staticmethod
        async def _holder_ids(session, bond_id: int) -> list[int]:
            return [1]

    engine = create_engine_from_settings(DatabaseConfig(sqlite_path=migrated_db_url))
    session_factory = create_session_factory(engine)
    bus = AsyncQueueEventBus(EventBusConfig())
    service = ServiceWithHolder(session_factory, bus)
    try:
        owner_id = await _seed_user(session_factory, "rollback-del-owner")
        await bus.start()
        created = await service.create(_wave15_bond("RU000A0JY1T7"), user_id=owner_id)
        await bus.stop()

        with pytest.raises(RuntimeError, match="not running"):
            await service.delete(created.id, user_id=owner_id)

        assert await service.get(created.id) is not None  # survived the rollback
    finally:
        await engine.dispose()


# =========================================================================== #
# 5. avg_buy_price open-position-only (Task 44)
# =========================================================================== #


async def test_avg_buy_price_open_position_only(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The three card scenarios, end-to-end through the analytics service.

    * buy 10@100, sell 5, buy 5@110 -> open 10 @ 105 (running average);
    * buy 10@100, sell 10 (flat) -> no open position;
    * buy 10@100, sell 10, buy 5@200 -> fresh average 200 after the close.
    """
    today = datetime.date(2026, 2, 1)
    users = {
        scenario: await _seed_user(session_factory, f"avg-{scenario}")
        for scenario in ("running", "flat", "reset")
    }
    accounts = {
        scenario: await _seed_account(session_factory, user) for scenario, user in users.items()
    }
    async with session_factory() as session:
        bond = Bond(
            isin="RU000A0JY3V9",
            name="Average bond",
            nominal=1000,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
            owner_id=users["running"],
        )
        session.add(bond)
        await session.commit()
        bond_id = bond.id

    def txn(
        type_: TransactionType, quantity: int, price: float, day: int, scenario: str
    ) -> TransactionCreate:
        return TransactionCreate(
            bond_id=bond_id,
            broker_account_id=accounts[scenario],
            type=type_,
            quantity=quantity,
            price=price,
            date=datetime.date(2026, 1, day),
        )

    # Scenario 1: running average across a partial close.
    uid = users["running"]
    await portfolio_service.add_transaction(uid, txn("BUY", 10, 100.0, 1, "running"))
    await portfolio_service.add_transaction(uid, txn("SELL", 5, 150.0, 10, "running"))
    await portfolio_service.add_transaction(uid, txn("BUY", 5, 110.0, 20, "running"))
    summary = await analytics_service.get_portfolio_summary(uid, today=today)
    assert len(summary.positions) == 1
    assert summary.positions[0].quantity == 10
    assert summary.positions[0].avg_buy_price == pytest.approx(105.0)
    assert summary.positions[0].total_invested == pytest.approx(1050.0)

    # Scenario 2: flat position -> no open position at all.
    uid = users["flat"]
    await portfolio_service.add_transaction(uid, txn("BUY", 10, 100.0, 1, "flat"))
    await portfolio_service.add_transaction(uid, txn("SELL", 10, 105.0, 10, "flat"))
    summary = await analytics_service.get_portfolio_summary(uid, today=today)
    assert summary.positions == []
    assert summary.total_invested == 0.0

    # Scenario 3: average resets after a full close.
    uid = users["reset"]
    await portfolio_service.add_transaction(uid, txn("BUY", 10, 100.0, 1, "reset"))
    await portfolio_service.add_transaction(uid, txn("SELL", 10, 105.0, 10, "reset"))
    await portfolio_service.add_transaction(uid, txn("BUY", 5, 200.0, 20, "reset"))
    summary = await analytics_service.get_portfolio_summary(uid, today=today)
    assert len(summary.positions) == 1
    assert summary.positions[0].quantity == 5
    assert summary.positions[0].avg_buy_price == pytest.approx(200.0)
    assert summary.positions[0].total_invested == pytest.approx(1000.0)
