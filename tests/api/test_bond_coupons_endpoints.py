"""REST tests for the bond coupon endpoints (DC-4) and REST-side create-time
auto-populate (DC-5): ``POST /api/bonds/{isin}/sync-coupons`` (202 + task_id,
404/422/401/503 matrix) and ``GET /api/bonds/{isin}/coupons`` (200 sorted
list / empty list, 404/422/401).

Stack notes
-----------
The shared ``tests/conftest.py`` ``app`` fixture deliberately wires the API
without a ``session_factory`` provider (the ``GET /coupons`` read depends on
``get_session_factory``) and builds ``BondReferenceService`` without a
``session_factory`` (so its fire-and-forget scheduler raises ``RuntimeError``).
Task-14 owns those fixtures, which have NOT landed yet; this module therefore
installs its own dependency override + service seam so the coupons endpoints
work against the real migrated SQLite DB. No production code and no root
conftest files are touched.

MOEX HTTP is mocked via the shared ``moex_mock_handler``. For tests that need a
deterministic coupon schedule we isolate ``fetch_coupon_schedule`` (the real
client method) so the background populate persists real rows via the real DB
session — no live network, no flaky sleeps.
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

import pytest

from bond_accounting.api.deps import get_session_factory
from bond_accounting.market_data import CouponScheduleEntry
from tests.rest_utils import _register_and_login

if TYPE_CHECKING:
    import asyncio

    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.market_data import BondReferenceService

#: 32 lowercase hex chars — ``uuid.uuid4().hex`` (DC-4a).
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_VALID_PAYLOAD = {
    "name": "ОФЗ 26207",
    "nominal": 1000,
    "coupon_rate": 8.15,
    "coupon_period_days": 182,
    "maturity_date": "2041-02-26",
}


def _schedule() -> list[CouponScheduleEntry]:
    """Deterministic schedule, deliberately NOT date-sorted.

    The read endpoint must return coupons ordered by ``coupon_date``
    regardless of insertion order.
    """
    return [
        CouponScheduleEntry(coupon_date=datetime.date(2026, 12, 8), coupon_amount=35.40),
        CouponScheduleEntry(coupon_date=datetime.date(2026, 6, 4), coupon_amount=34.04),
        CouponScheduleEntry(coupon_date=datetime.date(2027, 6, 2), coupon_amount=34.04),
    ]


@pytest.fixture(autouse=True)
def _wire_coupons_stack(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    bond_reference_service: BondReferenceService,
) -> None:
    """Make the coupons endpoints testable against the real migrated DB.

    ``app.dependency_overrides[get_session_factory]`` lets ``GET /coupons``
    open its own DB session, and injecting ``_session_factory`` into the real
    ``BondReferenceService`` lets ``schedule_populate_coupons`` spawn its
    background job instead of raising ``RuntimeError``.
    """
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    bond_reference_service._session_factory = session_factory  # test seam


@pytest.fixture
def capture_tasks(
    bond_reference_service: BondReferenceService,
) -> list[asyncio.Task[None]]:
    """Wrap ``schedule_populate_coupons`` to expose the spawned asyncio tasks.

    Both ``create_bond`` and ``sync_bond_coupons`` fire the job and discard the
    returned task; tests need a handle to await it deterministically (the card
    calls for "await the stubbed task"). The wrapper preserves fire-and-forget
    semantics and records every spawned task.
    """
    captured: list[asyncio.Task[None]] = []
    original = bond_reference_service.schedule_populate_coupons

    def wrapped(isin: str) -> asyncio.Task[None]:
        task = original(isin)
        captured.append(task)
        return task

    bond_reference_service.schedule_populate_coupons = wrapped  # type: ignore
    return captured


@pytest.fixture
def moex_schedule(bond_reference_service: BondReferenceService) -> list[CouponScheduleEntry]:
    """Isolate MOEX HTTP: the real client returns a fixed coupon schedule.

    Keeps ``populate_coupons``/``schedule_populate_coupons`` fully real (real
    DB writes via the injected session factory) while bypassing the network.
    """
    entries = _schedule()
    client = bond_reference_service._client  # test seam

    async def fake_fetch(isin: str) -> list[CouponScheduleEntry]:
        return entries

    client.fetch_coupon_schedule = fake_fetch  # type: ignore
    return entries


async def _create_bond(client: httpx.AsyncClient, headers: dict[str, str], isin: str) -> None:
    """Create a bond; asserting 201."""
    response = await client.post(
        "/api/bonds", json={**_VALID_PAYLOAD, "isin": isin}, headers=headers
    )
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# POST /api/bonds/{isin}/sync-coupons — DC-4a
# --------------------------------------------------------------------------- #


async def test_sync_coupons_returns_202_task_id(
    client: httpx.AsyncClient, app: FastAPI, capture_tasks: list[asyncio.Task[None]]
) -> None:
    """A known bond spawns a job and answers 202 with a fresh 32-hex task_id.

    Nothing about the task is persisted (the response body contains only
    ``task_id``), per user decision 2026-09-23.
    """
    headers = await _register_and_login(client)
    await _create_bond(client, headers, "RU000A0JX0J2")

    response = await client.post("/api/bonds/RU000A0JX0J2/sync-coupons", headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"task_id"}, f"unexpected body keys: {body}"
    assert _TASK_ID_RE.match(body["task_id"]), body["task_id"]
    # The background job was actually spawned.
    assert len(capture_tasks) >= 1


async def test_sync_coupons_unknown_bond_not_found(client: httpx.AsyncClient) -> None:
    """An ISIN absent from the DB maps to 404 before any job is spawned."""
    headers = await _register_and_login(client)
    response = await client.post("/api/bonds/XX0000000000/sync-coupons", headers=headers)
    assert response.status_code == 404, response.text
    assert "detail" in response.json()


async def test_sync_coupons_oversized_isin_unprocessable(
    client: httpx.AsyncClient,
) -> None:
    """An ISIN longer than 64 chars is rejected with 422 (``max_length``)."""
    headers = await _register_and_login(client)
    response = await client.post(f"/api/bonds/{'a' * 65}/sync-coupons", headers=headers)
    assert response.status_code == 422, response.text


async def test_sync_coupons_empty_isin_path_rejected(client: httpx.AsyncClient) -> None:
    """An empty ISIN segment (double slash) is not a routable path param.

    Starlette's ``{isin}`` route matches ``[^/]+`` (one or more chars), so an
    empty segment in ``/api/bonds//sync-coupons`` never matches the route and
    falls through to a 404 (route not found) — NOT a 422 bond-lookup response.
    ``min_length=1`` is therefore unobservable via the URL path.
    """
    headers = await _register_and_login(client)
    response = await client.post("/api/bonds//sync-coupons", headers=headers)
    assert response.status_code == 404, response.text


async def test_sync_coupons_without_token_unauthorized(client: httpx.AsyncClient) -> None:
    """The sync endpoint requires a bearer token."""
    response = await client.post("/api/bonds/RU000A0JX0J2/sync-coupons")
    assert response.status_code == 401


async def test_sync_coupons_disabled_integration_service_unavailable(
    client: httpx.AsyncClient, bond_reference_service: BondReferenceService
) -> None:
    """A disabled market_data integration answers 503 before spawning a job."""
    headers = await _register_and_login(client)
    await _create_bond(client, headers, "RU000A0JX0J2")
    bond_reference_service.enabled = False

    response = await client.post("/api/bonds/RU000A0JX0J2/sync-coupons", headers=headers)
    assert response.status_code == 503, response.text
    assert "detail" in response.json()


async def test_sync_coupons_background_job_populates_db(
    client: httpx.AsyncClient,
    capture_tasks: list[asyncio.Task[None]],
    moex_schedule: list[CouponScheduleEntry],
) -> None:
    """Awaiting the spawned job makes the schedule readable via GET /coupons.

    Exercises the full loop: POST sync → fire-and-forget job → DB write →
    GET returns the persisted schedule ordered by date.
    """
    headers = await _register_and_login(client)
    await _create_bond(client, headers, "RU000A0JX0J2")

    response = await client.post("/api/bonds/RU000A0JX0J2/sync-coupons", headers=headers)
    assert response.status_code == 202, response.text
    # Deterministically flush the background populate (no flaky real sleeps).
    for task in capture_tasks:
        await task

    response = await client.get("/api/bonds/RU000A0JX0J2/coupons", headers=headers)
    assert response.status_code == 200, response.text
    coupons = response.json()
    assert [c["coupon_date"] for c in coupons] == ["2026-06-04", "2026-12-08", "2027-06-02"]
    assert [c["coupon_amount"] for c in coupons] == [34.04, 35.4, 34.04]


# --------------------------------------------------------------------------- #
# GET /api/bonds/{isin}/coupons — DC-4b
# --------------------------------------------------------------------------- #


async def test_get_coupons_empty_list_is_valid(
    client: httpx.AsyncClient, capture_tasks: list[asyncio.Task[None]]
) -> None:
    """A known bond with no saved schedule is a valid 200 with ``[]`` (not 404/503).

    The create-time auto-populate fails silently here (no MOEX schedule) so no
    coupon rows are written; the read must still answer 200.
    """
    headers = await _register_and_login(client)
    await _create_bond(client, headers, "RU000A0JX0J2")
    # Let any auto-populate job settle so the DB state is deterministic.
    for task in capture_tasks:
        await task

    response = await client.get("/api/bonds/RU000A0JX0J2/coupons", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_get_coupons_populated_sorted(
    client: httpx.AsyncClient,
    capture_tasks: list[asyncio.Task[None]],
    moex_schedule: list[CouponScheduleEntry],
) -> None:
    """Coupons are returned with ISO dates + amounts, ordered by date.

    The schedule is fed out of order to prove the endpoint sorts on read.
    """
    headers = await _register_and_login(client)
    await _create_bond(client, headers, "RU000A0JX0J2")
    for task in capture_tasks:
        await task

    response = await client.get("/api/bonds/RU000A0JX0J2/coupons", headers=headers)
    assert response.status_code == 200, response.text
    coupons = response.json()
    assert [c["coupon_date"] for c in coupons] == ["2026-06-04", "2026-12-08", "2027-06-02"]
    assert all(isinstance(c["coupon_amount"], float) for c in coupons)


async def test_get_coupons_unknown_bond_not_found(client: httpx.AsyncClient) -> None:
    """An ISIN absent from the DB maps to 404."""
    headers = await _register_and_login(client)
    response = await client.get("/api/bonds/XX0000000000/coupons", headers=headers)
    assert response.status_code == 404, response.text
    assert "detail" in response.json()


async def test_get_coupons_oversized_isin_unprocessable(client: httpx.AsyncClient) -> None:
    """An ISIN longer than 64 chars is rejected with 422 (``max_length``)."""
    headers = await _register_and_login(client)
    response = await client.get(f"/api/bonds/{'a' * 65}/coupons", headers=headers)
    assert response.status_code == 422, response.text


async def test_get_coupons_empty_isin_path_rejected(client: httpx.AsyncClient) -> None:
    """An empty ISIN segment (double slash) does not match the route.

    Same as the POST case: ``/api/bonds//coupons`` cannot be parsed as
    ``{isin}`` (Starlette ``[^/]+``), so it answers 404 route-not-found rather
    than a 422 validation error.
    """
    headers = await _register_and_login(client)
    response = await client.get("/api/bonds//coupons", headers=headers)
    assert response.status_code == 404, response.text


async def test_get_coupons_without_token_unauthorized(client: httpx.AsyncClient) -> None:
    """The coupons endpoint requires a bearer token."""
    response = await client.get("/api/bonds/RU000A0JX0J2/coupons")
    assert response.status_code == 401
