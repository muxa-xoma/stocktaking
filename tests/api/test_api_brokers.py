"""Black-box REST API tests for the broker and broker-account endpoints.

Also covers the ``broker_account_id`` query parameter on the transaction and
portfolio endpoints, and the 401/404/409 guard paths. The fixture stack
(``app`` / ``client``) lives in ``tests/conftest.py``.

Known gap (recorded for the fix-loop): a duplicate broker name raises the
base ``BrokerError``, which ``register_exception_handlers`` does not map to
an HTTP status — the endpoint answers 500 instead of 409.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.rest_utils import (
    BROKER_PAYLOAD,
    _create_account,
    _create_account_for_user,
    _create_bond,
    _create_broker,
    _register,
    _register_and_login,
)

if TYPE_CHECKING:
    import httpx


# --------------------------------------------------------------------- #
# bearer-auth protection


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/brokers"),
        ("POST", "/api/brokers"),
        ("GET", "/api/brokers/1"),
        ("PUT", "/api/brokers/1"),
        ("DELETE", "/api/brokers/1"),
        ("GET", "/api/broker-accounts"),
        ("POST", "/api/broker-accounts"),
        ("GET", "/api/broker-accounts/1"),
        ("PUT", "/api/broker-accounts/1"),
        ("DELETE", "/api/broker-accounts/1"),
    ],
)
async def test_endpoints_without_token_are_unauthorized(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 401


# --------------------------------------------------------------------- #
# broker CRUD


async def test_create_and_get_broker(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    created = await _create_broker(client, headers, name="ВТБ", commission=0.3, min_commission=5.0)

    assert created["id"] > 0
    assert created["name"] == "ВТБ"
    # Commission is stored as raw percent: 0.3 == 0.3%.
    assert created["commission"] == pytest.approx(0.3)
    assert created["min_commission"] == pytest.approx(5.0)
    assert created["created_at"]

    response = await client.get(f"/api/brokers/{created['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json() == created


async def test_list_brokers(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    first = await _create_broker(client, headers)
    second = await _create_broker(client, headers)

    response = await client.get("/api/brokers", headers=headers)
    assert response.status_code == 200
    ids = [broker["id"] for broker in response.json()]
    assert {first["id"], second["id"]} <= set(ids)


async def test_get_nonexistent_broker_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.get("/api/brokers/999999", headers=headers)
    assert response.status_code == 404


async def test_update_broker_partial(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    created = await _create_broker(client, headers, commission=0.3)

    response = await client.put(
        f"/api/brokers/{created['id']}", json={"commission": 5.0}, headers=headers
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["commission"] == pytest.approx(5.0)
    # Untouched fields keep their values.
    assert updated["name"] == created["name"]


async def test_update_nonexistent_broker_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.put("/api/brokers/999999", json={"commission": 1.0}, headers=headers)
    assert response.status_code == 404


async def test_delete_broker_no_content(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    created = await _create_broker(client, headers)

    response = await client.delete(f"/api/brokers/{created['id']}", headers=headers)
    assert response.status_code == 204
    response = await client.get(f"/api/brokers/{created['id']}", headers=headers)
    assert response.status_code == 404


async def test_delete_nonexistent_broker_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.delete("/api/brokers/999999", headers=headers)
    assert response.status_code == 404


async def test_delete_broker_with_accounts_conflict(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    broker = await _create_broker(client, headers)
    await _create_account(client, headers, broker["id"])

    response = await client.delete(f"/api/brokers/{broker['id']}", headers=headers)
    assert response.status_code == 409
    assert "detail" in response.json()

    # The broker survives the blocked deletion.
    response = await client.get(f"/api/brokers/{broker['id']}", headers=headers)
    assert response.status_code == 200


async def test_create_broker_duplicate_name_conflict(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    await _create_broker(client, headers, name="Дубликат")

    response = await client.post(
        "/api/brokers", json={**BROKER_PAYLOAD, "name": "Дубликат"}, headers=headers
    )
    assert response.status_code == 409, response.text


# --------------------------------------------------------------------- #
# broker-account CRUD


async def test_create_and_get_account(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    broker = await _create_broker(client, headers)
    created = await _create_account(client, headers, broker["id"], account_type="IIS")

    assert created["id"] > 0
    assert created["broker_id"] == broker["id"]
    assert created["broker_name"] == broker["name"]
    assert created["name"] == "Main account"
    assert created["account_type"] == "IIS"

    response = await client.get(f"/api/broker-accounts/{created['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json() == created


async def test_create_account_unknown_broker_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.post(
        "/api/broker-accounts",
        json={"broker_id": 999999, "name": "Основной", "account_type": "STANDARD"},
        headers=headers,
    )
    assert response.status_code == 404
    assert "detail" in response.json()


async def test_list_accounts_shows_only_caller_accounts(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    other_headers, _ = await _register(client, "bob")
    broker = await _create_broker(client, headers)
    mine = await _create_account(client, headers, broker["id"])
    await _create_account(client, other_headers, broker["id"])

    response = await client.get("/api/broker-accounts", headers=headers)
    assert response.status_code == 200
    ids = [account["id"] for account in response.json()]
    assert ids == [mine["id"]]


async def test_update_account_by_owner(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    account = await _create_account_for_user(client, headers)

    response = await client.put(
        f"/api/broker-accounts/{account['id']}", json={"name": "Renamed"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    assert response.json()["broker_name"] == account["broker_name"]


async def test_update_account_by_stranger_not_found(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    stranger_headers, _ = await _register(client, "mallory")
    account = await _create_account_for_user(client, headers)

    response = await client.put(
        f"/api/broker-accounts/{account['id']}",
        json={"name": "hacked"},
        headers=stranger_headers,
    )
    assert response.status_code == 404

    # The account is unchanged.
    response = await client.get(f"/api/broker-accounts/{account['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == account["name"]


async def test_get_nonexistent_account_not_found(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    response = await client.get("/api/broker-accounts/999999", headers=headers)
    assert response.status_code == 404


async def test_get_account_by_stranger_not_found(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    stranger_headers, _ = await _register(client, "mallory")
    account = await _create_account_for_user(client, headers)

    response = await client.get(f"/api/broker-accounts/{account['id']}", headers=stranger_headers)
    assert response.status_code == 404


async def test_delete_account_no_content(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    account = await _create_account_for_user(client, headers)

    response = await client.delete(f"/api/broker-accounts/{account['id']}", headers=headers)
    assert response.status_code == 204
    response = await client.get(f"/api/broker-accounts/{account['id']}", headers=headers)
    assert response.status_code == 404


async def test_delete_account_by_stranger_not_found(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    stranger_headers, _ = await _register(client, "mallory")
    account = await _create_account_for_user(client, headers)

    response = await client.delete(
        f"/api/broker-accounts/{account['id']}", headers=stranger_headers
    )
    assert response.status_code == 404

    # The account survives the blocked deletion.
    response = await client.get(f"/api/broker-accounts/{account['id']}", headers=headers)
    assert response.status_code == 200


async def test_delete_account_with_transactions_conflict(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    account = await _create_account_for_user(client, headers)

    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond["id"],
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

    response = await client.delete(f"/api/broker-accounts/{account['id']}", headers=headers)
    assert response.status_code == 409
    assert "detail" in response.json()

    # The account survives the blocked deletion.
    response = await client.get(f"/api/broker-accounts/{account['id']}", headers=headers)
    assert response.status_code == 200


# --------------------------------------------------------------------- #
# broker_account_id on transactions and portfolio endpoints


async def _setup_two_accounts_with_buys(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[int, int, int]:
    """A bond and two accounts with one BUY each; returns ``(bond_id, acc1, acc2)``."""
    bond = await _create_bond(client, headers)
    broker = await _create_broker(client, headers)
    first = await _create_account(client, headers, broker["id"])
    second = await _create_account(client, headers, broker["id"])
    for account in (first, second):
        response = await client.post(
            "/api/transactions",
            json={
                "bond_id": bond["id"],
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
    return bond["id"], first["id"], second["id"]


async def test_transactions_filtered_by_account(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    _, first, second = await _setup_two_accounts_with_buys(client, headers)

    response = await client.get(
        "/api/transactions", params={"broker_account_id": first}, headers=headers
    )
    assert response.status_code == 200
    txns = response.json()
    assert len(txns) == 1
    assert txns[0]["broker_account_id"] == first

    # Without the filter both accounts' transactions are listed.
    response = await client.get("/api/transactions", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 2

    # An account with no transactions lists none (but belongs to the user).
    response = await client.get(
        "/api/transactions", params={"broker_account_id": second + 10**9}, headers=headers
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_positions_filtered_by_account(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    bond_id, first, _ = await _setup_two_accounts_with_buys(client, headers)

    response = await client.get(
        "/api/portfolio/positions", params={"broker_account_id": first}, headers=headers
    )
    assert response.status_code == 200
    positions = response.json()
    assert len(positions) == 1
    assert positions[0]["bond_id"] == bond_id
    assert positions[0]["quantity"] == 1

    # Without the filter the position aggregates both accounts.
    response = await client.get("/api/portfolio/positions", headers=headers)
    assert response.status_code == 200
    positions = response.json()
    assert len(positions) == 1
    assert positions[0]["quantity"] == 2


async def test_portfolio_summary_filtered_by_account(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    _, first, _ = await _setup_two_accounts_with_buys(client, headers)

    response = await client.get(
        "/api/portfolio", params={"broker_account_id": first}, headers=headers
    )
    assert response.status_code == 200, response.text
    summary = response.json()
    # One BUY of 1 @ 1000 on this account only.
    assert summary["total_invested"] == pytest.approx(1000.0)

    # Without the filter the summary aggregates both accounts.
    response = await client.get("/api/portfolio", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["total_invested"] == pytest.approx(2000.0)


async def test_transaction_with_foreign_account_bad_request(client: httpx.AsyncClient) -> None:
    headers, _ = await _register(client)
    stranger_headers, _ = await _register(client, "mallory")
    bond = await _create_bond(client, headers)
    foreign_account = await _create_account_for_user(client, stranger_headers)

    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond["id"],
            "broker_account_id": foreign_account["id"],
            "type": "BUY",
            "quantity": 1,
            "price": 1000.0,
            "date": "2026-01-10",
            "commission": 0.0,
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert "detail" in response.json()
