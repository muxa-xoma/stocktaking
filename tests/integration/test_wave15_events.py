"""Wave 15 regression tests for event-bus behavior (Tasks 23, 29, 30, 31).

Split out of the former monolithic ``test_wave15.py``: bus semantics without
REST — the pure ``AsyncQueueEventBus`` guarantees, plus the DB-backed event
flows (bond fan-out, analytics recalculation and cache invalidation) on a
migrated temporary SQLite database.

* T23 — per-holder ``bond.updated`` fan-out with ``user_id``, analytics recalc.
* T29 — analytics summary cache (hit + event-driven invalidation).
* T30 — ``request()`` never hangs when the handler raises.
* T31 — subscribe/unsubscribe during routing is safe.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from bond_accounting.analytics import AnalyticsService, PortfolioSummary, attach_to_event_bus
from bond_accounting.bonds import BondCreate, BondService, BondUpdate
from bond_accounting.config.settings import DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.db.models import Bond, Broker, BrokerAccount, Transaction, User
from bond_accounting.event_bus import AsyncQueueEventBus, Message, RequestHandlerError, Topic
from bond_accounting.portfolio import PortfolioService, TransactionCreate

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


async def _drain(events: list[Message], count: int, timeout: float = 2.0) -> list[Message]:
    """Wait until ``count`` messages were delivered to the recorder."""
    async with asyncio.timeout(timeout):
        while len(events) < count:
            await asyncio.sleep(0)
    return events


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Condition not met within timeout (async event not delivered?)")
        await asyncio.sleep(0.01)


async def _seed_user(session_factory: async_sessionmaker[AsyncSession], username: str) -> int:
    async with session_factory() as session:
        user = User(username=username, password_hash="not-a-real-hash")
        session.add(user)
        await session.commit()
        return user.id


async def _seed_account(session_factory: async_sessionmaker[AsyncSession], user_id: int) -> int:
    """Insert a broker and an account for ``user_id``; return the account id.

    ``TransactionCreate`` requires a ``broker_account_id`` owned by the user,
    and direct ``Transaction`` inserts need a NOT NULL ``broker_account_id``.
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


def _recorder(events: list[Message]) -> Callable[[Message], Awaitable[None]]:
    async def handler(message: Message) -> None:
        events.append(message)

    return handler


# =========================================================================== #
# 1. Bond events with user_id (Task 23)
# =========================================================================== #


