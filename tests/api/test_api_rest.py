"""Black-box REST API tests for the 12 endpoints under ``/api``.

The fixture stack (``migrated_db_url`` / ``session_factory`` / ``event_bus``
/ ``app`` / ``client``) lives in ``tests/conftest.py``: the application is
assembled exactly like in ``main.py``, with real services on a migrated
temporary SQLite database and a running ``AsyncQueueEventBus``. Requests go
through ``httpx``'s ``ASGITransport`` — no server socket is opened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.rest_utils import (
    BOND_PAYLOAD,
    PASSWORD,
    USERNAME,
    _create_bond,
    _register_and_login,
)

if TYPE_CHECKING:
    import httpx


# --------------------------------------------------------------------- #
# auth endpoints


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
