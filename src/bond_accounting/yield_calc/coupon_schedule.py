"""Coupon payment schedule construction for bonds.

The schedule is built with calendar-based month arithmetic (no day-count
conventions): ANNUAL = 12 months, SEMI_ANNUAL = 6 months, QUARTERLY = 3
months per coupon period.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

#: Months per coupon period for each supported frequency.
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


def _add_months(d: date, months: int) -> date:
    """Return ``d`` shifted by ``months`` calendar months.

    The day of month is clamped to the last valid day of the target month,
    e.g. 2024-01-31 + 1 month = 2024-02-29 (leap year) and
    2023-01-31 + 1 month = 2023-02-28.

    Args:
        d: Base date.
        months: Number of months to add (may be negative).

    Returns:
        The shifted date with the day clamped to the target month length.
    """
    total_months = d.month - 1 + months
    year = d.year + total_months // 12
    month = total_months % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class CouponPayment:
    """A single scheduled coupon payment.

    Attributes:
        payment_date: Date the payment is due.
        amount: Payment amount in currency units (coupon, or coupon plus
            nominal for the maturity payment).
        is_final: True if this is the maturity payment (includes the nominal).
    """

    payment_date: date
    amount: float
    is_final: bool


def build_coupon_schedule(
    coupon_rate: float,
    coupon_frequency: str,
    maturity_date: date,
    nominal: int = 1000,
    start_date: date | None = None,
) -> list[CouponPayment]:
    """Build the list of coupon payments from ``start_date`` to ``maturity_date``.

    The final payment includes the nominal (principal) in addition to the
    coupon. Periods are calendar-based: ANNUAL = 12 months, SEMI_ANNUAL = 6
    months, QUARTERLY = 3 months (not day-count based).

    Args:
        coupon_rate: Annual coupon rate in percent (e.g. ``5.0`` means 5%).
        coupon_frequency: One of ``ANNUAL``, ``SEMI_ANNUAL``, ``QUARTERLY``.
        maturity_date: Maturity date; always the date of the final payment.
        nominal: Nominal (face) value per bond unit.
        start_date: First coupon date. If None, defaults to
            ``maturity_date`` minus one coupon period (so the default
            schedule contains exactly two payments).

    Returns:
        Chronologically ordered coupon payments; the first payment is on
        ``start_date`` (the explicit or computed first coupon date) and the
        last one is the maturity payment (coupon + nominal,
        ``is_final=True``). Intermediate coupon dates are anchored on the
        maturity date (``maturity_date`` minus whole periods), which avoids
        day-clamping drift; the first period may therefore be irregular
        when ``start_date`` does not fall on that grid.

    Raises:
        ValueError: If the frequency is unknown, the coupon rate or nominal is
            negative/non-positive, or ``start_date`` is not before
            ``maturity_date``.
    """
    frequency = coupon_frequency.upper()
    months = _FREQUENCY_MONTHS.get(frequency)
    if months is None:
        raise ValueError(
            f"Unsupported coupon frequency {coupon_frequency!r}; expected one of {sorted(_FREQUENCY_MONTHS)}"
        )
    if coupon_rate < 0:
        raise ValueError(f"coupon_rate must be non-negative, got {coupon_rate}")
    if nominal <= 0:
        raise ValueError(f"nominal must be positive, got {nominal}")

    first_coupon = _add_months(maturity_date, -months) if start_date is None else start_date
    if first_coupon >= maturity_date:
        raise ValueError(
            f"First coupon date {first_coupon} must be before maturity {maturity_date}"
        )

    periods_per_year = _PERIODS_PER_YEAR[frequency]
    coupon = nominal * coupon_rate / 100 / periods_per_year

    # Anchor the coupon grid on the maturity date (final coupon at maturity,
    # previous coupons at maturity minus whole periods) and keep only the
    # dates strictly after the first coupon date.
    grid: list[date] = [maturity_date]
    current = _add_months(maturity_date, -months)
    while current > first_coupon:
        grid.append(current)
        current = _add_months(current, -months)
    grid.reverse()

    # start_date is the first coupon date; the grid only contains dates
    # strictly after it, so it always goes first.
    payment_dates = [first_coupon, *grid]

    payments = [
        CouponPayment(
            payment_date=payment_date,
            amount=coupon + (nominal if payment_date == maturity_date else 0.0),
            is_final=payment_date == maturity_date,
        )
        for payment_date in payment_dates
    ]
    logger.debug(
        "Built coupon schedule: frequency=%s, payments=%d, maturity=%s",
        frequency,
        len(payments),
        maturity_date,
    )
    return payments
