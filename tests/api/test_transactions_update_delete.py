"""Black-box REST tests for GET/PUT/DELETE ``/api/transactions/{id}``.

Position-invariant coverage (new in T05-fix): an update or delete that would
drive a position below zero is rejected; ownership is enforced (403 for
another user's transaction, 404 for a missing one); PUT applies only the
provided fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.rest_utils import (
    _create_account_for_user,
    _create_bond,
    _register,
    _register_and_login,
)

if TYPE_CHECKING:
    import httpx


def _txn_payload(bond_id: int, account_id: int, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "bond_id": bond_id,
        "broker_account_id": account_id,
        "type": "BUY",
        "quantity": 10,
        "price": 980.5,
        "date": "2026-01-10",
        "commission": 15.0,
    }
    payload.update(overrides)
    return payload


async def _buy(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    bond_id: int,
    account_id: int,
    **overrides: object,
) -> dict:
    response = await client.post(
        "/api/transactions", json=_txn_payload(bond_id, account_id, **overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _sell(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    bond_id: int,
    account_id: int,
    **overrides: object,
) -> dict:
    response = await client.post(
        "/api/transactions",
        json=_txn_payload(bond_id, account_id, type="SELL", quantity=5, **overrides),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _setup_pair(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[int, int, dict, dict]:
    """Create a bond + account, a BUY of 10 and a SELL of 5 (position 5)."""
    bond = await _create_bond(client, headers)
    account = await _create_account_for_user(client, headers)
    buy = await _buy(client, headers, bond["id"], account["id"])
    sell = await _sell(client, headers, bond["id"], account["id"])
    return bond["id"], account["id"], buy, sell


# --------------------------------------------------------------------- #
# GET


async def test_get_transaction(client: httpx.AsyncClient) -> None:
    """GET by id returns the transaction."""
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    account = await _create_account_for_user(client, headers)
    created = await _buy(client, headers, bond["id"], account["id"])

    response = await client.get(f"/api/transactions/{created['id']}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["quantity"] == 10
    assert body["price"] == 980.5


async def test_get_missing_transaction_404(client: httpx.AsyncClient) -> None:
    """An unknown transaction id answers 404."""
    headers = await _register_and_login(client)
    response = await client.get("/api/transactions/999999", headers=headers)
    assert response.status_code == 404


async def test_get_other_users_transaction_403(client: httpx.AsyncClient) -> None:
    """Another user's transaction answers 403 (TransactionForbiddenError)."""
    headers_alice, _ = await _register(client, "alice")
    bond = await _create_bond(client, headers_alice)
    account = await _create_account_for_user(client, headers_alice)
    created = await _buy(client, headers_alice, bond["id"], account["id"])

    headers_bob, _ = await _register(client, "bob")
    response = await client.get(f"/api/transactions/{created['id']}", headers=headers_bob)
    assert response.status_code == 403
    assert "detail" in response.json()


# --------------------------------------------------------------------- #
# PUT


