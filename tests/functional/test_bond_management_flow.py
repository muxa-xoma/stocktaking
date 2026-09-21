"""Functional scenarios: bond management over the REST API.

Moved as-is from ``tests/api/test_api_rest.py`` and
``tests/api/test_wave15_rest.py`` — full user paths: registering, managing a
bond through its lifecycle (CRUD, deletion rules) and the two-user ownership
scenario.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.functional._functional_utils import (
    BOND_PAYLOAD,
    _bond_payload,
    _create_bond,
    _create_transaction,
    _register,
    _register_and_login,
)

if TYPE_CHECKING:
    import httpx


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
