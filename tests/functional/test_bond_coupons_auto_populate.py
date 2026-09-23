"""Functional tests: create-time coupon auto-populate over the REST API (DC-5).

Two scenarios mirror the user decision "auto-populate must never alter the 201
create response":

1. Success: ``POST /api/bonds`` (201) fires the background populate job, which
   persists coupons; they become visible via ``GET /api/bonds/{isin}/coupons``
   once the task is awaited.
2. Provider failure during the background job: the 201 is unchanged, no rows
   are written (empty schedule does not wipe anything), and the failure is
   reported only via a warning log — the app does not crash.

Constraints honoured: no production code, no ``tests/conftest.py`` (task-14),
no network — MOEX is isolated by stubbing ``fetch_coupon_schedule`` on the real
client. The session-factory wiring that the shared ``app`` fixture omits is
installed here (mirroring ``bond_accounting/main.py``'s production wiring).
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

import pytest

from bond_accounting.api.deps import get_session_factory
from bond_accounting.market_data import CouponScheduleEntry
from tests.rest_utils import _register_and_login

if TYPE_CHECKING:
    import asyncio

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.market_data import BondReferenceService

_VALID_PAYLOAD = {
    "name": "ОФЗ 26229",
    "nominal": 1000,
    "coupon_rate": 7.5,
    "coupon_period_days": 182,
    "maturity_date": "2041-03-01",
}


@pytest.fixture(autouse=True)
def _wire_populate_stack(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    bond_reference_service: BondReferenceService,
) -> None:
    """Mirror production wiring that the shared ``app`` fixture omits.

    Lets ``schedule_populate_coupons`` spawn its real background job and
    ``GET /coupons`` read the migrated DB.
    """
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    bond_reference_service._session_factory = session_factory  # test seam


@pytest.fixture
def capture_tasks(
    bond_reference_service: BondReferenceService,
) -> list[asyncio.Task[None]]:
    """Expose the fire-and-forget tasks so the test can await them."""
    captured: list[asyncio.Task[None]] = []
    original = bond_reference_service.schedule_populate_coupons

    def wrapped(isin: str) -> asyncio.Task[None]:
        task = original(isin)
        captured.append(task)
        return task

    bond_reference_service.schedule_populate_coupons = wrapped  # type: ignore
    return captured


async def test_create_bond_populates_coupons_automatically(
    client, capture_tasks: list[asyncio.Task[None]], bond_reference_service: BondReferenceService
) -> None:
    """201 create → background populate → coupons visible via GET /coupons."""
    schedule = [
        CouponScheduleEntry(coupon_date=datetime.date(2027, 7, 1), coupon_amount=41.25),
        CouponScheduleEntry(coupon_date=datetime.date(2027, 1, 13), coupon_amount=40.0),
        CouponScheduleEntry(coupon_date=datetime.date(2028, 1, 5), coupon_amount=41.25),
    ]
    client_obj = bond_reference_service._client  # test seam

    async def fake_fetch(isin: str) -> list[CouponScheduleEntry]:
        return schedule

    client_obj.fetch_coupon_schedule = fake_fetch  # type: ignore

    headers = await _register_and_login(client)
    response = await client.post(
        "/api/bonds", json={**_VALID_PAYLOAD, "isin": "RU000A0JWS8E"}, headers=headers
    )
    assert response.status_code == 201, response.text
    assert response.json()["isin"] == "RU000A0JWS8E"

    # The create fired exactly one auto-populate job.
    assert len(capture_tasks) == 1
    for task in capture_tasks:
        await task

    coupons = await client.get("/api/bonds/RU000A0JWS8E/coupons", headers=headers)
    assert coupons.status_code == 200, coupons.text
    assert [c["coupon_date"] for c in coupons.json()] == [
        "2027-01-13",
        "2027-07-01",
        "2028-01-05",
    ]


async def test_create_bond_provider_failure_keeps_201_and_warns(
    client,
    capture_tasks: list[asyncio.Task[None]],
    bond_reference_service: BondReferenceService,
    caplog,
) -> None:
    """A provider failure in the background populate never changes the 201.

    The bond is still created, no coupon rows are written (no partial state),
    and the failure surfaces only as a warning log — not a crash.
    """
    client_obj = bond_reference_service._client  # test seam

    async def failing_fetch(isin: str) -> list[CouponScheduleEntry]:
        raise RuntimeError("provider exploded")

    client_obj.fetch_coupon_schedule = failing_fetch  # type: ignore

    headers = await _register_and_login(client)
    with caplog.at_level(logging.WARNING, logger="bond_accounting.market_data.service"):
        response = await client.post(
            "/api/bonds", json={**_VALID_PAYLOAD, "isin": "RU000A0JX1D4"}, headers=headers
        )
    assert response.status_code == 201, response.text
    assert response.json()["isin"] == "RU000A0JX1D4"

    # Background job settled without crashing the process.
    for task in capture_tasks:
        await task

    # Failure was logged as a warning, not raised into the response.
    assert any("background populate_coupons" in record.message for record in caplog.records)

    # No coupon rows were written for the failed populate.
    coupons = await client.get("/api/bonds/RU000A0JX1D4/coupons", headers=headers)
    assert coupons.status_code == 200, coupons.text
    assert coupons.json() == []
