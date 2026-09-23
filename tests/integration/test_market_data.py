"""Integration tests for the ``market_data`` module (spec §6.2, SC-3).

``MoexClient.search_bonds`` is exercised against ``httpx.MockTransport`` —
no real network, no new dependencies (httpx is already a runtime
dependency). ``BondReferenceService`` (TTL cache, ``enabled`` flag,
``get_by_isin``) is tested with a stubbed ``MoexClient`` along with the
MOEX→DTO field mapping from :mod:`bond_accounting.market_data.schemas`.

Because ``MoexClient`` creates its own ``httpx.AsyncClient`` internally, the
tests swap its ``_http`` attribute for a transport-backed client built with
:class:`httpx.MockTransport` (the module owns construction, so this is the
non-invasive seam for isolation).
"""

from __future__ import annotations

import typing
from datetime import date

import httpx
import pytest

from bond_accounting.market_data import (
    BondReferenceService,
    MoexBondRaw,
    MoexClient,
    NoBondsFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    bond_reference_from_moex,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable

BASE_URL = "https://iss.moex.com"

#: Canonical search-response columns (lowercase; the flow the client hits).
_SEARCH_COLUMNS = ["secid", "isin", "shortname", "name", "emitent_title", "group", "type"]

#: Canonical bond-detail columns (UPPERCASE).
_DETAIL_COLUMNS = [
    "SECID",
    "ISIN",
    "SHORTNAME",
    "FACEVALUE",
    "COUPONPERCENT",
    "COUPONPERIOD",
    "MATDATE",
]


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[MoexClient, httpx.AsyncClient]:
    """Build a ``MoexClient`` whose ``_http`` uses ``MockTransport(handler)``.

    Returns ``(client, injected_client)``; the injected client must be
    closed by the caller (typically in a ``try/finally``) to avoid warnings.
    """
    transport = httpx.MockTransport(handler)
    injected = httpx.AsyncClient(base_url=BASE_URL, timeout=5.0, transport=transport)
    client = MoexClient(base_url=BASE_URL, timeout_s=5.0)
    client._http = injected  # type: ignore[attr-defined]
    return client, injected


def _search_payload() -> dict:
    """One global-search row describing a stock bond."""
    return {
        "securities": {
            "columns": _SEARCH_COLUMNS,
            "data": [
                [
                    "SU26238RMFS5",
                    "RU000A100XP0",
                    "ОФЗ 26238",
                    "ОФЗ 26238-ПД",
                    "Минфин РФ",
                    "stock_bonds",
                    "stock_bond",
                ]
            ],
        }
    }


def _detail_payload(rows: list[list]) -> dict:
    """``securities`` block with UPPERCASE bond-detail rows."""
    return {"securities": {"columns": _DETAIL_COLUMNS, "data": rows}}


# --------------------------------------------------------------------------- #
# MoexClient.search_bonds via MockTransport
# --------------------------------------------------------------------------- #


async def test_search_bonds_success_parses_and_maps_rows() -> None:
    """A normal two-request search returns fully merged raw bond rows."""
    seen = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.add(request.url.path)
        if request.url.path == "/iss/securities.json":
            return httpx.Response(200, json=_search_payload())
        if "/markets/bonds/securities/" in request.url.path:
            return httpx.Response(
                200,
                json=_detail_payload(
                    [
                        [
                            "SU26238RMFS5",
                            "RU000A100XP0",
                            "ОФЗ 26238",
                            "1000",
                            "7.5",
                            "182",
                            "2041-05-15",
                        ]
                    ]
                ),
            )
        return httpx.Response(404, json={})

    client, injected = _mock_client(handler)
    try:
        rows = await client.search_bonds("SU26238", limit=10)
    finally:
        await injected.aclose()

    assert "/iss/securities.json" in seen
    assert any("/markets/bonds/securities/" in path for path in seen)
    assert len(rows) == 1
    ref = bond_reference_from_moex(rows[0])
    assert ref is not None
    assert ref.isin == "RU000A100XP0"
    assert ref.name == "ОФЗ 26238"
    assert ref.nominal == 1000
    assert ref.coupon_rate == 7.5
    assert ref.coupon_period_days == 182
    assert ref.maturity_date == date(2041, 5, 15)
    assert ref.issuer == "Минфин РФ"


async def test_search_bonds_timeout_raises_provider_timeout() -> None:
    """A response whose transport raises a timeout produces
    ``ProviderTimeoutError``.

    ``httpx.MockTransport`` dispatches directly to the handler and does not
    measure wall-clock read timeouts against it, so a genuinely slow handler
    would not trigger one deterministically. Instead the handler raises
    :class:`httpx.ReadTimeout` — exactly what a real slow response exceeding
    the configured ``timeout_s`` surfaces as — and the client must translate
    it into :class:`ProviderTimeoutError`.
    """

    def slow_handler(request: httpx.Request) -> httpx.Response:
        # Simulate the slow-response case: the provider exceeds the timeout,
        # which the transport reports as a read timeout.
        raise httpx.ReadTimeout("timed out waiting for MOEX response")

    transport = httpx.MockTransport(slow_handler)
    injected = httpx.AsyncClient(base_url=BASE_URL, timeout=0.05, transport=transport)
    client = MoexClient(base_url=BASE_URL, timeout_s=0.05)
    client._http = injected  # type: ignore[attr-defined]
    try:
        with pytest.raises(ProviderTimeoutError):
            await client.search_bonds("SU26238", limit=10)
    finally:
        await injected.aclose()


async def test_search_bonds_http_500_raises_provider_unavailable() -> None:
    """A non-200 provider response is a ``ProviderUnavailableError`` (never an
    empty list)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    client, injected = _mock_client(handler)
    try:
        with pytest.raises(ProviderUnavailableError):
            await client.search_bonds("SU26238", limit=10)
    finally:
        await injected.aclose()


async def test_search_bonds_invalid_json_raises_provider_unavailable() -> None:
    """Malformed (non-JSON) provider body is a ``ProviderUnavailableError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    client, injected = _mock_client(handler)
    try:
        with pytest.raises(ProviderUnavailableError):
            await client.search_bonds("SU26238", limit=10)
    finally:
        await injected.aclose()


async def test_search_bonds_malformed_payload_raises_provider_unavailable() -> None:
    """A well-formed transport response with a malformed ISS object (missing the
    expected ``securities`` block) is a provider error, not an empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": {}})

    client, injected = _mock_client(handler)
    try:
        with pytest.raises(ProviderUnavailableError):
            await client.search_bonds("SU26238", limit=10)
    finally:
        await injected.aclose()


# --------------------------------------------------------------------------- #
# BondReferenceService: TTL cache & enabled flag
# --------------------------------------------------------------------------- #


class _StubClient(MoexClient):
    """Stub ``MoexClient.search_bonds`` recording calls and returning rows.

    Subclasses :class:`MoexClient` only to satisfy the ``client`` type
    expected by :class:`BondReferenceService`; ``search_bonds`` is fully
    overridden so no provider request is ever issued.
    """

    def __init__(self, rows: list[MoexBondRaw]) -> None:
        super().__init__(BASE_URL, timeout_s=5.0)
        self.rows = rows
        self.calls: list[tuple[str, int]] = []

    async def search_bonds(self, query: str, limit: int = 10) -> list[MoexBondRaw]:
        self.calls.append((query, limit))
        return list(self.rows)


def _raw_row(
    isin: str = "RU000A100XP0",
    period: int = 182,
    matdate: str = "2041-05-15",
) -> MoexBondRaw:
    """A minimal ``MoexBondRaw`` ready for ``bond_reference_from_moex``."""
    return MoexBondRaw(
        {
            "ISIN": isin,
            "SHORTNAME": "ОФЗ 26238",
            "FACEVALUE": "1000",
            "COUPONPERCENT": "7.5",
            "COUPONPERIOD": str(period),
            "MATDATE": matdate,
            "ISSUER": "Минфин РФ",
        }
    )


async def test_search_cache_hit_within_ttl() -> None:
    """A second search within the TTL is served from the cache (one client call)."""
    stub = _StubClient([_raw_row()])
    service = BondReferenceService(stub, cache_ttl_s=300)

    first = await service.search("SU26238")
    second = await service.search("SU26238")

    assert stub.calls == [("SU26238", 10)]
    assert first == second
    assert len(first) == 1
    assert first[0].isin == "RU000A100XP0"


async def test_search_cache_miss_after_expiry() -> None:
    """Re-searching after the cached entry expires hits the provider again."""
    stub = _StubClient([_raw_row()])
    service = BondReferenceService(stub, cache_ttl_s=300)

    await service.search("SU26238")
    assert len(stub.calls) == 1

    # Force expiry deterministically (rewind the stored expiry stamp past TTL).
    key = ("SU26238", 10)
    entry = service._cache[key]  # type: ignore[attr-defined]
    service._cache[key] = (entry[0] - 1000.0, entry[1])  # type: ignore[attr-defined]

    await service.search("SU26238")
    assert len(stub.calls) == 2


async def test_search_disabled_raises_and_never_calls_client() -> None:
    """``enabled=False`` raises ``ProviderUnavailableError`` before any I/O."""
    stub = _StubClient([_raw_row()])
    service = BondReferenceService(stub, enabled=False)

    with pytest.raises(ProviderUnavailableError):
        await service.search("SU26238")
    assert stub.calls == []


async def test_get_by_isin_disabled_raises() -> None:
    """``get_by_isin`` on a disabled integration raises ``ProviderUnavailableError``."""
    stub = _StubClient([_raw_row()])
    service = BondReferenceService(stub, enabled=False)

    with pytest.raises(ProviderUnavailableError):
        await service.get_by_isin("RU000A100XP0")
    assert stub.calls == []


# --------------------------------------------------------------------------- #
# BondReferenceService.get_by_isin
# --------------------------------------------------------------------------- #


async def test_get_by_isin_exact_match() -> None:
    service = BondReferenceService(_StubClient([_raw_row()]), cache_ttl_s=300)

    ref = await service.get_by_isin("RU000A100XP0")

    assert ref.isin == "RU000A100XP0"
    assert ref.name == "ОФЗ 26238"


async def test_get_by_isin_case_insensitive_match() -> None:
    service = BondReferenceService(_StubClient([_raw_row()]), cache_ttl_s=300)

    ref = await service.get_by_isin("ru000a100xp0")

    assert ref.isin == "RU000A100XP0"


async def test_get_by_isin_not_found_raises_no_bonds_found() -> None:
    service = BondReferenceService(_StubClient([]), cache_ttl_s=300)

    with pytest.raises(NoBondsFoundError):
        await service.get_by_isin("RU000ZZZZZZZ")


# --------------------------------------------------------------------------- #
# Field mapping cases (spec §6.2)
# --------------------------------------------------------------------------- #


def test_mapping_couponperiod_zero_maps_to_zero_days() -> None:
    """``COUPONPERIOD=0`` (zero-coupon) maps to ``coupon_period_days=0``."""
    ref = bond_reference_from_moex(_raw_row(period=0, matdate="2041-05-15"))
    assert ref is not None
    assert ref.coupon_period_days == 0


def test_mapping_compact_matdate_parsed_to_date() -> None:
    """The compact ``YYYYMMDD`` ``MATDATE`` format is accepted as a fallback."""
    ref = bond_reference_from_moex(_raw_row(matdate="20410515"))
    assert ref is not None
    assert ref.maturity_date == date(2041, 5, 15)


def test_mapping_facevalue_coerced_to_int() -> None:
    """``FACEVALUE`` (a numeric string from ISS) maps to an ``int`` ``nominal``."""
    ref = bond_reference_from_moex(_raw_row())
    assert ref is not None
    assert ref.nominal == 1000
    assert isinstance(ref.nominal, int)


def test_mapping_from_frequency_when_no_period() -> None:
    """Without ``COUPONPERIOD``, ``coupon_period_days`` is derived from
    ``COUPONFREQUENCY`` (e.g. 2/year → 182 days) and the rate is unknown
    (``0.0``) per the description-block path."""
    row = MoexBondRaw(
        {
            "ISIN": "RU000A100XP0",
            "SHORTNAME": "ОФЗ 26238",
            "FACEVALUE": "1000",
            "COUPONFREQUENCY": "2",
            "MATDATE": "2041-05-15",
            "ISSUER": "Минфин РФ",
        }
    )
    ref = bond_reference_from_moex(row)
    assert ref is not None
    assert ref.coupon_period_days == 182
    assert ref.coupon_rate == 0.0  # rate unknown in the description-block path
