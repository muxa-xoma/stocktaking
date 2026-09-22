"""Black-box REST tests for the 5 ``/api/account-operations`` endpoints.

The fixture stack (migrated temp SQLite + real services + ``client``) lives in
``tests/conftest.py``; the ``app`` fixture wires the real
``AccountOperationService`` as the 7th dependency of
``build_api_dependencies``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.rest_utils import (
    PASSWORD,
    _create_account,
    _create_account_for_user,
    _create_broker,
    _register,
)

if TYPE_CHECKING:
    import httpx


async def _login(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    """Log in an already registered user; return bearer headers."""
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _operation_payload(broker_account_id: int, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "broker_account_id": broker_account_id,
        "type": "DEPOSIT",
        "amount": 1000.0,
        "date": "2026-01-15",
        "note": None,
    }
    payload.update(overrides)
    return payload


async def _create_operation(
    client: httpx.AsyncClient, headers: dict[str, str], broker_account_id: int
) -> dict:
    response = await client.post(
        "/api/account-operations",
        json=_operation_payload(broker_account_id),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- #
# auth


async def test_endpoints_require_token(client: httpx.AsyncClient) -> None:
    """All 5 endpoints answer 401 without a bearer token."""
    for method, url in [
        ("GET", "/api/account-operations"),
        ("POST", "/api/account-operations"),
        ("GET", "/api/account-operations/1"),
        ("PUT", "/api/account-operations/1"),
        ("DELETE", "/api/account-operations/1"),
    ]:
        response = await client.request(method, url)
        assert response.status_code == 401, (method, url, response.status_code)


async def test_invalid_token_rejected(client: httpx.AsyncClient) -> None:
    """A garbage bearer token is rejected with 401."""
    response = await client.get(
        "/api/account-operations", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------- #
# create / list / get


async def test_create_and_list_for_user(client: httpx.AsyncClient) -> None:
    """A created operation is visible in the caller's listing."""
    headers, _ = await _register(client)
    account = await _create_account_for_user(client, headers)

    created = await _create_operation(client, headers, account["id"])

    assert created["broker_account_id"] == account["id"]
    assert created["type"] == "DEPOSIT"
    assert created["amount"] == 1000.0
    assert created["date"] == "2026-01-15"

    response = await client.get("/api/account-operations", headers=headers)
    assert response.status_code == 200
    listed = response.json()
    assert [op["id"] for op in listed] == [created["id"]]


async def test_list_filtered_by_broker_account(client: httpx.AsyncClient) -> None:
    """``?broker_account_id=`` restricts the listing to one account."""
    headers, _ = await _register(client)
    broker = await _create_broker(client, headers)

    account_a = await _create_account(client, headers, broker["id"], name="A")
    account_b = await _create_account(client, headers, broker["id"], name="B")

    await _create_operation(client, headers, account_a["id"])
    await _create_operation(client, headers, account_b["id"])

    response = await client.get(
        "/api/account-operations", params={"broker_account_id": account_b["id"]}, headers=headers
    )
    assert response.status_code == 200
    listed = response.json()
    assert len(listed) == 1
    assert listed[0]["broker_account_id"] == account_b["id"]


