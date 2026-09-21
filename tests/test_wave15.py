"""Wave 15 regression tests (Tasks 23-37, 41-44).

One section per feature area; each area has at least one test. Areas already
covered by the pre-existing suite are exercised here only where the card
asks for a behavior not yet asserted anywhere:

* T23 — per-holder ``bond.updated`` fan-out with ``user_id``, analytics recalc.
* T24 — bond ownership over REST (403 for non-owners, global read visibility).
* T25 — YTM fractional (ACT/365) discounting, hand-computed values.
* T26 — YTM hard price bounds ``[1, 10*nominal]`` (nominal-relative since Task 52).
* T27 — weak JWT secret startup warning.
* T28 — SELL position guard (exact/insufficient).
* T29 — analytics summary cache (hit + event-driven invalidation).
* T30 — ``request()`` never hangs when the handler raises.
* T31 — subscribe/unsubscribe during routing is safe.
* T32 — bond DTO validation + ``issuer: null`` clearing semantics.
* T33 — SQLite pragma whitelist.
* T34 — ``nominal`` default 1000.
* T35 — cookie Max-Age/Secure attributes.
* T36 — password ``verify()`` symmetry (covered by ``test_password.py``).
* T37 — publish-before-commit rollback on a stopped bus.
* T41 — PyJWT: API 401 for expired/tampered tokens, jose-compatible token.
* T42/T43 — ``next_coupons`` / ``upcoming_cashflows`` query parameters.
* T44 — ``avg_buy_price`` open-position-only semantics end-to-end.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from nicegui.context import Context

from bond_accounting.analytics import AnalyticsService, PortfolioSummary, attach_to_event_bus
from bond_accounting.api import api_router, build_api_dependencies, register_exception_handlers
from bond_accounting.auth import AuthService, JwtService, PasswordHasher
from bond_accounting.bonds import BondCreate, BondService, BondUpdate
from bond_accounting.bonds.service import BondNotOwnedError
from bond_accounting.config.settings import (
    AppConfig,
    AuthConfig,
    DatabaseConfig,
    EventBusConfig,
    Settings,
    warn_if_weak_jwt_secret,
)
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.db.models import Bond, Transaction, User
from bond_accounting.event_bus import AsyncQueueEventBus, Message, RequestHandlerError, Topic
from bond_accounting.portfolio import InsufficientPositionError, PortfolioService, TransactionCreate
from bond_accounting.ui import common as ui_common
from bond_accounting.ui.common import clear_token_cookie, set_token_cookie, token_max_age
from bond_accounting.yield_calc import YtmCalculationError, calculate_ytm

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from bond_accounting.portfolio.dto import TransactionType


PROJECT_ROOT = Path(__file__).resolve().parents[1]

JWT_SECRET = "test-secret"

USERNAME = "alice"
PASSWORD = "correct-horse-battery"

# --------------------------------------------------------------------------- #
# shared fixtures (mirrors test_api_rest.py / test_analytics_integration.py)
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


@pytest.fixture
def app(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> FastAPI:
    """The API application assembled like ``main.py``, with real services."""
    jwt_service = JwtService(AuthConfig(jwt_secret=JWT_SECRET))
    auth_service = AuthService(session_factory, PasswordHasher(), jwt_service)
    application = FastAPI()
    application.include_router(api_router)
    register_exception_handlers(application)
    application.dependency_overrides.update(
        build_api_dependencies(
            auth_service,
            jwt_service,
            BondService(session_factory, event_bus),
            PortfolioService(session_factory, event_bus),
            AnalyticsService(session_factory, event_bus),
        ).overrides()
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _register(client: httpx.AsyncClient, username: str = USERNAME) -> tuple[dict, int]:
    """Register a user, log in; return ``(bearer headers, user id)``."""
    response = await client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}, user_id


def _bond_payload(isin: str, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "isin": isin,
        "name": "Wave 15 bond",
        "coupon_rate": 7.0,
        "coupon_frequency": "ANNUAL",
        "maturity_date": "2030-01-01",
    }
    payload.update(overrides)
    return payload


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
            session.add(
                Transaction(
                    user_id=user_id,
                    bond_id=created.id,
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
# 2. Bond ownership (Task 24)
# =========================================================================== #


async def test_bond_ownership_over_rest(client: httpx.AsyncClient) -> None:
    """Only the owner may update/delete; reads are globally visible."""
    owner_headers, owner_id = await _register(client, "owner-alice")
    other_headers, _ = await _register(client, "intruder-bob")

    response = await client.post(
        "/api/bonds", json=_bond_payload("RU000A0JW9G6"), headers=owner_headers
    )
    assert response.status_code == 201, response.text
    bond = response.json()
    assert bond["owner_id"] == owner_id
    bond_id = bond["id"]

    # Global registry: any authenticated user can read.
    response = await client.get("/api/bonds", headers=other_headers)
    assert response.status_code == 200
    assert bond_id in [b["id"] for b in response.json()]
    response = await client.get(f"/api/bonds/{bond_id}", headers=other_headers)
    assert response.status_code == 200
    assert response.json()["isin"] == "RU000A0JW9G6"

    # Non-owner mutations are forbidden.
    response = await client.put(
        f"/api/bonds/{bond_id}", json={"name": "hijacked"}, headers=other_headers
    )
    assert response.status_code == 403
    response = await client.delete(f"/api/bonds/{bond_id}", headers=other_headers)
    assert response.status_code == 403

    # The failed attempts did not damage the bond.
    response = await client.get(f"/api/bonds/{bond_id}", headers=owner_headers)
    assert response.status_code == 200
    assert response.json()["name"] == "Wave 15 bond"

    # The owner may update and delete.
    response = await client.put(
        f"/api/bonds/{bond_id}", json={"name": "renamed by owner"}, headers=owner_headers
    )
    assert response.status_code == 200
    assert response.json()["name"] == "renamed by owner"
    response = await client.delete(f"/api/bonds/{bond_id}", headers=owner_headers)
    assert response.status_code == 204


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
# 3. YTM fractional discounting (Task 25)
# =========================================================================== #


def test_ytm_coupon_one_day_away_discounted_fractionally() -> None:
    """A coupon 1 day away counts as 1 day, not a whole period.

    today=2026-12-29, maturity=2027-06-30, SEMI_ANNUAL, coupon 8% -> payments
    (1 day, 40) and (183 days, 1040). Hand-computed with an independent
    bisection on the ACT/365 fractional formula: r = 0.1438881367.
    """
    ytm = calculate_ytm(
        price=1010.0,
        coupon_rate=8.0,
        coupon_frequency="SEMI_ANNUAL",
        maturity_date=datetime.date(2027, 6, 30),
        nominal=1000,
        today=datetime.date(2026, 12, 29),
    )
    assert ytm is not None
    assert ytm == pytest.approx(0.1438881367, abs=1e-6)


def test_ytm_two_coupon_bond_hand_computed() -> None:
    """Simple 2-coupon bond, hand-computed with fractional discounting.

    today=2026-01-01, maturity=2027-01-01, SEMI_ANNUAL, coupon 10% -> payments
    (181 days, 50) and (365 days, 1050); par price 1000.

    With whole-period discounting the answer would be exactly 0.10 (par bond,
    periods aligned); the fractional ACT/365 formula gives 0.1000205481.
    """
    ytm = calculate_ytm(
        price=1000.0,
        coupon_rate=10.0,
        coupon_frequency="SEMI_ANNUAL",
        maturity_date=datetime.date(2027, 1, 1),
        nominal=1000,
        today=datetime.date(2026, 1, 1),
    )
    assert ytm is not None
    assert ytm == pytest.approx(0.1000205481, abs=1e-6)
    # Discriminating check: whole-period discounting would return exactly 0.10.
    assert abs(ytm - 0.10) > 1e-6


# =========================================================================== #
# 4. YTM price bound (Task 26)
# =========================================================================== #


@pytest.mark.parametrize("price", [0.5, 10000.01])
def test_ytm_price_outside_bounds_raises(price: float) -> None:
    with pytest.raises(YtmCalculationError, match="price"):
        calculate_ytm(
            price=price,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2031, 1, 1),
            today=datetime.date(2026, 1, 1),
        )


@pytest.mark.parametrize("price", [1.0, 9999.0])
def test_ytm_price_boundaries_are_valid(price: float) -> None:
    """Prices exactly at the [1, 9999] bounds pass validation (no raise)."""
    result = calculate_ytm(
        price=price,
        coupon_rate=0.0,  # closed form: always a number within bounds
        coupon_frequency="ANNUAL",
        maturity_date=datetime.date(2027, 1, 1),
        nominal=1000,
        today=datetime.date(2026, 1, 1),
    )
    assert isinstance(result, float)


# =========================================================================== #
# 5. Weak JWT secret guard (Task 27)
# =========================================================================== #


def _settings(jwt_secret: str, debug: bool) -> Settings:
    return Settings(app=AppConfig(debug=debug), auth=AuthConfig(jwt_secret=jwt_secret))


def test_weak_jwt_secret_logs_critical(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.CRITICAL, logger="bond_accounting.config.settings"):
        warn_if_weak_jwt_secret(_settings("test-secret", debug=False))
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical
    assert "weak" in critical[0].getMessage().lower()


def test_strong_jwt_secret_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.CRITICAL, logger="bond_accounting.config.settings"):
        warn_if_weak_jwt_secret(_settings("strong-unique-value", debug=False))
    assert [r for r in caplog.records if r.levelno == logging.CRITICAL] == []


def test_weak_jwt_secret_silent_in_debug_mode(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="bond_accounting.config.settings"):
        warn_if_weak_jwt_secret(_settings("test-secret", debug=True))
    # The check is skipped entirely in debug mode — not even a DEBUG record.
    assert caplog.records == []


# =========================================================================== #
# 6. SELL/MATURE position guard (Task 28)
# =========================================================================== #


async def test_sell_exact_position_succeeds_and_oversell_rejected(
    portfolio_service: PortfolioService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """SELL of exactly the held quantity succeeds; an oversell is rejected
    atomically (the position is left untouched)."""
    user_id = await _seed_user(session_factory, "sell-guard")
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
# 7. Analytics caching (Task 29)
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
    analytics_service._compute_summary = counting_compute  # type: ignore[method-assign]

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
# 8. Event bus request() guarantee (Task 30)
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
# 9. Event bus thread-safe subscriptions (Task 31)
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


# =========================================================================== #
# 10. Bond DTO validation (Task 32)
# =========================================================================== #


@pytest.mark.parametrize(
    "overrides",
    [
        {"coupon_rate": -1},
        {"nominal": 0},
        {"nominal": -100},
    ],
)
async def test_bond_create_invalid_fields_rejected_over_rest(
    client: httpx.AsyncClient, overrides: dict
) -> None:
    headers, _ = await _register(client)
    response = await client.post(
        "/api/bonds", json=_bond_payload("RU000A0JX4L0", **overrides), headers=headers
    )
    assert response.status_code == 422, response.text


async def test_bond_update_issuer_null_clears_field(client: httpx.AsyncClient) -> None:
    """An explicit ``null`` for the nullable ``issuer`` clears the column."""
    headers, _ = await _register(client)
    response = await client.post(
        "/api/bonds",
        json=_bond_payload("RU000A0JX5M1", issuer="Original issuer"),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    bond_id = response.json()["id"]
    assert response.json()["issuer"] == "Original issuer"

    response = await client.put(f"/api/bonds/{bond_id}", json={"issuer": None}, headers=headers)
    assert response.status_code == 200, response.text

    response = await client.get(f"/api/bonds/{bond_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["issuer"] is None


async def test_bond_update_omitted_issuer_unchanged(client: httpx.AsyncClient) -> None:
    """Omitting ``issuer`` from an update leaves the column untouched."""
    headers, _ = await _register(client)
    response = await client.post(
        "/api/bonds", json=_bond_payload("RU000A0JX6N2", issuer="Kept issuer"), headers=headers
    )
    assert response.status_code == 201, response.text
    bond_id = response.json()["id"]

    response = await client.put(f"/api/bonds/{bond_id}", json={"name": "Renamed"}, headers=headers)
    assert response.status_code == 200, response.text

    response = await client.get(f"/api/bonds/{bond_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["issuer"] == "Kept issuer"
    assert response.json()["name"] == "Renamed"


# =========================================================================== #
# 11. SQLite PRAGMA whitelist (Task 33)
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
# 12. Bond nominal DEFAULT 1000 (Task 34)
# =========================================================================== #


async def test_bond_nominal_defaults_to_1000(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    response = await client.post("/api/bonds", json=_bond_payload("RU000A0JX7P3"), headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["nominal"] == 1000

    # Persisted value confirmed by a subsequent read.
    bond_id = response.json()["id"]
    response = await client.get(f"/api/bonds/{bond_id}", headers=headers)
    assert response.json()["nominal"] == 1000


async def test_bond_nominal_explicit_value_persisted(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    response = await client.post(
        "/api/bonds", json=_bond_payload("RU000A0JX8Q4", nominal=5000), headers=headers
    )
    assert response.status_code == 201, response.text
    assert response.json()["nominal"] == 5000


# =========================================================================== #
# 13. Cookie TTL + Secure (Task 35)
# =========================================================================== #


class _FakeUrl:
    def __init__(self, scheme: str) -> None:
        self.scheme = scheme


class _FakeRequest:
    def __init__(self, scheme: str) -> None:
        self.url = _FakeUrl(scheme)


class _FakeClient:
    def __init__(self, scheme: str) -> None:
        self.request = _FakeRequest(scheme)


@pytest.fixture
def captured_cookie_js(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every ``document.cookie`` JS command issued via ``run_javascript``.

    The module object is patched directly (not via a dotted import string):
    NiceGUI's test cleanup pops ``bond_accounting*`` from ``sys.modules`` after
    each UI test, so a string-based lookup could resolve to a freshly imported
    module while ``set_token_cookie`` still uses the original one.
    """
    captured: list[str] = []
    monkeypatch.setattr(ui_common.ui, "run_javascript", lambda code: captured.append(code))
    return captured


