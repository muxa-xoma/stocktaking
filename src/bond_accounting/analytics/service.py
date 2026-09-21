"""Analytics service: portfolio summary, per-position metrics, events.

Aggregates a user's transaction history into a
:class:`~bond_accounting.analytics.dto.PortfolioSummary` (open positions,
yields, accrued coupons, realized PnL, upcoming cashflows).

Caching strategy (event-driven, in-memory):

* Read methods (:meth:`AnalyticsService.get_portfolio_summary`,
  :meth:`AnalyticsService.get_position_analytics`) serve a per-user summary
  from an in-process dictionary cache; a cache miss computes and stores the
  summary lazily. Read paths never publish events.
* Recalculation happens only when a change event arrives (see
  :func:`attach_to_event_bus`): ``transaction.created``, ``bond.updated`` /
  ``bond.deleted`` (fanned out per holder, with ``user_id`` in the payload)
  and ``position.updated``. The handler recomputes the affected user's
  summary via :meth:`AnalyticsService.recalculate`, refreshes the cache and
  publishes ``portfolio.recalculated``.
* Staleness window: a cached summary is fresh up to the last processed
  event for that user; reading never triggers a recalculation.
* The cache is per-process: when the app runs under multiprocessing, each
  worker keeps its own cache (acceptable for a small user base; entries
  converge after the next change event or cold-start fill).
* ``next_coupons`` and ``upcoming_cashflows`` are user-parameterizable
  (horizon in days / event limit; defaults 730/100 and 3650/500
  respectively). The parameterized sections are rebuilt on the fly from
  the cached summary's full calendars — ``upcoming_cashflows`` carries the
  complete coupon + principal calendar, uncapped — so they are neither
  cached nor published; the cache key stays ``("summary", user_id)`` and
  no parameter-dependent entries accumulate. The full calendar in the
  cached/published summary remains unparameterized (to maturity).

Reactivity is opt-in: the constructor does not subscribe to the event bus.
Call :func:`attach_to_event_bus` to wire the service to the
``bond.updated`` / ``bond.deleted`` / ``transaction.created`` /
``position.updated`` topics.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from bond_accounting.analytics.dto import (
    CASHFLOWS_HORIZON_DAYS_DEFAULT,
    CASHFLOWS_HORIZON_DAYS_MAX,
    CASHFLOWS_HORIZON_DAYS_MIN,
    CASHFLOWS_LIMIT_DEFAULT,
    CASHFLOWS_LIMIT_MAX,
    CASHFLOWS_LIMIT_MIN,
    NEXT_COUPONS_HORIZON_DAYS_DEFAULT,
    NEXT_COUPONS_HORIZON_DAYS_MAX,
    NEXT_COUPONS_HORIZON_DAYS_MIN,
    NEXT_COUPONS_LIMIT_DEFAULT,
    NEXT_COUPONS_LIMIT_MAX,
    NEXT_COUPONS_LIMIT_MIN,
    Cashflow,
    CouponDue,
    PortfolioSummary,
    PositionAnalytics,
    RealizedPnl,
)
from bond_accounting.db.models import Bond, Transaction
from bond_accounting.event_bus import Topic
from bond_accounting.portfolio.netting import net_position, realized_pnl
from bond_accounting.yield_calc import (
    YtmCalculationError,
    calculate_accrued_coupon,
    calculate_current_yield,
    calculate_ytm,
)
from bond_accounting.yield_calc.coupon_schedule import _add_months

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.event_bus import EventBus, Message

logger = logging.getLogger(__name__)

#: ``sender`` value stamped on every message published by this module.
_SENDER = "analytics"

#: Months per coupon period (calendar grid anchored on the maturity date).
_FREQUENCY_MONTHS: dict[str, int] = {
    "ANNUAL": 12,
    "SEMI_ANNUAL": 6,
    "QUARTERLY": 3,
}

#: Coupon periods per year for each supported frequency.
_PERIODS_PER_YEAR: dict[str, int] = {
    "ANNUAL": 1,
    "SEMI_ANNUAL": 2,
    "QUARTERLY": 4,
}

#: ``next_coupons`` horizon for the cached/published summary: coupon dates
#: further than ~24 months from ``today`` are dropped. Users may request a
#: different horizon (1..3650 days) — see :meth:`get_portfolio_summary`.
_NEXT_COUPONS_HORIZON: timedelta = timedelta(days=NEXT_COUPONS_HORIZON_DAYS_DEFAULT)

#: ``next_coupons`` cap for the cached/published summary: at most this many
#: events are returned. Users may request a different cap (1..1000).
_NEXT_COUPONS_MAX_EVENTS = NEXT_COUPONS_LIMIT_DEFAULT

#: Defensive floor for backward coupon-grid stepping; below it there is no
#: meaningful last coupon date and the accrued coupon is ``0.0``.
_MIN_COUPON_DATE = date(1900, 1, 1)

#: Topics that trigger a portfolio recalculation via
#: :func:`attach_to_event_bus`.
_CHANGE_TOPICS = (
    Topic.BOND_UPDATED,
    Topic.BOND_DELETED,
    Topic.TRANSACTION_CREATED,
    Topic.POSITION_UPDATED,
)


# --------------------------------------------------------------------- #
# pure calculation helpers (module-level for unit-testability)
# --------------------------------------------------------------------- #


def _future_coupon_dates(maturity_date: date, months: int, today: date) -> list[date]:
    """Coupon grid dates strictly after ``today``, ending with the maturity.

    Mirrors the ``yield_calc`` grid semantics: dates are produced by
    stepping one coupon period back from ``maturity_date`` while staying
    strictly after ``today`` (day-of-month clamped by ``_add_months``).

    Args:
        maturity_date: Bond maturity date.
        months: Months per coupon period.
        today: Valuation date.

    Returns:
        Chronologically ordered future coupon dates; the last entry is the
        maturity date itself (it doubles as the final coupon date). Empty
        when ``maturity_date <= today``.
    """
    dates: list[date] = []
    current = maturity_date
    while current > today:
        dates.append(current)
        current = _add_months(current, -months)
    dates.reverse()
    return dates


def _last_coupon_date(maturity_date: date, months: int, today: date) -> date | None:
    """Latest coupon grid date strictly before ``today``, or ``None``.

    Args:
        maturity_date: Bond maturity date.
        months: Months per coupon period.
        today: Valuation date.

    Returns:
        The most recent coupon date before ``today`` on the grid anchored
        on ``maturity_date``; ``None`` only for degenerate data (stepping
        below :data:`_MIN_COUPON_DATE`), in which case the accrued coupon
        is ``0.0``.
    """
    current = maturity_date
    while current >= today:
        current = _add_months(current, -months)
        if current < _MIN_COUPON_DATE:
            return None
    return current


def _coupon_per_period(bond: Bond) -> float:
    """Coupon amount per period, per one bond unit.

    Args:
        bond: Bond instrument.

    Returns:
        ``nominal * coupon_rate / 100 / periods_per_year``.
    """
    return bond.nominal * bond.coupon_rate / 100 / _PERIODS_PER_YEAR[bond.coupon_frequency]


def _position_of(txns: Sequence[Transaction]) -> tuple[int, float | None]:
    """Derive ``(quantity, avg_buy_price)`` from a bond's transactions.

    Thin wrapper over :func:`bond_accounting.portfolio.netting.net_position`,
    kept as a module-level helper so the unit tests can exercise the
    analytics-side aggregation directly.

    Args:
        txns: One bond's transactions, ordered by ``(date, id)``.

    Returns:
        The held quantity (BUY minus SELL minus MATURE) and the
        average-cost price of the currently open position (BUYs add to the
        cost basis, SELL/MATURE reduce it proportionally, a full close
        resets it) — or ``None`` when the position is flat.
    """
    return net_position(txns)


def _realized_pnl_of(txns: Sequence[Transaction]) -> tuple[float, float, float]:
    """Realized PnL contributions ``(sells, maturities, commissions)``.

    Thin wrapper over
    :func:`bond_accounting.portfolio.netting.realized_pnl`, kept as a
    module-level helper so the unit tests can exercise the
    analytics-side aggregation directly.

    Average-cost semantics (the same state machine as :func:`_position_of`):
    BUYs add to the cost basis, SELL/MATURE reduce it proportionally, and
    a full close resets it — so an exit after a reopen is priced against
    the post-reopen average only, never blended with the pre-close
    history. An exit without any open position contributes 0 PnL (logged
    as a warning). Commissions of all SELL/MATURE rows are summed
    regardless.

    Args:
        txns: One bond's transactions, ordered by ``(date, id)``.

    Returns:
        Tuple ``(sells, maturities, commissions)`` where ``sells`` is the
        sum of ``(price - avg_cost_at_that_time) * closed_quantity`` over
        SELL transactions and ``maturities`` is the same over MATURE.
    """
    return realized_pnl(txns)


def _build_position_analytics(
    bond: Bond, txns: Sequence[Transaction], today: date
) -> PositionAnalytics:
    """Compute :class:`PositionAnalytics` for one open position.

    Args:
        bond: Bond instrument.
        txns: The bond's full transaction history for the user, ordered by
            ``(date, id)``. The position must be open (``quantity > 0``),
            which also guarantees a non-``None`` ``avg_buy_price``.
        today: Valuation date.

    Returns:
        Per-position analytics; yields are ``None`` for matured bonds.
    """
    quantity, avg_buy_price = _position_of(txns)
    if avg_buy_price is None:
        # An open position implies at least one BUY; treat anything else as a
        # corrupt history rather than silently computing on None.
        raise ValueError(f"Open position for bond_id={bond.id} has no BUY transactions to average")
    months = _FREQUENCY_MONTHS[bond.coupon_frequency]
    matured = bond.maturity_date <= today

    future_dates = [] if matured else _future_coupon_dates(bond.maturity_date, months, today)
    next_coupon_date = future_dates[0] if future_dates else None

    ytm: float | None = None
    current_yield: float | None = None
    if not matured:
        try:
            ytm = calculate_ytm(
                price=avg_buy_price,
                coupon_rate=bond.coupon_rate,
                coupon_frequency=bond.coupon_frequency,
                maturity_date=bond.maturity_date,
                nominal=bond.nominal,
                today=today,
            )
            current_yield = calculate_current_yield(
                price=avg_buy_price,
                coupon_rate=bond.coupon_rate,
                nominal=bond.nominal,
            )
        except YtmCalculationError:
            logger.warning(
                "Yield calculation failed for bond_id=%s (avg_buy_price=%.4f); yields set to None",
                bond.id,
                avg_buy_price,
            )

    last_coupon = _last_coupon_date(bond.maturity_date, months, today)
    if last_coupon is None:
        accrued_coupon = 0.0
    else:
        try:
            accrued_coupon = calculate_accrued_coupon(
                bond.coupon_rate,
                bond.coupon_frequency,
                last_coupon,
                today=today,
                nominal=bond.nominal,
            )
        except ValueError:
            logger.warning("Accrued coupon failed for bond_id=%s; defaulting to 0.0", bond.id)
            accrued_coupon = 0.0

    return PositionAnalytics(
        bond_id=bond.id,
        isin=bond.isin,
        name=bond.name,
        quantity=quantity,
        avg_buy_price=avg_buy_price,
        total_invested=avg_buy_price * quantity,
        ytm=ytm,
        current_yield=current_yield,
        accrued_coupon=accrued_coupon,
        next_coupon_date=next_coupon_date,
        maturity_date=bond.maturity_date,
    )


def _next_coupons_from_summary(
    summary: PortfolioSummary, base: date, horizon_days: int, limit: int
) -> list[CouponDue]:
    """Derive ``next_coupons`` from the summary's full coupon calendar.

    ``upcoming_cashflows`` carries every future coupon of every open
    position with no horizon cap, so it doubles as the full coupon
    calendar; ``next_coupons`` is rebuilt from it on demand with the
    caller's horizon/limit while keeping the ``(date, bond_id)`` sort —
    the same policy :func:`_compute_summary` bakes in with its defaults.

    Args:
        summary: Summary whose ``upcoming_cashflows`` / ``positions`` are
            used as the calendar and the bond-name lookup respectively.
        base: Date the horizon counts from (the valuation date).
        horizon_days: Include coupon dates at most this many days after
            ``base``.
        limit: Keep at most this many events (after sorting).

    Returns:
        Coupon events within the horizon, capped at ``limit`` events.
    """
    names = {position.bond_id: position.name for position in summary.positions}
    horizon_end = base + timedelta(days=horizon_days)
    coupons = [
        CouponDue(
            bond_id=flow.bond_id,
            isin=flow.isin,
            # Every COUPON cashflow belongs to an open position, so the
            # name is always found; the isin fallback is purely defensive.
            name=names.get(flow.bond_id, flow.isin),
            date=flow.date,
            amount=flow.amount,
        )
        for flow in summary.upcoming_cashflows
        if flow.kind == "COUPON" and flow.date <= horizon_end
    ]
    coupons.sort(key=lambda due: (due.date, due.bond_id))
    return coupons[:limit]


def _cashflows_from_summary(
    summary: PortfolioSummary, base: date, horizon_days: int, limit: int
) -> list[Cashflow]:
    """Derive ``upcoming_cashflows`` from the summary's full cashflow calendar.

    The cached/published summary carries the complete coupon + principal
    calendar (to maturity, uncapped) — which :func:`_next_coupons_from_summary`
    also relies on — so this helper rebuilds the user-facing section on
    demand with the caller's horizon/limit while keeping the
    ``(date, bond_id)`` sort.

    Args:
        summary: Summary whose ``upcoming_cashflows`` is the full calendar.
        base: Date the horizon counts from (the valuation date).
        horizon_days: Include cashflow dates at most this many days after
            ``base``.
        limit: Keep at most this many events (after sorting).

    Returns:
        Cashflow events within the horizon, capped at ``limit`` events.
    """
    horizon_end = base + timedelta(days=horizon_days)
    flows = [flow for flow in summary.upcoming_cashflows if flow.date <= horizon_end]
    flows.sort(key=lambda flow: (flow.date, flow.bond_id))
    return flows[:limit]


class AnalyticsService:
    """Portfolio analytics aggregation with an event-driven in-memory cache.

    The cache maps ``("summary", user_id)`` to the user's
    :class:`~bond_accounting.analytics.dto.PortfolioSummary`. Read methods
    (:meth:`get_portfolio_summary`, :meth:`get_position_analytics`) return
    cached values — computing lazily on a miss — and never publish events.
    :meth:`recalculate` (invoked from the handlers installed by
    :func:`attach_to_event_bus`) recomputes a user's summary, refreshes the
    cache and publishes ``portfolio.recalculated``.

    The cache is per-process: under multiprocessing each worker keeps its
    own copy (acceptable for a small user base; entries converge on the
    next change event or cold-start fill). Cache access is guarded by an
    ``asyncio.Lock`` because GET requests and event-bus dispatcher tasks
    interleave on the same event loop; the lock covers only dictionary
    reads/writes — computation happens outside it, so a cold-start race
    may compute the same summary twice (last write wins) instead of
    serializing all users behind one lock.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], event_bus: EventBus
    ) -> None:
        """Create the service.

        Args:
            session_factory: Factory producing ``AsyncSession`` objects
                (``expire_on_commit=False`` recommended).
            event_bus: Bus used by :meth:`recalculate` to publish
                ``portfolio.recalculated`` events; must be started by the
                caller. The service does not subscribe on its own — see
                :func:`attach_to_event_bus`.
        """
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._cache_lock = asyncio.Lock()
        self._summary_cache: dict[tuple[str, int], PortfolioSummary] = {}

    # ------------------------------------------------------------------ #
    # queries

    async def get_portfolio_summary(
        self,
        user_id: int,
        today: date | None = None,
        horizon_days: int = NEXT_COUPONS_HORIZON_DAYS_DEFAULT,
        limit: int = NEXT_COUPONS_LIMIT_DEFAULT,
        cashflows_horizon_days: int = CASHFLOWS_HORIZON_DAYS_DEFAULT,
        cashflows_limit: int = CASHFLOWS_LIMIT_DEFAULT,
    ) -> PortfolioSummary:
        """Return the user's portfolio summary; only open positions.

        Serves the summary from the per-user in-memory cache (lazily
        computed and stored on a miss). The returned model is a deep copy,
        so callers cannot mutate the cached entry. Read paths never publish
        events and never trigger recalculation.

        An explicit ``today`` bypasses the cache entirely (deterministic
        valuation for tests / exports): the summary is computed on the
        fly and is neither cached nor published.

        ``next_coupons`` and ``upcoming_cashflows`` are user-parameterizable
        via ``horizon_days`` / ``limit`` and ``cashflows_horizon_days`` /
        ``cashflows_limit`` respectively (defaults: 730 days / 100 events
        and 3650 days / 500 events — the cached/published policy). The
        parameterized sections are rebuilt on the fly from the (possibly
        cached) summary's full calendars and are neither cached nor
        published; all other summary fields are unaffected.

        The summary contains:

        * open positions (``quantity > 0``) with yields, accrued coupon,
          next coupon date and total invested;
        * ``next_coupons`` — future coupon payments across open positions,
          within ``horizon_days`` of the valuation date and at most
          ``limit`` events (sorted by date, then bond_id);
        * ``realized_pnl`` — SELL/MATURE results with the cost basis
          known at the time of each exit, on the same average-cost basis
          as the open positions (full close resets the average);
        * ``upcoming_cashflows`` — all future coupons plus the maturity
          repayment for open positions, within ``cashflows_horizon_days``
          and at most ``cashflows_limit`` events (sorted by date, then
          bond_id). The default horizon (3650 days ≈ 10 years) effectively
          covers "to maturity" for typical bonds.

        Args:
            user_id: User whose portfolio is summarized.
            today: Valuation date; defaults to ``date.today()``. An
                explicit value bypasses the cache.
            horizon_days: ``next_coupons`` horizon in days from the
                valuation date; ``1..3650``, default 730.
            limit: Maximum number of ``next_coupons`` events;
                ``1..1000``, default 100.
            cashflows_horizon_days: ``upcoming_cashflows`` horizon in days
                from the valuation date; ``1..7300``, default 3650.
            cashflows_limit: Maximum number of ``upcoming_cashflows``
                events; ``1..5000``, default 500.

        Returns:
            The portfolio summary — from cache, or computed on a miss;
            ``next_coupons`` and ``upcoming_cashflows`` rebuilt with the
            requested parameters.

        Raises:
            ValueError: ``horizon_days``, ``limit``,
                ``cashflows_horizon_days`` or ``cashflows_limit`` out of
                bounds. The REST layer validates the same bounds earlier
                (422); this guard covers direct (UI) callers.
        """
        if not NEXT_COUPONS_HORIZON_DAYS_MIN <= horizon_days <= NEXT_COUPONS_HORIZON_DAYS_MAX:
            raise ValueError(
                "next_coupons horizon_days must be "
                f"{NEXT_COUPONS_HORIZON_DAYS_MIN}..{NEXT_COUPONS_HORIZON_DAYS_MAX}, "
                f"got {horizon_days}"
            )
        if not NEXT_COUPONS_LIMIT_MIN <= limit <= NEXT_COUPONS_LIMIT_MAX:
            raise ValueError(
                f"next_coupons limit must be {NEXT_COUPONS_LIMIT_MIN}..{NEXT_COUPONS_LIMIT_MAX}, got {limit}"
            )
        if not CASHFLOWS_HORIZON_DAYS_MIN <= cashflows_horizon_days <= CASHFLOWS_HORIZON_DAYS_MAX:
            raise ValueError(
                "upcoming_cashflows horizon_days must be "
                f"{CASHFLOWS_HORIZON_DAYS_MIN}..{CASHFLOWS_HORIZON_DAYS_MAX}, "
                f"got {cashflows_horizon_days}"
            )
        if not CASHFLOWS_LIMIT_MIN <= cashflows_limit <= CASHFLOWS_LIMIT_MAX:
            raise ValueError(
                f"upcoming_cashflows limit must be {CASHFLOWS_LIMIT_MIN}..{CASHFLOWS_LIMIT_MAX}, got {cashflows_limit}"
            )

        if today is not None:
            summary = await self._compute_summary(user_id, today)
            base = today
        else:
            base = date.today()
            cache_key = ("summary", user_id)
            async with self._cache_lock:
                cached = self._summary_cache.get(cache_key)
            if cached is not None:
                summary = cached.model_copy(deep=True)
            else:
                fresh = await self._compute_summary(user_id, base)
                async with self._cache_lock:
                    self._summary_cache[cache_key] = fresh
                summary = fresh.model_copy(deep=True)

        # next_coupons is derived from the full (uncapped) cashflow
        # calendar, so it must be rebuilt before upcoming_cashflows is
        # filtered down to the caller's horizon/limit.
        summary.next_coupons = _next_coupons_from_summary(summary, base, horizon_days, limit)
        summary.upcoming_cashflows = _cashflows_from_summary(
            summary, base, cashflows_horizon_days, cashflows_limit
        )
        return summary

    async def get_position_analytics(
        self, user_id: int, bond_id: int, today: date | None = None
    ) -> PositionAnalytics | None:
        """Analytics for a single position; no event is published.

        Derived from the same
        :class:`~bond_accounting.analytics.dto.PortfolioSummary` as
        :meth:`get_portfolio_summary` — cached when ``today`` is ``None``;
        an explicit ``today`` recomputes the summary on the fly, bypassing
        the cache.

        Args:
            user_id: User whose position is computed.
            bond_id: Bond the position is held in.
            today: Valuation date; defaults to ``date.today()``. An
                explicit value bypasses the cache.

        Returns:
            Position analytics, or ``None`` when the user-bond pair has no
            transaction history, the bond does not exist, or the position
            is closed (``quantity <= 0``).
        """
        if today is not None:
            summary = await self._compute_summary(user_id, today)
        else:
            summary = await self.get_portfolio_summary(user_id)
        return next((p for p in summary.positions if p.bond_id == bond_id), None)

    # ------------------------------------------------------------------ #
    # event-driven recalculation

    async def recalculate(self, user_id: int) -> PortfolioSummary:
        """Recompute the user's summary, refresh the cache and publish it.

        The only write path of the cache and the only place that publishes
        ``portfolio.recalculated`` (with ``sender="analytics"``). Invoked
        from the event handlers installed by :func:`attach_to_event_bus`
        — never from read paths.

        Args:
            user_id: User whose portfolio is recalculated.

        Returns:
            The freshly computed summary (a copy; the cache holds its own).
        """
        summary = await self._compute_summary(user_id, date.today())
        async with self._cache_lock:
            self._summary_cache[("summary", user_id)] = summary
        await self._event_bus.publish(
            Topic.PORTFOLIO_RECALCULATED, summary.model_dump(mode="json"), sender=_SENDER
        )
        return summary.model_copy(deep=True)

    async def _compute_summary(self, user_id: int, valuation_date: date) -> PortfolioSummary:
        """Compute the full portfolio summary (no cache, no publish).

        Args:
            user_id: User whose portfolio is summarized.
            valuation_date: Valuation date for yields and coupon grids.

        Returns:
            The freshly computed portfolio summary — including empty
            portfolios (zero sums, empty lists).
        """

        async with self._session_factory() as session:
            txn_result = await session.execute(
                select(Transaction)
                .where(Transaction.user_id == user_id)
                .order_by(Transaction.date, Transaction.id)
            )
            txns = list(txn_result.scalars().all())
            bonds: dict[int, Bond] = {}
            if txns:
                bond_ids = {txn.bond_id for txn in txns}
                bond_result = await session.execute(select(Bond).where(Bond.id.in_(bond_ids)))
                bonds = {bond.id: bond for bond in bond_result.scalars().all()}

        grouped: dict[int, list[Transaction]] = {}
        for txn in txns:
            grouped.setdefault(txn.bond_id, []).append(txn)

        positions: list[PositionAnalytics] = []
        sells_total = 0.0
        maturities_total = 0.0
        commissions_total = 0.0
        next_coupons: list[CouponDue] = []
        cashflows: list[Cashflow] = []
        horizon_end = valuation_date + _NEXT_COUPONS_HORIZON

        for bond_id in sorted(grouped):
            rows = grouped[bond_id]
            sells, maturities, commissions = _realized_pnl_of(rows)
            sells_total += sells
            maturities_total += maturities
            commissions_total += commissions

            bond = bonds.get(bond_id)
            if bond is None:
                # Unreachable under FK constraints; kept defensive.
                logger.warning(
                    "Bond id=%s referenced by transactions is missing; skipping analytics",
                    bond_id,
                )
                continue
            quantity, _ = _position_of(rows)
            if quantity <= 0:
                continue
            positions.append(_build_position_analytics(bond, rows, valuation_date))

            months = _FREQUENCY_MONTHS[bond.coupon_frequency]
            coupon_per_period = _coupon_per_period(bond)
            future_dates = (
                _future_coupon_dates(bond.maturity_date, months, valuation_date)
                if bond.maturity_date > valuation_date
                else []
            )
            for coupon_date in future_dates:
                if coupon_date <= horizon_end:
                    next_coupons.append(
                        CouponDue(
                            bond_id=bond.id,
                            isin=bond.isin,
                            name=bond.name,
                            date=coupon_date,
                            amount=coupon_per_period * quantity,
                        )
                    )
                cashflows.append(
                    Cashflow(
                        date=coupon_date,
                        bond_id=bond.id,
                        isin=bond.isin,
                        kind="COUPON",
                        amount=coupon_per_period * quantity,
                    )
                )
            if bond.maturity_date > valuation_date:
                cashflows.append(
                    Cashflow(
                        date=bond.maturity_date,
                        bond_id=bond.id,
                        isin=bond.isin,
                        kind="MATURITY",
                        amount=float(bond.nominal * quantity),
                    )
                )

        next_coupons.sort(key=lambda due: (due.date, due.bond_id))
        next_coupons = next_coupons[:_NEXT_COUPONS_MAX_EVENTS]
        cashflows.sort(key=lambda flow: (flow.date, flow.bond_id))

        summary = PortfolioSummary(
            user_id=user_id,
            generated_at=datetime.now(UTC),
            positions=positions,
            total_invested=sum((p.total_invested for p in positions), start=0.0),
            next_coupons=next_coupons,
            realized_pnl=RealizedPnl(
                sells=sells_total,
                maturities=maturities_total,
                commissions=commissions_total,
                total=sells_total + maturities_total - commissions_total,
            ),
            upcoming_cashflows=cashflows,
        )
        logger.info(
            "Portfolio summary for user_id=%s: positions=%d, total_invested=%.2f",
            user_id,
            len(positions),
            summary.total_invested,
        )
        return summary


