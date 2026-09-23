"""Thin async MOEX ISS client for bond reference search.

Live-request spike findings (2026-09-23, iss.moex.com) that shaped this
module — the originally designed single request against
``/iss/engines/stock/markets/bonds/securities.json?q=&limit=`` is NOT
usable because that endpoint ignores both ``q`` and ``limit`` (verified:
it always returns the full active-board bond list) and never contains
matured/delisted securities. The working, verified flow is:

1. ``GET /iss/securities.json?q=<query>&limit=<n>`` — the global search:
   filters server-side, honours ``limit`` and includes non-traded
   (``is_traded=0``) securities, so delisted/matured bonds stay findable.
   Lowercase columns; carries ``isin``, ``shortname``, ``name`` and the
   issuer title (``emitent_title``).
2. ``GET /iss/engines/stock/markets/bonds/securities/{secid}.json`` —
   full bond fields (UPPERCASE columns: ``FACEVALUE``, ``COUPONPERCENT``,
   ``COUPONPERIOD``, ``MATDATE``, ...) for a security with an active
   board row.
3. ``GET /iss/securities/{secid}.json`` — a ``description`` block
   (param/value pairs) that also works for matured securities when step 2
   yields no row; its ``COUPONFREQUENCY`` (coupons per year) is the only
   coupon hint available there.

``search_bonds`` combines the three: global search for candidates (bond
instruments only), then per-security detail requests, merged into
:class:`MoexBondRaw` rows with canonical UPPERCASE keys. All knowledge
about ISS URLs and response shapes lives in this module; the DTO field
interpretation lives in :mod:`bond_accounting.market_data.schemas`, so a
future field correction after a live request touches only these two
files.

Security: outbound HTTPS only to the configured ``base_url``; only the
search query travels to MOEX; no secrets are involved.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from bond_accounting.market_data.errors import (
    NoBondsFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from bond_accounting.market_data.schemas import (
    CouponScheduleEntry,
    coupon_schedule_from_block,
)

__all__ = ["CouponScheduleEntry", "MoexBondRaw", "MoexClient"]

logger = logging.getLogger(__name__)

#: Global ISS securities search (lowercase columns).
_SEARCH_PATH = "/iss/securities.json"

#: Bound on concurrent per-security detail requests issued by one search.
#: Keeps a large ``limit`` from firing 50 parallel HTTP calls at MOEX.
_DETAIL_CONCURRENCY_LIMIT = 10

#: Candidate cap when resolving an ISIN to its SECID for the bondization
#: endpoint (parity with the service's default search limit).
_COUPON_ISIN_SEARCH_LIMIT = 10

#: Bond fields for one security on the stock/bonds market (UPPERCASE columns).
_BOND_DETAIL_PATH = "/iss/engines/stock/markets/bonds/securities/{secid}.json"

#: Param/value description block for one security; works for delisted securities.
_DESCRIPTION_PATH = "/iss/securities/{secid}.json"

#: Bond coupon/amortization schedule statistics (bondization), keyed by SECID.
#: Live-verified 2026-09-23 (iss.moex.com): the ``coupons`` block carries the
#: actual payment schedule (``coupondate`` + cash amount per period) and is
#: paginated via a cursor block (default PAGESIZE=20).
_BONDIZATION_PATH = "/iss/statistics/engines/stock/markets/bonds/bondization/{secid}.json"

#: Block name holding the coupon schedule rows in a bondization response.
_BONDIZATION_COUPONS_BLOCK = "coupons"

#: Cursor block reporting ``[INDEX, TOTAL, PAGESIZE]`` for the coupons block.
_BONDIZATION_COUPONS_CURSOR = "coupons.cursor"

#: Default page size of the bondization coupons cursor (verified live).
_BONDIZATION_PAGE_SIZE = 20

#: Safety valve capping the number of bondization coupon pages fetched per
#: security. Real bond schedules are far below this (20 rows/page x 100 =
#: 2000 coupons); the bound only guards against a misbehaving cursor that
#: returns the same page for successive ``start`` values and would otherwise
#: loop forever.
_BONDIZATION_MAX_PAGES = 100

#: Canonical keys merged into every :class:`MoexBondRaw`.
_KEY_SECID = "SECID"
_KEY_ISIN = "ISIN"
_KEY_SHORTNAME = "SHORTNAME"
_KEY_SECNAME = "SECNAME"
_KEY_ISSUER = "ISSUER"


class MoexBondRaw(Mapping[str, Any]):
    """Read-only dict-like view of one raw MOEX ISS bond row.

    Keys are canonical UPPERCASE ISS column names (``ISIN``, ``SHORTNAME``,
    ``FACEVALUE``, ``COUPONPERCENT``, ``COUPONPERIOD``, ``MATDATE``,
    ``ISSUER``, ...). The type is intentionally opaque to callers: only
    :mod:`bond_accounting.market_data.schemas` interprets the fields, so a
    field-name correction stays inside the market_data module.
    """

    __slots__ = ("_fields",)

    def __init__(self, fields: Mapping[str, Any]) -> None:
        """Wrap a column-keyed ISS row (the mapping is copied)."""
        self._fields = dict(fields)

    def __getitem__(self, key: str) -> Any:
        return self._fields[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._fields!r})"


def _is_bond(search_row: Mapping[str, Any]) -> bool:
    """Return whether a global-search row describes a bond instrument."""
    group = str(search_row.get("group") or "")
    security_type = str(search_row.get("type") or "")
    return group == "stock_bonds" or security_type.endswith("_bond")


def _rows_of(payload: Any, block: str) -> list[dict[str, Any]]:
    """Extract ``block`` rows from an ISS response as column-keyed dicts.

    Raises:
        ProviderUnavailableError: The response is not an ISS JSON object with
            the expected ``block`` (``columns`` + ``data``) structure.
    """
    if not isinstance(payload, dict):
        raise ProviderUnavailableError(f"MOEX ISS response is not a JSON object (block {block!r})")
    section = payload.get(block)
    if not isinstance(section, dict):
        raise ProviderUnavailableError(f"MOEX ISS response is missing the {block!r} block")
    columns = section.get("columns")
    data = section.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise ProviderUnavailableError(f"MOEX ISS {block!r} block is malformed")
    rows: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, list):
            raise ProviderUnavailableError(
                f"MOEX ISS {block!r} block contains a malformed row: {entry!r}"
            )
        rows.append(dict(zip(columns, entry, strict=False)))
    return rows


def _cursor_total(payload: Any, cursor_block: str) -> int:
    """Read the ``TOTAL`` row count from an ISS cursor block.

    Returns ``0`` when the cursor is absent or malformed (callers then treat
    the single already-fetched page as the complete dataset, preferring a
    partial schedule over an error).
    """
    section = payload.get(cursor_block) if isinstance(payload, dict) else None
    if not isinstance(section, dict):
        return 0
    columns = section.get("columns")
    data = section.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        return 0
    total_index = None
    for index, name in enumerate(columns):
        if str(name).upper() == "TOTAL":
            total_index = index
            break
    if total_index is None or not data:
        return 0
    try:
        return int(data[0][total_index])
    except TypeError, ValueError, IndexError:
        return 0


class MoexClient:
    """Thin async client for MOEX ISS bond reference search.

    The instance owns its ``httpx.AsyncClient`` and must be released with
    :meth:`aclose` during application shutdown (the composition root does
    this before disposing of the database engine).
    """

    def __init__(self, base_url: str, timeout_s: float) -> None:
        """Configure the module-owned HTTP client.

        Args:
            base_url: MOEX ISS root URL, e.g. ``https://iss.moex.com``.
            timeout_s: Per-request timeout in seconds.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=timeout_s)
        # Cap concurrent per-security detail requests to avoid throttling
        # by the provider when a search returns many candidates.
        self._detail_semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY_LIMIT)

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` (graceful shutdown).

        Guards against the uvloop/httpcore teardown race where closing an
        already-closed transport raises ``RuntimeError`` ("the handler is
        closed") from ``write_eof`` — e.g. when a background request was in
        flight and its connection is torn down concurrently. Only that
        teardown error is swallowed (with a warning); anything else propagates.
        """
        try:
            await self._http.aclose()
        except RuntimeError:
            logger.warning(
                "MOEX client teardown hit an already-closed transport; ignoring", exc_info=True
            )

    async def search_bonds(self, query: str, limit: int = 10) -> list[MoexBondRaw]:
        """Search MOEX bonds matching ``query`` and return at most ``limit`` rows.

        Bond instruments only; no listing-status filtering — delisted and
        matured bonds stay findable. One uncached search performs 1 global
        search request plus one detail request per matching candidate (run
        concurrently).

        Args:
            query: Search string (ISIN, SECID or name fragment).
            limit: Maximum number of bonds to return.

        Returns:
            Raw rows with canonical UPPERCASE keys. An empty list means
            "no results", never "provider failure".

        Raises:
            ProviderTimeoutError: A MOEX ISS request timed out.
            ProviderUnavailableError: A MOEX ISS request failed, returned a
                non-200 status or a malformed response.
        """
        candidates = await self._search_candidates(query, limit)

        async def _load_row(row: Mapping[str, Any]) -> MoexBondRaw:
            async with self._detail_semaphore:
                return await self._load_bond_row(row)

        return list(await asyncio.gather(*(_load_row(row) for row in candidates)))

    async def fetch_coupon_schedule(self, isin: str) -> list[CouponScheduleEntry]:
        """Fetch a bond's coupon schedule from the MOEX bondization dataset.

        The bondization endpoint is keyed by SECID, not ISIN, so the ISIN is
        resolved to a SECID through the same global search that
        :meth:`search_bonds` uses (mirroring :meth:`_resolve_bond_secid` and the
        service-level case-insensitive exact ISIN match). The ``coupons`` block
        is paginated (cursor PAGESIZE=20); every page is fetched.

        Args:
            isin: ISIN of the bond whose schedule is requested.

        Returns:
            The coupon schedule, in the provider's order. An empty list means
            the provider reports no coupons for the bond — never a failure.

        Raises:
            NoBondsFoundError: No bond with this ISIN was found in the MOEX
                registry (so its SECID could not be resolved).
            ProviderTimeoutError: A MOEX ISS request timed out.
            ProviderUnavailableError: A MOEX ISS request failed, returned a
                non-200 status or a malformed response.
        """
        secid = await self._resolve_bond_secid(isin)
        return await self._fetch_bondization_coupons(secid)

    async def _resolve_bond_secid(self, isin: str) -> str:
        """Resolve an ISIN to its MOEX SECID via the global securities search.

        Matches the ISIN case-insensitively (parity with
        :meth:`BondReferenceService.get_by_isin`).

        Raises:
            NoBondsFoundError: No search candidate matched the ISIN.
        """
        candidates = await self._search_candidates(isin, limit=_COUPON_ISIN_SEARCH_LIMIT)
        folded = isin.casefold()
        for row in candidates:
            if (row.get("isin") or "").casefold() == folded:
                secid = row.get("secid")
                if isinstance(secid, str) and secid:
                    return secid
        raise NoBondsFoundError(f"No MOEX bond found for ISIN {isin!r}")

    async def _fetch_bondization_coupons(self, secid: str) -> list[CouponScheduleEntry]:
        """Read and paginate the ``coupons`` block of one bondization response.

        Iterates the cursor (``[INDEX, TOTAL, PAGESIZE]``) with ``start`` until
        the full schedule is collected. An empty block yields an empty list.
        Parsing of the raw rows into DTOs is delegated to
        :func:`coupon_schedule_from_block` in :mod:`market_data.schemas`, the
        single place where MOEX field changes are fixed.
        """
        path = _BONDIZATION_PATH.format(secid=secid)
        accumulated: list[dict[str, Any]] = []
        total = 0
        offset = 0
        pages_fetched = 0
        max_pages = _BONDIZATION_MAX_PAGES
        while pages_fetched < max_pages:
            payload = await self._get_json(path, params={"start": offset})
            page = _rows_of(payload, _BONDIZATION_COUPONS_BLOCK)
            accumulated.extend(page)
            total = _cursor_total(payload, _BONDIZATION_COUPONS_CURSOR)
            pages_fetched += 1
            # Stop when a page came back empty/partial relative to the total, or
            # the cursor reports nothing more (unknown total → fetch one page).
            if not page or not total or len(accumulated) >= total:
                break
            offset += len(page)
        if pages_fetched >= max_pages and len(accumulated) < total:
            logger.warning(
                "Bondization coupon fetch for secid=%s hit the page cap (%d); "
                "returning %d rows of %d total",
                secid,
                max_pages,
                len(accumulated),
                total,
            )
        return coupon_schedule_from_block(accumulated)

    async def _search_candidates(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Run the global ISS securities search and keep bond instruments."""
        payload = await self._get_json(_SEARCH_PATH, params={"q": query, "limit": limit})
        rows = _rows_of(payload, "securities")
        bonds = [row for row in rows if _is_bond(row)]
        if len(bonds) < len(rows):
            logger.debug(
                "MOEX search %r: skipped %d non-bond securities",
                query,
                len(rows) - len(bonds),
            )
        return bonds

    async def _load_bond_row(self, search_row: Mapping[str, Any]) -> MoexBondRaw:
        """Build one raw bond row for a global-search candidate.

        Prefers the bond-market detail endpoint (full fields for securities
        with an active board row); falls back to the description block for
        matured/delisted securities. The issuer title is only available in
        the global-search row and is always merged in.
        """
        secid = search_row.get("secid")
        if not isinstance(secid, str) or not secid:
            raise ProviderUnavailableError(f"MOEX search row is missing 'secid': {search_row!r}")

        detail_payload = await self._get_json(_BOND_DETAIL_PATH.format(secid=secid))
        rows = _rows_of(detail_payload, "securities")
        if rows:
            fields = dict(rows[0])
        else:
            fields = await self._description_fields(secid)

        fields.setdefault(_KEY_SECID, secid)
        fields.setdefault(_KEY_ISIN, search_row.get("isin"))
        fields.setdefault(_KEY_SHORTNAME, search_row.get("shortname"))
        fields.setdefault(_KEY_SECNAME, search_row.get("name"))
        fields.setdefault(_KEY_ISSUER, search_row.get("emitent_title"))
        return MoexBondRaw(fields)

    async def _description_fields(self, secid: str) -> dict[str, Any]:
        """Read the param/value description block of one security.

        Used only for securities without an active board row (matured/
        delisted bonds): the description endpoint still answers for them.
        """
        payload = await self._get_json(_DESCRIPTION_PATH.format(secid=secid))
        fields: dict[str, Any] = {}
        for row in _rows_of(payload, "description"):
            name = row.get("name")
            if isinstance(name, str) and name:
                fields[name] = row.get("value")
        # The description block calls the full security name "NAME"; the
        # bond-market rows call it "SECNAME" — normalize to the latter.
        if "NAME" in fields:
            fields.setdefault(_KEY_SECNAME, fields["NAME"])
        return fields

    async def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET ``path`` with ``params`` and return the decoded JSON payload.

        Raises:
            ProviderTimeoutError: The request timed out.
            ProviderUnavailableError: Transport error, non-200 status or
                invalid JSON.
        """
        try:
            response = await self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"MOEX ISS request to {path} timed out after {self._timeout_s} s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"MOEX ISS request to {path} failed: {exc}") from exc

        if response.status_code != 200:
            raise ProviderUnavailableError(
                f"MOEX ISS returned HTTP {response.status_code} for {path}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(f"MOEX ISS returned invalid JSON for {path}") from exc
