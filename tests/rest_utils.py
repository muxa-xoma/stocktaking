"""Shared REST-test payloads/helpers.

Single definition of the constants and httpx helper functions used by both
the ``tests/api`` and ``tests/functional`` layers. Import this module as
``tests.rest_utils``; ``tests/functional/_functional_utils.py`` re-exports
the same names for backwards compatibility. No side effects at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

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


async def _create_bond(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    """Create the default bond; returns the created bond JSON."""
    response = await client.post("/api/bonds", json=BOND_PAYLOAD, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


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