def attach_to_event_bus(service: AnalyticsService, event_bus: EventBus) -> list[Callable[[], None]]:
    """Subscribe ``service`` to portfolio-affecting change events.

    Subscribes to ``bond.updated``, ``bond.deleted``,
    ``transaction.created`` and ``position.updated``: on every event the
    portfolio of the user named in the payload (``user_id`` key) is
    recalculated via
    :meth:`~bond_accounting.analytics.service.AnalyticsService.recalculate`
    (which refreshes the in-memory cache and re-publishes
    ``portfolio.recalculated``). Every publisher in this codebase stamps
    ``user_id`` on the payload — ``bond.updated`` / ``bond.deleted`` are
    fanned out by :class:`~bond_accounting.bonds.service.BondService`
    with one event per holder. Malformed events whose payload carries no
    ``user_id`` are ignored with a warning.

    Handler failures are logged and swallowed: the bus must never break
    because of analytics errors.

    Args:
        service: Service whose summaries are recalculated.
        event_bus: Bus to subscribe on; must be started for dispatching.

    Returns:
        Unsubscribe callables, one per subscribed topic.
    """

    async def handle(message: Message) -> None:
        """Recalculate the affected user's portfolio; never raises."""
        try:
            raw_user_id = message.payload.get("user_id")
            if raw_user_id is None:
                logger.warning(
                    "Ignoring %s event without user_id (sender=%s)",
                    message.topic,
                    message.sender,
                )
                return
            user_id = int(raw_user_id)
            summary = await service.recalculate(user_id)
            logger.info(
                "Recalculated portfolio user_id=%s after %s: positions=%d, total_invested=%.2f",
                user_id,
                message.topic,
                len(summary.positions),
                summary.total_invested,
            )
        except Exception:
            logger.exception(
                "Analytics handler failed for %s event (sender=%s)",
                message.topic,
                message.sender,
            )

    unsubscribers = [event_bus.subscribe(topic, handle) for topic in _CHANGE_TOPICS]
    logger.info("Analytics attached to event bus: topics=%s", sorted(_CHANGE_TOPICS))
    return unsubscribers