async def test_bond_updated_fans_out_one_event_per_holder(
    bond_service: BondService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two holders on the same bond -> exactly two events, one per user."""
    owner_id = await _seed_user(session_factory, "event-owner")
    holder_b = await _seed_user(session_factory, "event-holder-b")
    created = await bond_service.create(
        BondCreate(
            isin="RU000A0JW7E4",
            name="Fan-out bond",
            nominal=1000,
            coupon_rate=7.5,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
        ),
        user_id=owner_id,
    )
    async with session_factory() as session:
        for user_id in (owner_id, holder_b):
            broker = Broker(name=f"fanout-broker-{user_id}", commission=0.3)
            session.add(broker)
            await session.flush()
            account = BrokerAccount(
                user_id=user_id, broker_id=broker.id, name="Основной", account_type="STANDARD"
            )
            session.add(account)
            await session.flush()
            session.add(
                Transaction(
                    user_id=user_id,
                    bond_id=created.id,
                    broker_account_id=account.id,
                    type="BUY",
                    quantity=1,
                    price=1000.0,
                    date=datetime.date(2026, 1, 10),
                )
            )
        await session.commit()

    events: list[Message] = []
    unsubscribe = bond_service._event_bus.subscribe(Topic.BOND_UPDATED, _recorder(events))
    try:
        updated = await bond_service.update(
            created.id, BondUpdate(name="Fan-out bond renamed"), user_id=owner_id
        )
        assert updated is not None

        messages = await _drain(events, 2)
        assert sorted(message.payload["user_id"] for message in messages) == sorted(
            [owner_id, holder_b]
        )
        for message in messages:
            assert message.payload["id"] == created.id
            assert message.payload["name"] == "Fan-out bond renamed"
            assert message.sender == "bonds"
    finally:
        unsubscribe()


async def test_bond_updated_without_holders_publishes_nothing(
    bond_service: BondService,
) -> None:
    """A bond with no holders is updated silently (nobody to recalculate)."""
    owner_id = await _seed_user(bond_service._session_factory, "silent-owner")
    created = await bond_service.create(
        BondCreate(
            isin="RU000A0JW8F5",
            name="No holders bond",
            nominal=1000,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
        ),
        user_id=owner_id,
    )

    events: list[Message] = []
    unsubscribe = bond_service._event_bus.subscribe(Topic.BOND_UPDATED, _recorder(events))
    try:
        updated = await bond_service.update(
            created.id, BondUpdate(name="Still no holders"), user_id=owner_id
        )
        assert updated is not None
        await asyncio.sleep(0.05)
        assert events == []
    finally:
        unsubscribe()


async def test_analytics_recalculates_specific_user_on_bond_events(
    analytics_service: AnalyticsService,
    event_bus: AsyncQueueEventBus,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``bond.updated`` / ``bond.deleted`` payloads with ``user_id`` trigger
    a recalculation for exactly that user."""
    user_a = await _seed_user(session_factory, "recalc-a")
    user_b = await _seed_user(session_factory, "recalc-b")
    unsubscribers = attach_to_event_bus(analytics_service, event_bus)
    recalculated: list[Message] = []
    unsubscribe = event_bus.subscribe(Topic.PORTFOLIO_RECALCULATED, _recorder(recalculated))
    try:
        await event_bus.publish(Topic.BOND_UPDATED, {"id": 1, "user_id": user_a}, sender="bonds")
        await _wait_until(lambda: len(recalculated) >= 1)
        assert recalculated[-1].payload["user_id"] == user_a

        await event_bus.publish(
            Topic.BOND_DELETED, {"bond_id": 1, "user_id": user_b}, sender="bonds"
        )
        await _wait_until(lambda: len(recalculated) >= 2)
        assert recalculated[-1].payload["user_id"] == user_b
    finally:
        unsubscribe()
        for detach in unsubscribers:
            detach()


# =========================================================================== #
# 2. Analytics caching (Task 29)
# =========================================================================== #


async def test_analytics_summary_cached_until_event_invalidates_it(
    analytics_service: AnalyticsService,
    portfolio_service: PortfolioService,
    event_bus: AsyncQueueEventBus,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Second GET is served from the cache; a transaction event forces a
    recompute; GET paths never publish events."""

    user_id = await _seed_user(session_factory, "cache-user")
    account_id = await _seed_account(session_factory, user_id)
    async with session_factory() as session:
        bond = Bond(
            isin="RU000A0JX3K9",
            name="Cache bond",
            nominal=1000,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2030, 1, 1),
            owner_id=user_id,
        )
        session.add(bond)
        await session.commit()
        bond_id = bond.id

    compute_calls = 0
    original_compute = analytics_service._compute_summary

    async def counting_compute(user_id: int, valuation_date: datetime.date) -> PortfolioSummary:
        nonlocal compute_calls
        compute_calls += 1
        return await original_compute(user_id, valuation_date)

    # Test-only instrumentation of the bound method.
    analytics_service._compute_summary = counting_compute  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    recalculated: list[Message] = []
    unsubscribe = event_bus.subscribe(Topic.PORTFOLIO_RECALCULATED, _recorder(recalculated))
    unsubscribers = attach_to_event_bus(analytics_service, event_bus)
    try:
        # First read: computed and cached.
        first = await analytics_service.get_portfolio_summary(user_id)
        assert compute_calls == 1
        assert first.positions == []

        # Second read (no events in between): served from the cache.
        second = await analytics_service.get_portfolio_summary(user_id)
        assert compute_calls == 1
        assert second.positions == []

        # GET paths never publish.
        await asyncio.sleep(0.05)
        assert recalculated == []

        # A transaction invalidates the cache. add_transaction publishes two
        # change events (transaction.created + position.updated), each
        # triggering a recalculation that recomputes and refreshes the cache;
        # the next GET is again a cache hit.
        await portfolio_service.add_transaction(
            user_id,
            TransactionCreate(
                bond_id=bond_id,
                broker_account_id=account_id,
                type="BUY",
                quantity=2,
                price=1000.0,
                date=datetime.date(2026, 1, 10),
            ),
        )
        await _wait_until(lambda: len(recalculated) >= 2)
        assert compute_calls == 3  # two events, two recomputes

        third = await analytics_service.get_portfolio_summary(user_id)
        assert compute_calls == 3  # served from the refreshed cache
        assert len(third.positions) == 1
        assert third.positions[0].quantity == 2
    finally:
        unsubscribe()
        for detach in unsubscribers:
            detach()


# =========================================================================== #
# 3. Event bus request() guarantee (Task 30)
# =========================================================================== #


async def test_request_with_failing_handler_does_not_hang(
    event_bus: AsyncQueueEventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising request handler resolves the pending future with an error:
    ``request(timeout=None)`` never hangs, and the failure is logged."""
    events: list[Message] = []

    async def failing_handler(message: Message) -> dict[str, int]:
        events.append(message)
        raise RuntimeError("boom")

    unsubscribe = event_bus.request_handler(Topic.BOND_CREATED, failing_handler)
    try:
        with (
            caplog.at_level(logging.ERROR, logger="bond_accounting.event_bus.async_queue_bus"),
            # timeout=None would hang forever without the resolution guarantee;
            # wait_for merely guards the test itself.
            pytest.raises(RequestHandlerError) as exc_info,
        ):
            await asyncio.wait_for(
                event_bus.request(Topic.BOND_CREATED, {"v": 1}, "tests"), timeout=2.0
            )
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert len(events) == 1  # the handler really ran
        assert any("Event handler failed" in record.getMessage() for record in caplog.records)
    finally:
        unsubscribe()

    # The bus survived and still serves requests.
    async def echo(message: Message) -> dict[str, int]:
        return {"ok": 1}

    unsubscribe = event_bus.request_handler(Topic.BOND_CREATED, echo)
    try:
        assert await event_bus.request(Topic.BOND_CREATED, {"v": 2}, "tests") == {"ok": 1}
    finally:
        unsubscribe()


async def test_publish_to_failing_request_handler_is_fire_and_forget(
    event_bus: AsyncQueueEventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """``publish()`` semantics are unchanged: a failing request handler only
    produces an ERROR log, the publisher is unaffected."""

    async def failing_handler(message: Message) -> dict[str, int]:
        raise RuntimeError("boom")

    unsubscribe = event_bus.request_handler(Topic.BOND_DELETED, failing_handler)
    try:
        with caplog.at_level(logging.ERROR, logger="bond_accounting.event_bus.async_queue_bus"):
            # publish() is fire-and-forget: it returns after routing, so the
            # dispatcher task processes (and fails) the handler afterwards.
            message_id = await event_bus.publish(Topic.BOND_DELETED, {"n": 1}, "tests")
            assert message_id
            await _wait_until(
                lambda: any("Event handler failed" in r.getMessage() for r in caplog.records)
            )
        assert event_bus.running
    finally:
        unsubscribe()


# =========================================================================== #
# 4. Event bus thread-safe subscriptions (Task 31)
# =========================================================================== #


async def test_unsubscribe_during_dispatch_does_not_raise(
    event_bus: AsyncQueueEventBus,
) -> None:
    """Unsubscribing a handler while another handler is dispatching the same
    message does not raise ``RuntimeError`` (the subscription registry is
    mutated under a lock, and routing iterates a snapshot).

    Per the documented ``_route`` semantics, a subscription unsubscribed
    mid-flight has its dispatcher cancelled and its queued message dropped —
    the detached second subscriber never sees the in-flight message, and the
    surviving first subscriber keeps receiving future messages.
    """
    received_first: list[int] = []
    received_second: list[int] = []
    unsubscribe_second: Callable[[], None] | None = None

    async def first_handler(message: Message) -> None:
        received_first.append(message.payload["n"])
        assert unsubscribe_second is not None
        unsubscribe_second()  # detach the second subscriber mid-dispatch

    async def second_handler(message: Message) -> None:
        received_second.append(message.payload["n"])

    unsubscribe_first = event_bus.subscribe(Topic.BOND_UPDATED, first_handler)
    unsubscribe_second = event_bus.subscribe(Topic.BOND_UPDATED, second_handler)
    try:
        # Would raise RuntimeError (or the publish would fail) with a
        # non-thread-safe registry; here it routes both queues cleanly.
        await event_bus.publish(Topic.BOND_UPDATED, {"n": 1}, "tests")
        await _wait_until(lambda: len(received_first) == 1)

        # The second subscription was detached before its dispatcher ran:
        # its in-flight message was dropped (cancelled dispatcher + drained
        # queue), not delivered.
        await asyncio.sleep(0.05)
        assert received_second == []

        # The surviving subscriber is unaffected and keeps receiving.
        await event_bus.publish(Topic.BOND_UPDATED, {"n": 2}, "tests")
        await _wait_until(lambda: len(received_first) == 2)
        assert received_second == []
    finally:
        unsubscribe_first()
        assert unsubscribe_second is not None
        unsubscribe_second()  # idempotent


async def test_concurrent_publish_and_churn_no_runtime_error(
    event_bus: AsyncQueueEventBus,
) -> None:
    """Publishing while handlers are subscribed/unsubscribed concurrently
    never raises ``RuntimeError`` (snapshot-then-route under a lock)."""

    async def noop(message: Message) -> None:
        pass

    async def publisher() -> None:
        for n in range(100):
            await event_bus.publish(Topic.POSITION_UPDATED, {"n": n}, "tests")

    async def churn() -> None:
        for _ in range(50):
            unsubscribe = event_bus.subscribe(Topic.POSITION_UPDATED, noop)
            await asyncio.sleep(0)
            unsubscribe()

    # A failing task (RuntimeError) would surface as an exception here.
    await asyncio.gather(publisher(), churn())
    assert event_bus.running