async def test_update_partial_fields_unchanged_others(client: httpx.AsyncClient) -> None:
    """PUT applies only provided fields; the rest stay untouched."""
    headers = await _register_and_login(client)
    bond_id, account_id, buy, _ = await _setup_pair(client, headers)

    response = await client.put(
        f"/api/transactions/{buy['id']}",
        json={"price": 995.0, "commission": 20.0},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["price"] == 995.0
    assert updated["commission"] == 20.0
    # Unprovided fields are unchanged.
    assert updated["quantity"] == 10
    assert updated["type"] == "BUY"
    assert updated["bond_id"] == bond_id
    assert updated["broker_account_id"] == account_id
    assert updated["date"] == buy["date"]


async def test_update_type_and_date(client: httpx.AsyncClient) -> None:
    """Type and date are updatable within the position invariant."""
    headers = await _register_and_login(client)
    _, _, buy, _ = await _setup_pair(client, headers)

    response = await client.put(
        f"/api/transactions/{buy['id']}",
        json={"date": "2026-01-05"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["date"] == "2026-01-05"


async def test_update_missing_transaction_404(client: httpx.AsyncClient) -> None:
    """PUT on an unknown id answers 404."""
    headers = await _register_and_login(client)
    response = await client.put("/api/transactions/999999", json={"price": 1.0}, headers=headers)
    assert response.status_code == 404


async def test_update_other_users_transaction_403(client: httpx.AsyncClient) -> None:
    """Another user's transaction answers 403 on PUT and stays untouched."""
    headers_alice, _ = await _register(client, "alice")
    bond = await _create_bond(client, headers_alice)
    account = await _create_account_for_user(client, headers_alice)
    created = await _buy(client, headers_alice, bond["id"], account["id"])

    headers_bob, _ = await _register(client, "bob")
    response = await client.put(
        f"/api/transactions/{created['id']}", json={"price": 1.0}, headers=headers_bob
    )
    assert response.status_code == 403

    response = await client.get(f"/api/transactions/{created['id']}", headers=headers_alice)
    assert response.status_code == 200
    assert response.json()["price"] == 980.5


async def test_update_sell_above_position_422(client: httpx.AsyncClient) -> None:
    """Growing a SELL beyond the position is rejected (InsufficientPositionError)."""
    headers = await _register_and_login(client)
    _, _, _, sell = await _setup_pair(client, headers)

    response = await client.put(
        f"/api/transactions/{sell['id']}", json={"quantity": 15}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert "detail" in response.json()

    # The stored row is untouched.
    response = await client.get(f"/api/transactions/{sell['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["quantity"] == 5


async def test_update_buy_quantity_below_outstanding_sell_422(client: httpx.AsyncClient) -> None:
    """Shrinking a BUY below the outstanding SELL is rejected (position < 0)."""
    headers = await _register_and_login(client)
    _, _, buy, _ = await _setup_pair(client, headers)  # BUY 10, SELL 5 -> position 5

    response = await client.put(
        f"/api/transactions/{buy['id']}", json={"quantity": 3}, headers=headers
    )
    assert response.status_code == 422, response.text

    response = await client.get(f"/api/transactions/{buy['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["quantity"] == 10


async def test_update_buy_to_foreign_account_400(client: httpx.AsyncClient) -> None:
    """Moving a transaction to another user's account is rejected with 400."""
    headers_alice, _ = await _register(client, "alice")
    bond = await _create_bond(client, headers_alice)
    account = await _create_account_for_user(client, headers_alice)
    created = await _buy(client, headers_alice, bond["id"], account["id"])

    headers_bob, _ = await _register(client, "bob")
    account_of_bob = await _create_account_for_user(client, headers_bob)

    response = await client.put(
        f"/api/transactions/{created['id']}",
        json={"broker_account_id": account_of_bob["id"]},
        headers=headers_alice,
    )
    assert response.status_code == 400
    assert "detail" in response.json()


# --------------------------------------------------------------------- #
# DELETE


async def test_delete_sell_succeeds(client: httpx.AsyncClient) -> None:
    """Deleting a SELL (only increases the position) answers 204."""
    headers = await _register_and_login(client)
    _, _, _, sell = await _setup_pair(client, headers)

    response = await client.delete(f"/api/transactions/{sell['id']}", headers=headers)
    assert response.status_code == 204

    response = await client.get(f"/api/transactions/{sell['id']}", headers=headers)
    assert response.status_code == 404


async def test_delete_buy_with_unmatched_sell_422(client: httpx.AsyncClient) -> None:
    """Deleting a BUY that would leave a SELL unmatched is rejected."""
    headers = await _register_and_login(client)
    _, _, buy, _ = await _setup_pair(client, headers)  # BUY 10, SELL 5

    response = await client.delete(f"/api/transactions/{buy['id']}", headers=headers)
    assert response.status_code == 422, response.text
    assert "detail" in response.json()

    # The BUY survived the rejected delete.
    response = await client.get(f"/api/transactions/{buy['id']}", headers=headers)
    assert response.status_code == 200


async def test_delete_buy_without_sell_succeeds(client: httpx.AsyncClient) -> None:
    """Deleting a BUY with no outstanding SELL answers 204."""
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    account = await _create_account_for_user(client, headers)
    buy = await _buy(client, headers, bond["id"], account["id"])

    response = await client.delete(f"/api/transactions/{buy['id']}", headers=headers)
    assert response.status_code == 204


async def test_delete_missing_transaction_404(client: httpx.AsyncClient) -> None:
    """DELETE on an unknown id answers 404."""
    headers = await _register_and_login(client)
    response = await client.delete("/api/transactions/999999", headers=headers)
    assert response.status_code == 404


async def test_delete_other_users_transaction_403(client: httpx.AsyncClient) -> None:
    """Another user's transaction answers 403 on DELETE and stays alive."""
    headers_alice, _ = await _register(client, "alice")
    bond = await _create_bond(client, headers_alice)
    account = await _create_account_for_user(client, headers_alice)
    created = await _buy(client, headers_alice, bond["id"], account["id"])

    headers_bob, _ = await _register(client, "bob")
    response = await client.delete(f"/api/transactions/{created['id']}", headers=headers_bob)
    assert response.status_code == 403

    response = await client.get(f"/api/transactions/{created['id']}", headers=headers_alice)
    assert response.status_code == 200
