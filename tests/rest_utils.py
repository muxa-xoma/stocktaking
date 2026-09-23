"""Shared REST-test payloads/helpers.

Single definition of the constants and httpx helper functions used by both
the ``tests/api`` and ``tests/functional`` layers. Import this module as
``tests.rest_utils``; ``tests/functional/_functional_utils.py`` re-exports
the same names for backwards compatibility. No side effects at import time.
"""

from __future__ import annotations

import uuid
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
    "coupon_period_days": 182,
    "maturity_date": "2030-01-01",
}

#: Valid broker payload; commission is a raw percent (0.3 == 0.3%).
BROKER_PAYLOAD = {
    "name": "Test Broker",
    "commission": 0.3,
    "min_commission": None,
    "description": None,
}

#: Valid broker-account payload (broker_id is injected per call).
ACCOUNT_PAYLOAD = {
    "name": "Main account",
    "account_number": "AB-001",
    "account_type": "STANDARD",
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


async def _create_broker(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides: object
) -> dict:
    """Create a broker with a unique name; returns the created broker JSON.

    ``name`` defaults to a uuid-suffixed value so repeated calls inside one
    test never collide on the unique broker name.
    """
    payload: dict[str, object] = {
        **BROKER_PAYLOAD,
        "name": f"Test Broker {uuid.uuid4().hex[:8]}",
        **overrides,
    }
    response = await client.post("/api/brokers", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_account(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    broker_id: int,
    **overrides: object,
) -> dict:
    """Create a broker account; returns the created account JSON."""
    payload: dict[str, object] = {**ACCOUNT_PAYLOAD, "broker_id": broker_id, **overrides}
    response = await client.post("/api/broker-accounts", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_account_for_user(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides: object
) -> dict:
    """Create a broker + an account for the caller; returns the account JSON.

    Convenience for transaction tests that only need a valid
    ``broker_account_id`` owned by the authenticated user.
    """
    broker = await _create_broker(client, headers)
    return await _create_account(client, headers, broker["id"], **overrides)


async def _create_transaction(
    client: httpx.AsyncClient, headers: dict[str, str], bond_id: int
) -> dict:
    """Create a minimal BUY transaction; returns the created transaction JSON.

    A broker + broker account are created on the fly: ``TransactionCreate``
    requires a ``broker_account_id`` owned by the calling user.
    """
    account = await _create_account_for_user(client, headers)
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond_id,
            "broker_account_id": account["id"],
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
        "coupon_period_days": 182,
        "maturity_date": "2030-01-01",
    }
    payload.update(overrides)
    return payload