def _cookie_from_js(code: str) -> str:
    """Extract the cookie string from a ``document.cookie = "<...>"`` command."""
    prefix = "document.cookie = "
    assert code.startswith(prefix)
    assert code.endswith(";")
    return json.loads(code[len(prefix) : -1])


def _fake_https_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Context, "client", property(lambda self: _FakeClient("https")))


def test_set_token_cookie_http_has_max_age_no_secure(
    jwt_service: JwtService, captured_cookie_js: list[str]
) -> None:
    """Over plain HTTP: Max-Age matches the token TTL, no Secure flag."""
    token = jwt_service.create_token(user_id=1, username="alice", expires_minutes=30)
    set_token_cookie(token)

    cookie = _cookie_from_js(captured_cookie_js[0])
    assert cookie.startswith("token=")
    assert "SameSite=Lax" in cookie
    assert "Secure" not in cookie
    # Max-Age matches the JWT expiry (29..30 minutes remaining).
    max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert 29 * 60 <= max_age <= 30 * 60
    assert max_age == pytest.approx(token_max_age(token), abs=1)


def test_set_token_cookie_https_has_secure(
    jwt_service: JwtService,
    captured_cookie_js: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over HTTPS the cookie carries the Secure attribute."""
    _fake_https_client(monkeypatch)
    token = jwt_service.create_token(user_id=1, username="alice", expires_minutes=30)
    set_token_cookie(token)

    cookie = _cookie_from_js(captured_cookie_js[0])
    assert cookie.startswith("token=")
    assert "; Secure" in cookie


def test_clear_token_cookie_expires_immediately(captured_cookie_js: list[str]) -> None:
    """Logout clears the cookie with Max-Age=0 (over HTTP: no Secure)."""
    clear_token_cookie()
    cookie = _cookie_from_js(captured_cookie_js[0])
    assert cookie.startswith("token=")
    assert "Max-Age=0" in cookie
    assert "Secure" not in cookie


def test_clear_token_cookie_https_has_secure(
    captured_cookie_js: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_https_client(monkeypatch)
    clear_token_cookie()
    cookie = _cookie_from_js(captured_cookie_js[0])
    assert "Max-Age=0" in cookie
    assert "; Secure" in cookie


# =========================================================================== #
# 15. Publish before commit (Task 37)
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
            txn = Transaction(
                user_id=owner_id,
                bond_id=created.id,
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
# 16. PyJWT migration (Task 41)
# =========================================================================== #


def test_jwt_round_trip_claims(jwt_service: JwtService) -> None:
    token = jwt_service.create_token(user_id=42, username="carol")
    payload = jwt_service.verify_token(token)
    assert payload.user_id == 42
    assert payload.username == "carol"
    assert payload.expires_at > datetime.datetime.now(datetime.UTC)


async def test_api_rejects_expired_token(client: httpx.AsyncClient) -> None:
    jwt_service = JwtService(AuthConfig(jwt_secret=JWT_SECRET))
    expired = jwt_service.create_token(user_id=1, username="alice", expires_minutes=-1)
    response = await client.get("/api/bonds", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


async def test_api_rejects_tampered_token(client: httpx.AsyncClient) -> None:
    jwt_service = JwtService(AuthConfig(jwt_secret=JWT_SECRET))
    token = jwt_service.create_token(user_id=1, username="alice")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    response = await client.get("/api/bonds", headers={"Authorization": f"Bearer {tampered}"})
    assert response.status_code == 401


def test_jose_compatible_token_verifies() -> None:
    """A token produced by any RFC 7515 HS256 signer (e.g. python-jose, the
    pre-migration library) is a plain compact JWS and must verify.

    ``python-jose`` is not installed here, so the token is hand-built with
    ``hmac``/``hashlib`` — byte-for-byte the same format jose emits.
    """

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = b64url(
        json.dumps({"sub": "7", "username": "carol", "iat": now, "exp": now + 3600}).encode()
    )
    signature = b64url(
        hmac.new(JWT_SECRET.encode(), f"{header}.{claims}".encode(), hashlib.sha256).digest()
    )
    token = f"{header}.{claims}.{signature}"

    payload = JwtService(AuthConfig(jwt_secret=JWT_SECRET)).verify_token(token)
    assert payload.user_id == 7
    assert payload.username == "carol"


# =========================================================================== #
# 17/18. next_coupons / upcoming_cashflows parameters (Tasks 42, 43)
# =========================================================================== #


async def _portfolio_client_with_annual_bond(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> tuple[httpx.AsyncClient, dict, datetime.date, list[datetime.date]]:
    """Register alice, seed an annual bond matiring in ~9 years and a BUY of
    10 units; return ``(client, headers, maturity, coupon_dates)``.

    The bond matures on June 30 of ``today.year + 9`` so that (a) every coupon
    grid date is strictly in the future and (b) the maturity payment is always
    within the 3650-day default cashflows horizon, for any run date.
    """
    headers, user_id = await _register(client)
    today = datetime.date.today()
    maturity = datetime.date(today.year + 9, 6, 30)
    coupon_dates: list[datetime.date] = []
    year = today.year
    while True:
        coupon = datetime.date(year, 6, 30)
        if coupon > today:
            coupon_dates.append(coupon)
        if coupon == maturity:
            break
        year += 1
    assert coupon_dates

    async with session_factory() as session:
        bond = Bond(
            isin="RU000A0JY2U8",
            name="Coupon grid bond",
            nominal=1000,
            coupon_rate=7.0,
            coupon_frequency="ANNUAL",
            maturity_date=maturity,
            owner_id=user_id,
        )
        session.add(bond)
        await session.flush()
        session.add(
            Transaction(
                user_id=user_id,
                bond_id=bond.id,
                type="BUY",
                quantity=10,
                price=1000.0,
                date=datetime.date(2026, 1, 10),
            )
        )
        await session.commit()
    return client, headers, maturity, coupon_dates


async def test_next_coupons_horizon_and_limit(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, headers, _, coupon_dates = await _portfolio_client_with_annual_bond(client, session_factory)
    today = datetime.date.today()

    # Default: 730-day horizon, at most 100 events.
    response = await client.get("/api/portfolio", headers=headers)
    assert response.status_code == 200, response.text
    expected_default = [d for d in coupon_dates if (d - today).days <= 730][:100]
    got = [datetime.date.fromisoformat(d["date"]) for d in response.json()["next_coupons"]]
    assert got == expected_default
    assert all(item["amount"] == pytest.approx(700.0) for item in response.json()["next_coupons"])

    # Explicit horizon + limit: coupons within 365 days, capped at 5.
    response = await client.get(
        "/api/portfolio",
        params={"next_coupons_horizon_days": 365, "next_coupons_limit": 5},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    expected = [d for d in coupon_dates if (d - today).days <= 365][:5]
    got = [datetime.date.fromisoformat(d["date"]) for d in response.json()["next_coupons"]]
    assert got == expected

    # The limit alone caps the (default-horizon) list.
    response = await client.get("/api/portfolio", params={"next_coupons_limit": 1}, headers=headers)
    assert response.status_code == 200, response.text
    got = [datetime.date.fromisoformat(d["date"]) for d in response.json()["next_coupons"]]
    assert got == expected_default[:1]


@pytest.mark.parametrize(
    "params",
    [
        {"next_coupons_horizon_days": 0},
        {"next_coupons_limit": 0},
        {"next_coupons_horizon_days": 3651},
        {"next_coupons_limit": 1001},
    ],
)
async def test_next_coupons_invalid_params_rejected(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    params: dict,
) -> None:
    _, headers, _, _ = await _portfolio_client_with_annual_bond(client, session_factory)
    response = await client.get("/api/portfolio", params=params, headers=headers)
    assert response.status_code == 422, response.text


async def test_cashflows_horizon_and_limit(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, headers, maturity, coupon_dates = await _portfolio_client_with_annual_bond(
        client, session_factory
    )
    today = datetime.date.today()

    # Default: 3650-day horizon, at most 500 events -> every coupon plus the
    # maturity repayment (the bond matures well inside the horizon).
    response = await client.get("/api/portfolio", headers=headers)
    assert response.status_code == 200, response.text
    expected_flows = [(d, "COUPON") for d in coupon_dates] + [(maturity, "MATURITY")]
    got_flows = [
        (datetime.date.fromisoformat(f["date"]), f["kind"])
        for f in response.json()["upcoming_cashflows"]
    ]
    assert got_flows == expected_flows[:500]

    # Explicit horizon + limit.
    response = await client.get(
        "/api/portfolio",
        params={"cashflows_horizon_days": 365, "cashflows_limit": 5},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    expected = [d for d in coupon_dates if (d - today).days <= 365][:5]
    got = [datetime.date.fromisoformat(f["date"]) for f in response.json()["upcoming_cashflows"]]
    assert got == expected

    # The limit alone caps the (default-horizon) list.
    response = await client.get("/api/portfolio", params={"cashflows_limit": 2}, headers=headers)
    assert response.status_code == 200, response.text
    got = [datetime.date.fromisoformat(f["date"]) for f in response.json()["upcoming_cashflows"]]
    assert got == [f[0] for f in expected_flows[:2]]


@pytest.mark.parametrize(
    "params",
    [
        {"cashflows_horizon_days": 0},
        {"cashflows_limit": 0},
        {"cashflows_horizon_days": 7301},
        {"cashflows_limit": 5001},
    ],
)
async def test_cashflows_invalid_params_rejected(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    params: dict,
) -> None:
    _, headers, _, _ = await _portfolio_client_with_annual_bond(client, session_factory)
    response = await client.get("/api/portfolio", params=params, headers=headers)
    assert response.status_code == 422, response.text


# =========================================================================== #
# 19. avg_buy_price open-position-only (Task 44)
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

    def txn(type_: TransactionType, quantity: int, price: float, day: int) -> TransactionCreate:
        return TransactionCreate(
            bond_id=bond_id,
            type=type_,
            quantity=quantity,
            price=price,
            date=datetime.date(2026, 1, day),
        )

    # Scenario 1: running average across a partial close.
    uid = users["running"]
    await portfolio_service.add_transaction(uid, txn("BUY", 10, 100.0, 1))
    await portfolio_service.add_transaction(uid, txn("SELL", 5, 150.0, 10))
    await portfolio_service.add_transaction(uid, txn("BUY", 5, 110.0, 20))
    summary = await analytics_service.get_portfolio_summary(uid, today=today)
    assert len(summary.positions) == 1
    assert summary.positions[0].quantity == 10
    assert summary.positions[0].avg_buy_price == pytest.approx(105.0)
    assert summary.positions[0].total_invested == pytest.approx(1050.0)

    # Scenario 2: flat position -> no open position at all.
    uid = users["flat"]
    await portfolio_service.add_transaction(uid, txn("BUY", 10, 100.0, 1))
    await portfolio_service.add_transaction(uid, txn("SELL", 10, 105.0, 10))
    summary = await analytics_service.get_portfolio_summary(uid, today=today)
    assert summary.positions == []
    assert summary.total_invested == 0.0

    # Scenario 3: average resets after a full close.
    uid = users["reset"]
    await portfolio_service.add_transaction(uid, txn("BUY", 10, 100.0, 1))
    await portfolio_service.add_transaction(uid, txn("SELL", 10, 105.0, 10))
    await portfolio_service.add_transaction(uid, txn("BUY", 5, 200.0, 20))
    summary = await analytics_service.get_portfolio_summary(uid, today=today)
    assert len(summary.positions) == 1
    assert summary.positions[0].quantity == 5
    assert summary.positions[0].avg_buy_price == pytest.approx(200.0)
    assert summary.positions[0].total_invested == pytest.approx(1000.0)
