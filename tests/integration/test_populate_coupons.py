"""Integration tests for the bondization coupon population (DC-1/DC-2/DC-3).

Covers the new ``MoexClient.fetch_coupon_schedule`` client method and the
``BondReferenceService.populate_coupons`` / ``schedule_populate_coupons``
service logic against a real SQLite database (Alembic-migrated temp DB via
the ``session_factory`` fixture) and an in-memory ``httpx.MockTransport``
MOEX responder.

Every MOEX HTTP interaction is mocked (no network). The response shapes
mirror the task-12 spike exactly: ISIN→SECID resolution through the global
``/iss/securities.json`` search (lowercase columns), then the ``bondization``
endpoint's ``coupons`` block. Per the delivered code the ``coupons`` block is
read with the lowercase keys ``coupondate`` / ``value_rub`` / ``value`` and is
paginated through the ``coupons.cursor`` block.
"""

from __future__ import annotations

import datetime
import logging
import typing

import httpx
import pytest

from bond_accounting.db.models import Bond, BondCoupon, User
from bond_accounting.market_data import (
    BondReferenceService,
    MoexClient,
    NoBondsFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from bond_accounting.market_data.schemas import CouponScheduleEntry

if typing.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE_URL = "https://iss.moex.com"
_ISIN = "RU000A100XP0"
_SECID = "SU26238RMFS5"

#: Lowercase global-search columns (bond candidates; ``secid``+``isin`` suffice
#: for ISIN→SECID resolution and ``_is_bond``).
_SEARCH_COLUMNS = ["secid", "isin", "shortname", "name", "group", "type"]
_SEARCH_ROW = [
    _SECID,
    _ISIN,
    "ОФЗ 26238",
    "ОФЗ 26238-ПД",
    "stock_bonds",
    "stock_bond",
]

#: ``coupons`` block columns read by ``coupon_schedule_from_block``
#: (lowercase keys, as the delivered parsing contract expects).
_COUPON_COLUMNS = ["coupondate", "value_rub", "value", "valueprc"]

#: A canonical 2-coupon schedule for a single page of the bondization block.
_COUPON_DATA = [
    ["2026-09-08", 35.4, 35.4, 7.1],
    ["2027-03-08", 35.4, 35.4, 7.1],
]


def _search_payload() -> dict:
    """A global-search response containing one resolvable bond candidate."""
    return {"securities": {"columns": _SEARCH_COLUMNS, "data": [_SEARCH_ROW]}}


def _empty_search_payload() -> dict:
    """A global-search response with no rows (ISIN not in the registry)."""
    return {"securities": {"columns": _SEARCH_COLUMNS, "data": []}}


def _coupons_block(data: list[list], total: int | None = None) -> dict:
    """One page of the ``bondization`` ``coupons`` block plus its cursor.

    ``total`` is the overall row count reported by the cursor (used to drive
    the client's pagination). Defaults to ``len(data)`` for single-page mocks.
    """
    total = len(data) if total is None else total
    return {
        "coupons": {"columns": _COUPON_COLUMNS, "data": data},
        "coupons.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, total, 20]]},
    }


def _make_client(
    search_data: list[list] | None = None,
    coupon_data: list[list] | None = None,
    page_size: int = 20,
    transport_error: Exception | None = None,
    http_status: int | None = None,
) -> tuple[MoexClient, httpx.AsyncClient]:
    """Build a ``MoexClient`` over a MockTransport serving search+bondization.

    ``search_data``: rows for the ``/iss/securities.json`` block; ``None``
    means the default one-candidate search. ``coupon_data``: rows for the
    ``bondization`` ``coupons`` block, paginated by the ``start`` query param
    in ``page_size`` slices honouring the ``coupons.cursor`` TOTAL.
    ``transport_error``: when set, the transport raises it for every request.
    ``http_status``: when set, every request returns that status instead of 200.

    The injected ``httpx.AsyncClient`` must be closed by the caller.
    """
    search_rows = _SEARCH_ROW if search_data is None else (search_data or [])
    coupon_rows = coupon_data if coupon_data is not None else []
    total = len(coupon_rows)
    page_size = min(page_size, total) if total else 0

    def handler(request: httpx.Request) -> httpx.Response:
        if transport_error is not None:
            raise transport_error
        if http_status is not None:
            return httpx.Response(http_status, json={})
        if request.url.path == "/iss/securities.json":
            return httpx.Response(
                200, json=_search_payload() if search_rows else _empty_search_payload()
            )
        if request.url.path.startswith("/iss/statistics/engines/stock/markets/bonds/bondization/"):
            if not total:
                return httpx.Response(200, json=_coupons_block([]))
            start = 0
            raw = request.url.params.get("start")
            if raw is not None:
                try:
                    start = int(raw)
                except TypeError, ValueError:
                    start = 0
            page = coupon_rows[start : start + page_size]
            return httpx.Response(200, json=_coupons_block(page, total=total))
        return httpx.Response(
            404, json={"error": {"code": f"unexpected path {request.url.path!r}"}}
        )

    transport = httpx.MockTransport(handler)
    injected = httpx.AsyncClient(base_url=BASE_URL, timeout=5.0, transport=transport)
    client = MoexClient(base_url=BASE_URL, timeout_s=5.0)
    client._http = injected  # type: ignore[attr-defined]
    return client, injected


def _coupon_rows(count: int) -> list[list]:
    """Build ``count`` coupon rows with distinct ascending dates/amounts."""
    rows: list[list] = []
    for i in range(count):
        day = (i % 28) + 1
        month = ((i // 28) % 12) + 1
        year = 2026 + i // 336
        rows.append([f"{year:04d}-{month:02d}-{day:02d}", float(30 + i), float(30 + i), 7.0])
    rows.sort(key=lambda r: r[0])
    return rows


async def _seed_bond(session_factory, isin: str = _ISIN) -> int:
    """Insert a user + bond and return the bond id."""
    async with session_factory() as session:
        user = User(username=f"owner-{isin}", password_hash="not-a-real-hash")
        session.add(user)
        await session.flush()
        bond = Bond(
            owner_id=user.id,
            isin=isin,
            name="Тестовая облигация",
            nominal=1000,
            coupon_rate=7.0,
            coupon_period_days=182,
            maturity_date=datetime.date(2041, 5, 15),
        )
        session.add(bond)
        await session.commit()
        return bond.id


async def _coupon_count(session_factory, bond_id: int) -> int:
    """Return the number of ``BondCoupon`` rows for ``bond_id``."""
    from sqlalchemy import func, select

    async with session_factory() as session:
        return await session.scalar(
            select(func.count()).select_from(BondCoupon).where(BondCoupon.bond_id == bond_id)
        )


async def _fetch_coupons(session_factory, bond_id: int) -> list[tuple[datetime.date, float]]:
    """Read ``BondCoupon`` rows for ``bond_id`` ordered by ``coupon_date``."""
    from sqlalchemy import select

    async with session_factory() as session:
        result = await session.execute(
            select(BondCoupon).where(BondCoupon.bond_id == bond_id).order_by(BondCoupon.coupon_date)
        )
        return [(row.coupon_date, row.coupon_amount) for row in result.scalars()]


async def _seed_coupons(
    session_factory, bond_id: int, dates: list[datetime.date], amounts: list[float] | None = None
) -> None:
    """Insert ``BondCoupon`` rows for ``bond_id`` (stale data in the tests)."""
    async with session_factory() as session:
        for idx, coupon_date in enumerate(dates):
            session.add(
                BondCoupon(
                    bond_id=bond_id,
                    coupon_date=coupon_date,
                    coupon_amount=(amounts[idx] if amounts else 10.0 + idx),
                )
            )
        await session.commit()


# --------------------------------------------------------------------------- #
# MoexClient.fetch_coupon_schedule (via MockTransport)
# --------------------------------------------------------------------------- #


async def test_fetch_coupon_schedule_success_parses_entries() -> None:
    """A resolvable ISIN + single-page schedule yields parsed entries."""
    client, injected = _make_client(coupon_data=_COUPON_DATA)
    try:
        result = await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()

    assert result == [
        CouponScheduleEntry(coupon_date=datetime.date(2026, 9, 8), coupon_amount=35.4),
        CouponScheduleEntry(coupon_date=datetime.date(2027, 3, 8), coupon_amount=35.4),
    ]


async def test_fetch_coupon_schedule_empty_block_returns_empty_list() -> None:
    """An empty ``coupons`` block is an empty schedule (never an error)."""
    client, injected = _make_client(coupon_data=[])
    try:
        result = await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()

    assert result == []


async def test_fetch_coupon_schedule_pagination_concatenates_pages() -> None:
    """A multi-page schedule (TOTAL > page) is concatenated in provider order."""
    # 45 rows → first page has the first 20 (our mock returns all up front);
    # the client loops on ``_cursor_total`` until ``len(accumulated) >= total``.
    rows = _coupon_rows(45)
    # A single call serves all rows; the mock returns the full block each page so
    # the client collects everything once and stops (total reached).
    client, injected = _make_client(coupon_data=rows)
    try:
        result = await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()

    assert len(result) == 45
    assert result[0].coupon_date < result[-1].coupon_date
    assert result[0].coupon_amount == 30.0
    assert result[-1].coupon_amount == 74.0


async def test_fetch_coupon_schedule_http_500_raises_provider_unavailable() -> None:
    """A non-200 provider response is ``ProviderUnavailableError`` (never [])."""
    client, injected = _make_client(http_status=500)
    try:
        with pytest.raises(ProviderUnavailableError):
            await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()


async def test_fetch_coupon_schedule_connect_error_raises_provider_unavailable() -> None:
    """A transport (connect) error maps to ``ProviderUnavailableError``."""
    client, injected = _make_client(transport_error=httpx.ConnectError("connection refused"))
    try:
        with pytest.raises(ProviderUnavailableError):
            await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()


async def test_fetch_coupon_schedule_timeout_raises_provider_timeout() -> None:
    """A read-timeout surfaces as ``ProviderTimeoutError`` (never [])."""
    client, injected = _make_client(transport_error=httpx.ReadTimeout("timed out"))
    try:
        with pytest.raises(ProviderTimeoutError):
            await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()


async def test_fetch_coupon_schedule_malformed_row_raises_provider_unavailable() -> None:
    """A malformed coupon row inside the block is a provider error (never [])."""
    client, injected = _make_client(coupon_data=[["not-a-date", 35.4, 35.4, 7.1]])
    try:
        with pytest.raises(ProviderUnavailableError):
            await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()


async def test_fetch_coupon_schedule_unknown_isin_raises_no_bonds_found() -> None:
    """An ISIN absent from the registry raises ``NoBondsFoundError``."""
    client, injected = _make_client(search_data=[])
    try:
        with pytest.raises(NoBondsFoundError):
            await client.fetch_coupon_schedule(_ISIN)
    finally:
        await injected.aclose()


async def test_fetch_coupon_schedule_isin_resolved_case_insensitively() -> None:
    """ISIN→SECID resolution matches case-insensitively (parity with get_by_isin)."""
    client, injected = _make_client(coupon_data=_COUPON_DATA)
    try:
        result = await client.fetch_coupon_schedule(_ISIN.casefold())
    finally:
        await injected.aclose()

    assert len(result) == 2


# --------------------------------------------------------------------------- #
# BondReferenceService.populate_coupons (real SQLite DB)
# --------------------------------------------------------------------------- #


async def test_populate_coupons_success_replaces_stale_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Success replaces N stale rows with the fetched schedule; returns N."""
    bond_id = await _seed_bond(session_factory)
    await _seed_coupons(
        session_factory,
        bond_id,
        [datetime.date(2020, 1, 1), datetime.date(2020, 7, 1), datetime.date(2021, 1, 1)],
    )

    client, injected = _make_client(coupon_data=_coupon_rows(6))
    service = BondReferenceService(client)
    async with session_factory() as session:
        try:
            inserted = await service.populate_coupons(_ISIN, session)
        finally:
            await injected.aclose()

    assert inserted == 6
    coupons = await _fetch_coupons(session_factory, bond_id)
    assert len(coupons) == 6
    # Old 2020 rows are gone; all remaining dates are from the new schedule.
    assert all(coupon_date.year >= 2026 for coupon_date, _ in coupons)
    assert coupons == sorted(coupons, key=lambda pair: pair[0])


async def test_populate_coupons_idempotent_rerun_same_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Running twice with the same schedule keeps the same final state (6, not 12)."""
    bond_id = await _seed_bond(session_factory)
    await _seed_coupons(
        session_factory,
        bond_id,
        [datetime.date(2020, 1, 1), datetime.date(2020, 7, 1), datetime.date(2021, 1, 1)],
    )
    coupon_rows = _coupon_rows(6)

    client, injected = _make_client(coupon_data=coupon_rows)
    service = BondReferenceService(client)
    async with session_factory() as session:
        try:
            first = await service.populate_coupons(_ISIN, session)
            second = await service.populate_coupons(_ISIN, session)
        finally:
            await injected.aclose()

    assert first == 6
    assert second == 6
    assert await _coupon_count(session_factory, bond_id) == 6


async def test_populate_coupons_empty_schedule_keeps_existing_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty schedule → returns 0 and the existing rows are unchanged (USER DECISION)."""
    bond_id = await _seed_bond(session_factory)
    stale_dates = [datetime.date(2020, 1, 1), datetime.date(2020, 7, 1), datetime.date(2021, 1, 1)]
    await _seed_coupons(session_factory, bond_id, stale_dates)
    before = await _fetch_coupons(session_factory, bond_id)

    client, injected = _make_client(coupon_data=[])
    service = BondReferenceService(client)
    async with session_factory() as session:
        try:
            result = await service.populate_coupons(_ISIN, session)
        finally:
            await injected.aclose()

    assert result == 0
    assert await _fetch_coupons(session_factory, bond_id) == before


async def test_populate_coupons_provider_error_keeps_existing_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A provider failure propagates and the existing rows are unchanged."""
    bond_id = await _seed_bond(session_factory)
    stale = [datetime.date(2020, 1, 1), datetime.date(2020, 7, 1), datetime.date(2021, 1, 1)]
    await _seed_coupons(session_factory, bond_id, stale)
    before = await _fetch_coupons(session_factory, bond_id)

    client, injected = _make_client(http_status=500)
    service = BondReferenceService(client)
    async with session_factory() as session:
        try:
            with pytest.raises(ProviderUnavailableError):
                await service.populate_coupons(_ISIN, session)
        finally:
            await injected.aclose()

    assert await _fetch_coupons(session_factory, bond_id) == before


async def test_populate_coupons_unknown_bond_no_rows_anywhere(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ISIN absent from the local DB raises NoBondsFoundError and writes nothing."""
    client, injected = _make_client(coupon_data=_coupon_rows(2))
    service = BondReferenceService(client)
    async with session_factory() as session:
        try:
            with pytest.raises(NoBondsFoundError):
                await service.populate_coupons("RU000ZZZZZZZZ", session)
        finally:
            await injected.aclose()

    from sqlalchemy import func, select

    async with session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(BondCoupon))
    assert total == 0


async def test_populate_coupons_case_insensitive_isin_parity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """populate_coupons matches the DB ISIN case-insensitively (parity via lower())."""
    bond_id = await _seed_bond(session_factory, isin=_ISIN)
    await _seed_coupons(
        session_factory,
        bond_id,
        [datetime.date(2020, 1, 1), datetime.date(2020, 7, 1), datetime.date(2021, 1, 1)],
    )

    client, injected = _make_client(coupon_data=_coupon_rows(2))
    service = BondReferenceService(client)
    async with session_factory() as session:
        try:
            inserted = await service.populate_coupons(_ISIN.casefold(), session)
        finally:
            await injected.aclose()

    assert inserted == 2
    assert await _coupon_count(session_factory, bond_id) == 2


# --------------------------------------------------------------------------- #
# BondReferenceService.schedule_populate_coupons (fire-and-forget)
# --------------------------------------------------------------------------- #


async def test_schedule_populate_coupons_background_replace_lands(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fires ``populate_coupons`` in the background; awaiting the task lands the replace."""
    caplog.set_level(logging.INFO)
    bond_id = await _seed_bond(session_factory)
    await _seed_coupons(
        session_factory,
        bond_id,
        [datetime.date(2020, 1, 1), datetime.date(2020, 7, 1), datetime.date(2021, 1, 1)],
    )

    client, injected = _make_client(coupon_data=_coupon_rows(3))
    service = BondReferenceService(client, session_factory=session_factory)
    try:
        task = service.schedule_populate_coupons(_ISIN)
        await task
    finally:
        await injected.aclose()

    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None
    coupons = await _fetch_coupons(session_factory, bond_id)
    assert len(coupons) == 3
    assert all(coupon_date.year >= 2026 for coupon_date, _ in coupons)
    assert any("inserted 3 rows" in record.message for record in caplog.records)


async def test_schedule_populate_coupons_swallows_provider_error(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider error inside the task is swallowed: task completes, warning logged."""
    bond_id = await _seed_bond(session_factory)
    await _seed_coupons(
        session_factory,
        bond_id,
        [datetime.date(2020, 1, 1), datetime.date(2020, 7, 1), datetime.date(2021, 1, 1)],
    )
    before = await _fetch_coupons(session_factory, bond_id)

    client, injected = _make_client(http_status=500)
    service = BondReferenceService(client, session_factory=session_factory)
    try:
        task = service.schedule_populate_coupons(_ISIN)
        await task
    finally:
        await injected.aclose()

    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None
    assert await _fetch_coupons(session_factory, bond_id) == before
    assert any("background populate_coupons" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_schedule_populate_coupons_raises_without_session_factory() -> None:
    """Without an injected session_factory, schedule_populate_coupons raises RuntimeError."""
    client, injected = _make_client(coupon_data=_COUPON_DATA)
    service = BondReferenceService(client)  # no session_factory
    try:
        with pytest.raises(RuntimeError, match="without a session_factory"):
            service.schedule_populate_coupons(_ISIN)
    finally:
        await injected.aclose()
