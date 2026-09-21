"""Pydantic DTOs for the analytics module.

``PortfolioSummary`` is the canonical read shape returned by
:class:`~bond_accounting.analytics.service.AnalyticsService` and the payload
of the ``portfolio.recalculated`` event-bus topic.
"""

from __future__ import annotations

# Runtime import on purpose: pydantic resolves the postponed `datetime.date`
# annotations against the module namespace (same as db/models.py).
import datetime  # noqa: TC003

from pydantic import BaseModel, Field

#: Lower/upper bound for the ``next_coupons`` horizon (days from the valuation date).
NEXT_COUPONS_HORIZON_DAYS_MIN = 1
NEXT_COUPONS_HORIZON_DAYS_MAX = 3650

#: Default ``next_coupons`` horizon: ~24 months ahead.
NEXT_COUPONS_HORIZON_DAYS_DEFAULT = 730

#: Lower/upper bound for the number of ``next_coupons`` events.
NEXT_COUPONS_LIMIT_MIN = 1
NEXT_COUPONS_LIMIT_MAX = 1000

#: Default cap on the number of ``next_coupons`` events.
NEXT_COUPONS_LIMIT_DEFAULT = 100

#: Lower/upper bound for the ``upcoming_cashflows`` horizon (days from the
#: valuation date).
CASHFLOWS_HORIZON_DAYS_MIN = 1
CASHFLOWS_HORIZON_DAYS_MAX = 7300

#: Default ``upcoming_cashflows`` horizon: ~10 years ahead — effectively
#: "to maturity" for typical bonds, preserving the pre-parameterization
#: behaviour of the full calendar.
CASHFLOWS_HORIZON_DAYS_DEFAULT = 3650

#: Lower/upper bound for the number of ``upcoming_cashflows`` events.
CASHFLOWS_LIMIT_MIN = 1
CASHFLOWS_LIMIT_MAX = 5000

#: Default cap on the number of ``upcoming_cashflows`` events.
CASHFLOWS_LIMIT_DEFAULT = 500


class CouponDue(BaseModel):
    """An upcoming coupon payment on the current portfolio position."""

    bond_id: int
    isin: str
    name: str
    date: datetime.date
    amount: float


class RealizedPnl(BaseModel):
    """Realized profit and loss from closed (SELL/MATURE) transactions."""

    sells: float
    maturities: float
    commissions: float
    total: float


class Cashflow(BaseModel):
    """A single upcoming cash inflow (coupon or principal repayment)."""

    date: datetime.date
    bond_id: int
    isin: str
    kind: str
    amount: float


class PositionAnalytics(BaseModel):
    """Per-bond analytics for one open position.

    ``ytm`` and ``current_yield`` are fractions (e.g. ``0.05`` = 5%) and are
    ``None`` for matured bonds or when the calculation fails;
    ``accrued_coupon`` is per one unit of nominal.
    """

    bond_id: int
    isin: str
    name: str
    quantity: int
    avg_buy_price: float
    total_invested: float
    ytm: float | None
    current_yield: float | None
    accrued_coupon: float
    next_coupon_date: datetime.date | None
    maturity_date: datetime.date


class NextCouponsParams(BaseModel):
    """User-tunable parameters for the ``next_coupons`` section of the summary.

    Default policy: ~24 months ahead (730 days), at most 100 events. Both
    fields are optional; bounds are enforced via ``Field(ge/le)`` (422 on
    out-of-range values). ``next_coupons`` itself is recomputed on the fly
    from the full coupon calendar using these values — the summary cache
    is not parameterized.
    """

    next_coupons_horizon_days: int = Field(
        default=NEXT_COUPONS_HORIZON_DAYS_DEFAULT,
        ge=NEXT_COUPONS_HORIZON_DAYS_MIN,
        le=NEXT_COUPONS_HORIZON_DAYS_MAX,
        description="Horizon for next_coupons, in days from the valuation date (1..3650).",
    )
    next_coupons_limit: int = Field(
        default=NEXT_COUPONS_LIMIT_DEFAULT,
        ge=NEXT_COUPONS_LIMIT_MIN,
        le=NEXT_COUPONS_LIMIT_MAX,
        description="Maximum number of next_coupons events (1..1000).",
    )


class CashflowsParams(BaseModel):
    """User-tunable parameters for the ``upcoming_cashflows`` section of the summary.

    Default policy: ~10 years ahead (3650 days — effectively "to maturity"
    for typical bonds), at most 500 events. Both fields are optional; bounds
    are enforced via ``Field(ge/le)`` (422 on out-of-range values).
    ``upcoming_cashflows`` is recomputed on the fly from the full cashflow
    calendar using these values — the summary cache is not parameterized.
    """

    cashflows_horizon_days: int = Field(
        default=CASHFLOWS_HORIZON_DAYS_DEFAULT,
        ge=CASHFLOWS_HORIZON_DAYS_MIN,
        le=CASHFLOWS_HORIZON_DAYS_MAX,
        description="Horizon for upcoming_cashflows, in days from the valuation date (1..7300).",
    )
    cashflows_limit: int = Field(
        default=CASHFLOWS_LIMIT_DEFAULT,
        ge=CASHFLOWS_LIMIT_MIN,
        le=CASHFLOWS_LIMIT_MAX,
        description="Maximum number of upcoming_cashflows events (1..5000).",
    )


class PortfolioQueryParams(NextCouponsParams, CashflowsParams):
    """Combined query parameters for ``GET /api/portfolio``.

    FastAPI only expands a Pydantic query model when it is the *sole*
    query parameter of an endpoint, so the per-section models are combined
    into one here instead of being declared as two ``Annotated[..., Query()]``
    parameters. Field names (and therefore the REST query parameters) are
    unchanged: ``next_coupons_horizon_days`` / ``next_coupons_limit`` and
    ``cashflows_horizon_days`` / ``cashflows_limit``. The same rule applies
    to ``broker_account_id``: it lives here rather than in a separate
    endpoint parameter, otherwise FastAPI stops expanding the model and
    demands a literal ``params`` query field.
    """

    broker_account_id: int | None = Field(
        default=None,
        description="Restrict the summary to transactions on this broker account.",
    )


class PortfolioSummary(BaseModel):
    """Full portfolio snapshot for a user.

    Only open positions (``quantity > 0``) are included; their SELL/MATURE
    history is still reflected in ``realized_pnl``.
    """

    user_id: int
    generated_at: datetime.datetime
    positions: list[PositionAnalytics]
    total_invested: float
    next_coupons: list[CouponDue]
    realized_pnl: RealizedPnl
    upcoming_cashflows: list[Cashflow]
