"""Black-box REST API tests for the 12 endpoints under ``/api``.

The application is assembled exactly like in ``main.py`` (but on a plain
``FastAPI()`` instance instead of ``nicegui.app``): the router is included,
domain exception handlers are registered, and real services on a migrated
temporary SQLite database plus a running ``AsyncQueueEventBus`` are wired in
through ``build_api_dependencies``. Requests go through ``httpx``'s
``ASGITransport`` — no server socket is opened.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from bond_accounting.analytics import AnalyticsService
from bond_accounting.api import api_router, build_api_dependencies, register_exception_handlers
from bond_accounting.auth import AuthService, JwtService, PasswordHasher
from bond_accounting.bonds.service import BondService
from bond_accounting.config.settings import AuthConfig, DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.event_bus import AsyncQueueEventBus
from bond_accounting.portfolio import PortfolioService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Project root (where ``alembic.ini`` lives).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

USERNAME = "alice"
PASSWORD = "correct-horse-battery"

#: Valid bond payload used across the CRUD tests.
BOND_PAYLOAD = {
    "isin": "RU000A0JX0J2",
    "name": "OFLZ 2030",
    "nominal": 1000,
    "coupon_rate": 7.0,
    "coupon_frequency": "ANNUAL",
    "maturity_date": "2030-01-01",
}


# --------------------------------------------------------------------- #
# fixtures


@pytest.fixture
def migrated_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create an empty migrated SQLite DB via alembic; return its file path."""
    db_path = tmp_path / "api_test.db"
    monkeypatch.setenv("BOND_DATABASE__SQLITE_PATH", str(db_path))
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", "test-secret")
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
def app(
    session_factory: async_sessionmaker[AsyncSession], event_bus: AsyncQueueEventBus
) -> FastAPI:
    """The API application assembled like in ``main.py``, with real services."""
    jwt_service = JwtService(AuthConfig(jwt_secret="test-secret"))
    auth_service = AuthService(session_factory, PasswordHasher(), jwt_service)
    bond_service = BondService(session_factory, event_bus)
    portfolio_service = PortfolioService(session_factory, event_bus)
    analytics_service = AnalyticsService(session_factory, event_bus)

    application = FastAPI()
    application.include_router(api_router)
    register_exception_handlers(application)
    application.dependency_overrides.update(
        build_api_dependencies(
            auth_service,
            jwt_service,
            bond_service,
            portfolio_service,
            analytics_service,
        ).overrides()
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An httpx client talking to the app in-process, without a socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


async def _register_and_login(
    client: httpx.AsyncClient, username: str = USERNAME
) -> dict[str, str]:
    """Register a user, log in and return the bearer-auth headers."""
    response = await client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _create_bond(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    """Create the default bond; returns the created bond JSON."""
    response = await client.post("/api/bonds", json=BOND_PAYLOAD, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- #
# auth endpoints


async def test_register_and_login_happy_path(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["username"] == USERNAME
    assert isinstance(body["id"], int)

    response = await client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 200
    assert isinstance(response.json()["token"], str)
    assert response.json()["token"]


async def test_register_taken_username_conflict(client: httpx.AsyncClient) -> None:
    await _register_and_login(client)
    response = await client.post(
        "/api/auth/register", json={"username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 409
    assert "detail" in response.json()


async def test_login_wrong_password_unauthorized(client: httpx.AsyncClient) -> None:
    await _register_and_login(client)
    response = await client.post(
        "/api/auth/login", json={"username": USERNAME, "password": "wrong-password"}
    )
    assert response.status_code == 401


async def test_login_unknown_user_unauthorized(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "ghost-user", "password": PASSWORD}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------- #
# F1: password length is limited to bcrypt's 72-byte maximum. A longer
# password must be rejected with 400 (not a 500 from inside bcrypt).


async def test_register_password_over_72_bytes_bad_request(client: httpx.AsyncClient) -> None:
    """A password longer than 72 bytes is rejected with 400, not 500."""
    response = await client.post(
        "/api/auth/register",
        json={"username": "long-pw-user", "password": "a" * 80},
    )
    assert response.status_code == 400
    assert "detail" in response.json()


async def test_register_password_exactly_72_bytes_created(client: httpx.AsyncClient) -> None:
    """A password of exactly 72 bytes is accepted (boundary of the limit)."""
    response = await client.post(
        "/api/auth/register",
        json={"username": "exact-72-user", "password": "a" * 72},
    )
    assert response.status_code == 201
    assert response.json()["username"] == "exact-72-user"

    # The boundary password must also work for login.
    response = await client.post(
        "/api/auth/login",
        json={"username": "exact-72-user", "password": "a" * 72},
    )
    assert response.status_code == 200


# --------------------------------------------------------------------- #
# bearer-auth protection


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/bonds"),
        ("POST", "/api/bonds"),
        ("GET", "/api/bonds/1"),
        ("PUT", "/api/bonds/1"),
        ("DELETE", "/api/bonds/1"),
        ("GET", "/api/transactions"),
        ("POST", "/api/transactions"),
        ("GET", "/api/portfolio"),
        ("GET", "/api/portfolio/positions"),
        ("GET", "/api/bonds/1/yield"),
    ],
)
async def test_endpoints_without_token_are_unauthorized(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 401


async def test_endpoints_with_garbage_token_are_unauthorized(client: httpx.AsyncClient) -> None:
    headers = {"Authorization": "Bearer not-a-jwt"}
    response = await client.get("/api/bonds", headers=headers)
    assert response.status_code == 401


# --------------------------------------------------------------------- #
# bond CRUD


async def test_bond_crud_flow(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)

    # CREATE
    created = await _create_bond(client, headers)
    assert created["isin"] == BOND_PAYLOAD["isin"]
    assert created["name"] == BOND_PAYLOAD["name"]
    assert created["nominal"] == BOND_PAYLOAD["nominal"]
    assert created["coupon_rate"] == BOND_PAYLOAD["coupon_rate"]
    assert created["maturity_date"] == BOND_PAYLOAD["maturity_date"]
    bond_id = created["id"]

    # LIST
    response = await client.get("/api/bonds", headers=headers)
    assert response.status_code == 200
    bonds = response.json()
    assert [bond["id"] for bond in bonds] == [bond_id]

    # GET one
    response = await client.get(f"/api/bonds/{bond_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["isin"] == BOND_PAYLOAD["isin"]

    # UPDATE
    response = await client.put(
        f"/api/bonds/{bond_id}", json={"name": "OFLZ 2030 renamed"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["name"] == "OFLZ 2030 renamed"
    assert response.json()["isin"] == BOND_PAYLOAD["isin"]  # untouched field

    # DELETE
    response = await client.delete(f"/api/bonds/{bond_id}", headers=headers)
    assert response.status_code == 204
    response = await client.get(f"/api/bonds/{bond_id}", headers=headers)
    assert response.status_code == 404


async def test_get_nonexistent_bond_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.get("/api/bonds/999999", headers=headers)
    assert response.status_code == 404


async def test_update_nonexistent_bond_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.put("/api/bonds/999999", json={"name": "no such bond"}, headers=headers)
    assert response.status_code == 404


async def test_delete_nonexistent_bond_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.delete("/api/bonds/999999", headers=headers)
    assert response.status_code == 404


# --------------------------------------------------------------------- #
# F2: a bond with at least one transaction must not be deletable.


async def _create_transaction(
    client: httpx.AsyncClient, headers: dict[str, str], bond_id: int
) -> dict:
    """Create a minimal BUY transaction; returns the created transaction JSON."""
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond_id,
            "type": "BUY",
            "quantity": 1,
            "price": 1000.0,
            "date": "2026-01-10",
            "commission": 0.0,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_delete_bond_with_transactions_conflict(client: httpx.AsyncClient) -> None:
    """Deleting a bond that has transactions fails with 409 and keeps the bond."""
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    await _create_transaction(client, headers, bond["id"])

    response = await client.delete(f"/api/bonds/{bond['id']}", headers=headers)
    assert response.status_code == 409
    assert "detail" in response.json()

    # The bond is still there and still lists its transaction.
    response = await client.get(f"/api/bonds/{bond['id']}", headers=headers)
    assert response.status_code == 200
    response = await client.get(
        "/api/transactions", params={"bond_id": bond["id"]}, headers=headers
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_delete_bond_without_transactions_no_content(client: httpx.AsyncClient) -> None:
    """Deleting a bond without transactions succeeds with 204."""
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)

    response = await client.delete(f"/api/bonds/{bond['id']}", headers=headers)
    assert response.status_code == 204

    response = await client.get(f"/api/bonds/{bond['id']}", headers=headers)
    assert response.status_code == 404


async def test_create_bond_duplicate_isin_conflict(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    await _create_bond(client, headers)

    response = await client.post("/api/bonds", json=BOND_PAYLOAD, headers=headers)
    assert response.status_code == 409
    assert "detail" in response.json()


async def test_create_bond_invalid_isin_unprocessable(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    invalid = {**BOND_PAYLOAD, "isin": "not-an-isin"}
    response = await client.post("/api/bonds", json=invalid, headers=headers)
    assert response.status_code == 422


async def test_create_bond_invalid_frequency_unprocessable(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    invalid = {**BOND_PAYLOAD, "coupon_frequency": "MONTHLY"}
    response = await client.post("/api/bonds", json=invalid, headers=headers)
    assert response.status_code == 422


# --------------------------------------------------------------------- #
# transactions


async def test_transactions_and_portfolio_flow(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    bond_id = bond["id"]

    # BUY 10 @ 1000, commission 5.
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond_id,
            "type": "BUY",
            "quantity": 10,
            "price": 1000.0,
            "date": "2026-01-10",
            "commission": 5.0,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    buy = response.json()
    assert buy["type"] == "BUY"
    assert buy["quantity"] == 10

    # SELL 4 @ 1050, commission 5.
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond_id,
            "type": "SELL",
            "quantity": 4,
            "price": 1050.0,
            "date": "2026-01-15",
            "commission": 5.0,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    # LIST transactions (with and without the bond filter).
    response = await client.get("/api/transactions", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 2

    response = await client.get("/api/transactions", params={"bond_id": bond_id}, headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 2

    response = await client.get("/api/transactions", params={"bond_id": 999999}, headers=headers)
    assert response.status_code == 200
    assert response.json() == []

    # PORTFOLIO summary: one open position, realized PnL 195.
    response = await client.get("/api/portfolio", headers=headers)
    assert response.status_code == 200
    summary = response.json()
    assert len(summary["positions"]) == 1
    assert summary["positions"][0]["quantity"] == 6
    assert summary["positions"][0]["avg_buy_price"] == pytest.approx(1000.0)
    assert summary["total_invested"] == pytest.approx(6000.0)
    assert summary["realized_pnl"]["sells"] == pytest.approx(200.0)
    assert summary["realized_pnl"]["commissions"] == pytest.approx(5.0)
    assert summary["realized_pnl"]["total"] == pytest.approx(195.0)
    assert summary["next_coupons"]

    # POSITIONS.
    response = await client.get("/api/portfolio/positions", headers=headers)
    assert response.status_code == 200
    positions = response.json()
    assert len(positions) == 1
    assert positions[0]["quantity"] == 6
    assert positions[0]["avg_buy_price"] == pytest.approx(1000.0)

    # YIELD analytics for the position.
    response = await client.get(f"/api/bonds/{bond_id}/yield", headers=headers)
    assert response.status_code == 200
    yield_body = response.json()
    assert yield_body["quantity"] == 6
    assert yield_body["current_yield"] == pytest.approx(0.07)
    assert yield_body["next_coupon_date"]


async def test_sell_more_than_position_unprocessable(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)

    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond["id"],
            "type": "SELL",
            "quantity": 999,
            "price": 1050.0,
            "date": "2026-01-10",
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert "detail" in response.json()


async def test_yield_without_open_position_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    response = await client.get(f"/api/bonds/{bond['id']}/yield", headers=headers)
    assert response.status_code == 404


async def test_transaction_non_positive_quantity_unprocessable(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond["id"],
            "type": "BUY",
            "quantity": 0,
            "price": 1000.0,
            "date": "2026-01-10",
        },
        headers=headers,
    )
    assert response.status_code == 422


# --------------------------------------------------------------------- #
# fix-loop regressions (Tasks 50-52): average-cost realized PnL,
# position valuation via avg_buy_price, YTM nominal-relative bounds


async def test_portfolio_realized_sells_reset_after_full_close(client: httpx.AsyncClient) -> None:
    """BUY 10@100, SELL 10@120, BUY 5@200, SELL 5@210 -> realized sells = 250.

    The exit after the reopen must be priced against the post-reopen average
    (200), not blended with the pre-close history.
    """
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    bond_id = bond["id"]
    trades: list[tuple[str, int, float, str]] = [
        ("BUY", 10, 100.0, "2026-01-10"),
        ("SELL", 10, 120.0, "2026-01-15"),
        ("BUY", 5, 200.0, "2026-01-20"),
        ("SELL", 5, 210.0, "2026-01-25"),
    ]
    for type_, quantity, price, day in trades:
        response = await client.post(
            "/api/transactions",
            json={
                "bond_id": bond_id,
                "type": type_,
                "quantity": quantity,
                "price": price,
                "date": day,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text

    response = await client.get("/api/portfolio", headers=headers)
    assert response.status_code == 200
    summary = response.json()
    assert summary["realized_pnl"]["sells"] == pytest.approx(250.0)
    assert summary["realized_pnl"]["maturities"] == pytest.approx(0.0)
    assert summary["realized_pnl"]["commissions"] == pytest.approx(0.0)


@pytest.mark.parametrize("price", [-5.0, 0.0])
async def test_transaction_non_positive_price_unprocessable(
    client: httpx.AsyncClient, price: float
) -> None:
    """A non-positive price is a data-entry error: HTTP 422."""
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond["id"],
            "type": "BUY",
            "quantity": 1,
            "price": price,
            "date": "2026-01-10",
        },
        headers=headers,
    )
    assert response.status_code == 422


async def test_transaction_positive_fractional_price_created(client: httpx.AsyncClient) -> None:
    """Boundary sanity: any strictly positive price passes validation."""
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond["id"],
            "type": "BUY",
            "quantity": 1,
            "price": 100.5,
            "date": "2026-01-10",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["price"] == pytest.approx(100.5)


async def test_portfolio_yields_with_large_nominal_are_not_none(
    client: httpx.AsyncClient,
) -> None:
    """A bond with nominal=10000 bought at par (10000) yields valid analytics.

    Regression for the old absolute price cap (9999): a par price above it
    must still produce a non-None ytm/current_yield.
    """
    headers = await _register_and_login(client)
    payload = {**BOND_PAYLOAD, "isin": "RU000A0JX5W1", "nominal": 10000}
    response = await client.post("/api/bonds", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    bond_id = response.json()["id"]

    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond_id,
            "type": "BUY",
            "quantity": 5,
            "price": 10000.0,
            "date": "2026-01-10",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    response = await client.get("/api/portfolio", headers=headers)
    assert response.status_code == 200
    positions = response.json()["positions"]
    assert len(positions) == 1
    assert positions[0]["ytm"] is not None
    assert positions[0]["current_yield"] is not None
