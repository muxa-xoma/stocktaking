"""Wave 15 regression REST tests (Tasks 32, 34, 41, 42, 43, 44).

Split out of the former monolithic ``test_wave15.py``: everything exercising
the API over ``httpx``/ASGITransport, backed by real services on a migrated
temporary SQLite database and a running ``AsyncQueueEventBus``. The fixture
stack (``migrated_db_url`` / ``session_factory`` / ``event_bus`` / ``app`` /
``client``) lives in ``tests/conftest.py``.

* T32 — bond DTO validation + ``issuer: null`` clearing semantics.
* T34 — ``nominal`` default 1000.
* T41 — API 401 for expired/tampered tokens.
* T42/T43 — ``next_coupons`` / ``upcoming_cashflows`` query parameters.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest

from bond_accounting.auth import JwtService
from bond_accounting.config.settings import AuthConfig
from bond_accounting.db.models import Bond, Broker, BrokerAccount, Transaction
from tests.rest_utils import _bond_payload, _register

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# =========================================================================== #
# 2. Bond DTO validation (Task 32)
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
# 3. Bond nominal DEFAULT 1000 (Task 34)
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
# 4. PyJWT API rejection (Task 41)
# =========================================================================== #


async def test_api_rejects_expired_token(client: httpx.AsyncClient, jwt_secret: str) -> None:
    jwt_service = JwtService(AuthConfig(jwt_secret=jwt_secret))
    expired = jwt_service.create_token(user_id=1, username="alice", expires_minutes=-1)
    response = await client.get("/api/bonds", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


async def test_api_rejects_tampered_token(client: httpx.AsyncClient, jwt_secret: str) -> None:
    jwt_service = JwtService(AuthConfig(jwt_secret=jwt_secret))
    token = jwt_service.create_token(user_id=1, username="alice")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    response = await client.get("/api/bonds", headers={"Authorization": f"Bearer {tampered}"})
    assert response.status_code == 401


# =========================================================================== #
# 5/6. next_coupons / upcoming_cashflows parameters (Tasks 42, 43)
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
        broker = Broker(name="Coupon grid broker", commission=0.3)
        session.add(broker)
        await session.flush()
        account = BrokerAccount(
            user_id=user_id, broker_id=broker.id, name="Основной", account_type="STANDARD"
        )
        session.add(account)
        await session.flush()
        session.add(
            Transaction(
                user_id=user_id,
                bond_id=bond.id,
                broker_account_id=account.id,
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