async def test_get_operation(client: httpx.AsyncClient) -> None:
    """GET by id returns the operation."""
    headers, _ = await _register(client)
    account = await _create_account_for_user(client, headers)
    created = await _create_operation(client, headers, account["id"])

    response = await client.get(f"/api/account-operations/{created['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_missing_operation_404(client: httpx.AsyncClient) -> None:
    """An unknown operation id answers 404."""
    headers, _ = await _register(client)
    response = await client.get("/api/account-operations/999999", headers=headers)
    assert response.status_code == 404


# --------------------------------------------------------------------- #
# validation


async def test_create_invalid_amount_422(client: httpx.AsyncClient) -> None:
    """A non-positive amount is rejected with 422."""
    headers, _ = await _register(client)
    account = await _create_account_for_user(client, headers)

    for amount in (0, -100.0):
        response = await client.post(
            "/api/account-operations",
            json=_operation_payload(account["id"], amount=amount),
            headers=headers,
        )
        assert response.status_code == 422, response.text


async def test_create_invalid_type_422(client: httpx.AsyncClient) -> None:
    """An unknown operation type is rejected with 422."""
    headers, _ = await _register(client)
    account = await _create_account_for_user(client, headers)

    response = await client.post(
        "/api/account-operations",
        json=_operation_payload(account["id"], type="TRANSFER"),
        headers=headers,
    )
    assert response.status_code == 422, response.text


async def test_create_missing_fields_422(client: httpx.AsyncClient) -> None:
    """A payload without the required fields is rejected with 422."""
    headers, _ = await _register(client)
    response = await client.post("/api/account-operations", json={"type": "TAX"}, headers=headers)
    assert response.status_code == 422


async def test_create_nonexistent_account_404(client: httpx.AsyncClient) -> None:
    """An operation on a nonexistent broker account answers 404."""
    headers, _ = await _register(client)
    response = await client.post(
        "/api/account-operations", json=_operation_payload(999999), headers=headers
    )
    assert response.status_code == 404
    assert "detail" in response.json()


async def test_create_on_foreign_account_404(client: httpx.AsyncClient) -> None:
    """An operation on another user's account answers 404 (not 403)."""
    headers_a, _ = await _register(client, "alice")
    headers_b, _ = await _register(client, "bob")
    account_of_b = await _create_account_for_user(client, headers_b)

    response = await client.post(
        "/api/account-operations",
        json=_operation_payload(account_of_b["id"]),
        headers=headers_a,
    )
    assert response.status_code == 404
    assert "detail" in response.json()


# --------------------------------------------------------------------- #
# update / delete


async def test_update_partial_fields(client: httpx.AsyncClient) -> None:
    """PUT applies only the provided fields."""
    headers, _ = await _register(client)
    account = await _create_account_for_user(client, headers)
    created = await _create_operation(client, headers, account["id"])

    response = await client.put(
        f"/api/account-operations/{created['id']}",
        json={"amount": 2500.0, "type": "WITHDRAWAL", "note": "перевод"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["amount"] == 2500.0
    assert updated["type"] == "WITHDRAWAL"
    assert updated["note"] == "перевод"
    # Unprovided fields are unchanged.
    assert updated["date"] == created["date"]
    assert updated["broker_account_id"] == account["id"]


async def test_update_missing_404(client: httpx.AsyncClient) -> None:
    """PUT on an unknown id answers 404."""
    headers, _ = await _register(client)
    response = await client.put(
        "/api/account-operations/999999", json={"amount": 1.0}, headers=headers
    )
    assert response.status_code == 404


async def test_delete_operation(client: httpx.AsyncClient) -> None:
    """DELETE removes the operation (204, then 404)."""
    headers, _ = await _register(client)
    account = await _create_account_for_user(client, headers)
    created = await _create_operation(client, headers, account["id"])

    response = await client.delete(f"/api/account-operations/{created['id']}", headers=headers)
    assert response.status_code == 204

    response = await client.get(f"/api/account-operations/{created['id']}", headers=headers)
    assert response.status_code == 404


async def test_delete_missing_404(client: httpx.AsyncClient) -> None:
    """DELETE on an unknown id answers 404."""
    headers, _ = await _register(client)
    response = await client.delete("/api/account-operations/999999", headers=headers)
    assert response.status_code == 404


# --------------------------------------------------------------------- #
# ownership


async def _second_user_with_operation(
    client: httpx.AsyncClient,
) -> tuple[dict[str, str], dict]:
    """Register alice+bob; return bob's headers and alice's operation."""
    headers_alice, _ = await _register(client, "alice")
    headers_bob, _ = await _register(client, "bob")
    account = await _create_account_for_user(client, headers_alice)
    created = await _create_operation(client, headers_alice, account["id"])
    return headers_bob, created


async def test_get_other_users_operation_403(client: httpx.AsyncClient) -> None:
    """Another user's operation answers 403 on GET."""
    headers_bob, operation = await _second_user_with_operation(client)

    response = await client.get(f"/api/account-operations/{operation['id']}", headers=headers_bob)
    assert response.status_code == 403
    assert "detail" in response.json()


async def test_update_other_users_operation_403(client: httpx.AsyncClient) -> None:
    """Another user's operation answers 403 on PUT and stays untouched."""
    headers_bob, operation = await _second_user_with_operation(client)

    response = await client.put(
        f"/api/account-operations/{operation['id']}",
        json={"amount": 1.0},
        headers=headers_bob,
    )
    assert response.status_code == 403

    # The operation survived the forbidden update.
    headers_alice = await _login(client, "alice")
    response = await client.get(f"/api/account-operations/{operation['id']}", headers=headers_alice)
    assert response.status_code == 200
    assert response.json()["amount"] == 1000.0


async def test_delete_other_users_operation_403(client: httpx.AsyncClient) -> None:
    """Another user's operation answers 403 on DELETE and stays alive."""
    headers_bob, operation = await _second_user_with_operation(client)

    response = await client.delete(
        f"/api/account-operations/{operation['id']}", headers=headers_bob
    )
    assert response.status_code == 403

    headers_alice = await _login(client, "alice")
    response = await client.get(f"/api/account-operations/{operation['id']}", headers=headers_alice)
    assert response.status_code == 200
