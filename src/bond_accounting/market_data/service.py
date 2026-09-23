"""Bond reference service: MOEX search, DTO mapping and a TTL cache.

Wraps :class:`~bond_accounting.market_data.client.MoexClient` with the
``BondReference`` mapping and an in-memory TTL cache keyed per
``(query, limit)``, preventing repeated ISS hits while the user types
(the UI debounces at 300 ms and re-searches on every keystroke).

Error contract (spec §5): a disabled integration raises
:class:`ProviderUnavailableError` from both public methods — before any
cache or provider access — so the REST layer can answer 503 and the UI
show its static hint; a missing ISIN raises :class:`NoBondsFoundError`
(REST 404).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from bond_accounting.db.models import Bond, BondCoupon
from bond_accounting.market_data.errors import NoBondsFoundError, ProviderUnavailableError
from bond_accounting.market_data.schemas import BondReference, bond_reference_from_moex

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.market_data.client import MoexClient

__all__ = ["BondReferenceService"]

logger = logging.getLogger(__name__)

#: Cache-entry value: (expiry monotonic timestamp, mapped references).
_CacheEntry = tuple[float, list[BondReference]]


class BondReferenceService:
    """Bond reference search facade over the MOEX ISS client.

    Attributes:
        enabled: Whether the integration is active. Read directly by the
            UI to render the static "reference disabled" hint; when
            ``False`` both public methods raise
            :class:`ProviderUnavailableError` before touching the cache or
            the provider.
    """

    def __init__(
        self,
        client: MoexClient,
        cache_ttl_s: int = 300,
        enabled: bool = True,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        """Wire the service.

        Args:
            client: The MOEX ISS client used for provider requests.
            cache_ttl_s: Lifetime of cached search results, in seconds.
            enabled: Master switch for the integration.
            session_factory: Optional factory of ``AsyncSession`` objects used
                only by :meth:`schedule_populate_coupons` to open its own
                short-lived session. Backward-compatible: ``None`` disables the
                fire-and-forget wrapper (which then raises ``RuntimeError``).
        """
        self._client = client
        self._cache_ttl_s = cache_ttl_s
        self.enabled = enabled
        self._cache: dict[tuple[str, int], _CacheEntry] = {}
        self._session_factory = session_factory
        # Registry of live fire-and-forget populate tasks so shutdown can
        # settle them deterministically before the shared HTTP client closes.
        self._populate_tasks: set[asyncio.Task[None]] = set()

    async def search(self, query: str, limit: int = 10) -> list[BondReference]:
        """Search the MOEX bond reference registry.

        Results are cached per ``(query, limit)`` for ``cache_ttl_s``
        seconds. An empty list means "no results", never "provider
        failure". Expired entries are dropped opportunistically on insert
        so the cache cannot grow without bound as the user types.

        Args:
            query: Search string (ISIN, SECID or name fragment).
            limit: Maximum number of references to return.

        Returns:
            Matching bond references (possibly empty).

        Raises:
            ProviderUnavailableError: The integration is disabled or the
                provider failed / returned a malformed response.
            ProviderTimeoutError: The provider timed out.
        """
        self._ensure_enabled()
        key = (query, limit)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._cache_ttl_s:
            return cached[1]

        raw_rows = await self._client.search_bonds(query, limit=limit)
        references = [
            reference
            for row in raw_rows
            if (reference := bond_reference_from_moex(row)) is not None
        ]
        self._prune_expired(now)
        self._cache[key] = (now, references)
        return references

    def _prune_expired(self, now: float) -> None:
        """Drop cache entries whose TTL has elapsed.

        Called before inserting a new entry so every distinct
        ``(query, limit)`` cannot accumulate entries forever.
        """
        for existing_key, (expiry, _) in list(self._cache.items()):
            if now - expiry >= self._cache_ttl_s:
                del self._cache[existing_key]

    async def get_by_isin(self, isin: str) -> BondReference:
        """Fetch one bond reference by exact (case-insensitive) ISIN match.

        Implemented as an ISS search by ISIN followed by a case-insensitive
        exact match on the results, so delisted/matured bonds remain
        findable.

        Args:
            isin: ISIN to look up.

        Returns:
            The reference for the requested ISIN.

        Raises:
            ProviderUnavailableError: The integration is disabled or the
                provider failed / returned a malformed response.
            ProviderTimeoutError: The provider timed out.
            NoBondsFoundError: No bond with this ISIN exists in the MOEX
                registry.
        """
        self._ensure_enabled()
        for reference in await self.search(isin):
            if reference.isin.casefold() == isin.casefold():
                return reference
        raise NoBondsFoundError(f"No MOEX bond found for ISIN {isin!r}")

    async def populate_coupons(self, isin: str, session: AsyncSession) -> int:
        """Replace a bond's coupon schedule with the MOEX bondization schedule.

        Idempotent: on success the existing ``BondCoupon`` rows for the bond are
        deleted and the fetched schedule is inserted in one unit of work on the
        passed ``session`` (which this method commits itself, matching the
        codebase's services-own-their-unit-of-work style). An empty schedule is
        a no-op: no rows are removed and ``0`` is returned, preserving existing
        data (user decision 2026-09-23). Provider failures propagate without any
        writes.

        Args:
            isin: ISIN of the bond whose schedule is replaced.
            session: The ``AsyncSession`` on which the replacement is performed
                and committed.

        Returns:
            The number of coupon rows inserted.

        Raises:
            ProviderUnavailableError: The integration is disabled or the
                provider failed / returned a malformed response.
            ProviderTimeoutError: The provider timed out.
            NoBondsFoundError: The bond is not in the local database, or it
                cannot be resolved in the MOEX registry.
        """
        self._ensure_enabled()
        bond = await session.scalar(select(Bond).where(func.lower(Bond.isin) == isin.casefold()))
        if bond is None:
            raise NoBondsFoundError(f"No bond with ISIN {isin!r} in the local database")

        entries = await self._client.fetch_coupon_schedule(isin)
        if not entries:
            # Empty schedule → keep existing rows (idempotent no-op).
            return 0

        await session.execute(delete(BondCoupon).where(BondCoupon.bond_id == bond.id))
        session.add_all(
            BondCoupon(
                bond_id=bond.id,
                coupon_date=entry.coupon_date,
                coupon_amount=entry.coupon_amount,
            )
            for entry in entries
        )
        await session.commit()
        return len(entries)

    def schedule_populate_coupons(self, isin: str) -> asyncio.Task[None]:
        """Fire-and-forget :meth:`populate_coupons` in its own session.

        Opens a session from the injected ``session_factory`` and runs
        :meth:`populate_coupons`; every exception is swallowed and warning-
        logged so the wrapper never raises into the caller (provider errors for
        unknown/newly-created bonds are expected and harmless).

        Args:
            isin: ISIN of the bond to populate.

        Returns:
            The created :class:`asyncio.Task`; failures inside it are reported
            only via logging.

        Raises:
            RuntimeError: No ``session_factory`` was injected into the service.
        """
        if self._session_factory is None:
            raise RuntimeError(
                "BondReferenceService was constructed without a session_factory; "
                "schedule_populate_coupons is unavailable"
            )
        task = asyncio.create_task(self._run_populate_coupons(isin))
        # Keep a strong reference (create_task only keeps a weak one) and drop
        # it as soon as the task settles; the registry powers shutdown().
        self._populate_tasks.add(task)
        task.add_done_callback(self._populate_tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Cancel outstanding background populate tasks and await their settlement.

        Deterministic-teardown counterpart to :meth:`schedule_populate_coupons`,
        called by the composition root before closing the shared MOEX client
        so no populate task still holds an HTTP connection while the transport
        is being closed (fix-teardown-race). Cancellation is awaited through
        ``gather(..., return_exceptions=True)`` so nothing escapes; tasks that
        already settled on their own are simply dropped from the registry by
        their done callback. Fire-and-forget semantics for callers are
        unchanged — this method is only for shutdown.
        """
        outstanding = [task for task in self._populate_tasks if not task.done()]
        for task in outstanding:
            task.cancel()
        if outstanding:
            await asyncio.gather(*outstanding, return_exceptions=True)

    async def _run_populate_coupons(self, isin: str) -> None:
        """Coroutine behind :meth:`schedule_populate_coupons`."""
        session_factory = self._session_factory
        if session_factory is None:
            # Unreachable in practice (schedule_populate_coupons raises before
            # creating the task when the factory is None). Kept as a local so
            # the type checker narrows ``async_sessionmaker[AsyncSession] | None``.
            raise RuntimeError("BondReferenceService was constructed without a session_factory")
        async with session_factory() as session:
            try:
                inserted = await self.populate_coupons(isin, session)
            except Exception as exc:
                logger.warning("background populate_coupons(isin=%r) failed: %s", isin, exc)
            else:
                logger.info("background populate_coupons(isin=%r) inserted %d rows", isin, inserted)

    def _ensure_enabled(self) -> None:
        """Raise when the integration is disabled (before cache/provider access)."""
        if not self.enabled:
            raise ProviderUnavailableError("market_data integration is disabled by config")
