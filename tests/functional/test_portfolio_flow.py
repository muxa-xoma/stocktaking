"""Functional scenarios: investing and reviewing the portfolio over REST.

Moved as-is from ``tests/api/test_api_rest.py`` — full user paths: registering,
creating a bond, trading it and verifying the resulting portfolio state
(positions, realized PnL, analytics).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.functional._functional_utils import (
    _create_bond,
    _register_and_login,
)
from tests.rest_utils import _create_account_for_user

if TYPE_CHECKING:
    import httpx


async def test_transactions_and_portfolio_flow(client: httpx.AsyncClient) -> None:
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    bond_id = bond["id"]
    # TransactionCreate requires a broker_account_id owned by the caller.
    account = await _create_account_for_user(client, headers)

    # BUY 10 @ 1000, commission 5.
    response = await client.post(
        "/api/transactions",
        json={
            "bond_id": bond_id,
            "broker_account_id": account["id"],
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
            "broker_account_id": account["id"],
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


async def test_portfolio_realized_sells_reset_after_full_close(client: httpx.AsyncClient) -> None:
    """BUY 10@100, SELL 10@120, BUY 5@200, SELL 5@210 -> realized sells = 250.

    The exit after the reopen must be priced against the post-reopen average
    (200), not blended with the pre-close history.
    """
    headers = await _register_and_login(client)
    bond = await _create_bond(client, headers)
    bond_id = bond["id"]
    # TransactionCreate requires a broker_account_id owned by the caller.
    account = await _create_account_for_user(client, headers)
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
                "broker_account_id": account["id"],
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
