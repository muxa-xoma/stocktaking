"""Coupon payment schedule construction for bonds.

The schedule is built with calendar-day arithmetic: coupon dates are spaced
``coupon_period_days`` calendar days apart, anchored on the maturity date.
A zero-coupon bond (``coupon_period_days == 0``) has no grid at all — its
only payment is the nominal repaid at maturity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: Day-count denominator for the per-period coupon fraction (ACT/365-style).
_DAYS_PER_YEAR = 365

#: Defensive floor for backward coupon-grid stepping; below it there is no
#: meaningful coupon date and grid construction stops.
_MIN_COUPON_DATE = date(1900, 1, 1)


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
    coupon_period_days: int,
    maturity_date: date,
    nominal: int = 1000,
    start_date: date | None = None,
) -> list[CouponPayment]:
    """Build the list of coupon payments from ``start_date`` to ``maturity_date``.

    The final payment includes the nominal (principal) in addition to the
    coupon. Coupon dates are spaced ``coupon_period_days`` calendar days
    apart (day-stepping, not calendar months).

    Args:
        coupon_rate: Annual coupon rate in percent (e.g. ``5.0`` means 5%).
        coupon_period_days: Calendar days between coupon payments;
            ``0`` denotes a zero-coupon bond.
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
        maturity date (``maturity_date`` minus whole periods), so the first
        period may be irregular when ``start_date`` does not fall on that
        grid. A zero-coupon bond (``coupon_period_days == 0``) yields a
        single payment: the nominal at ``maturity_date``.

    Raises:
        ValueError: If the coupon period or coupon rate is negative, the
            nominal is not positive, or ``start_date`` is not before
            ``maturity_date``.
    """
    if coupon_period_days < 0:
        raise ValueError(f"coupon_period_days must be non-negative, got {coupon_period_days}")
    if coupon_rate < 0:
        raise ValueError(f"coupon_rate must be non-negative, got {coupon_rate}")
    if nominal <= 0:
        raise ValueError(f"nominal must be positive, got {nominal}")

    # Zero-coupon bond: the only payment is the nominal repaid at maturity.
    if coupon_period_days == 0:
        logger.debug("Built zero-coupon schedule: maturity=%s, nominal=%d", maturity_date, nominal)
        return [CouponPayment(payment_date=maturity_date, amount=nominal, is_final=True)]

    period = timedelta(days=coupon_period_days)
    first_coupon = maturity_date - period if start_date is None else start_date
    if first_coupon >= maturity_date:
        raise ValueError(
            f"First coupon date {first_coupon} must be before maturity {maturity_date}"
        )

    coupon = nominal * coupon_rate / 100 * (coupon_period_days / _DAYS_PER_YEAR)

    # Anchor the coupon grid on the maturity date (final coupon at maturity,
    # previous coupons at maturity minus whole periods) and keep only the
    # dates strictly after the first coupon date; stop at the defensive
    # floor so degenerate inputs cannot step unboundedly far back.
    grid: list[date] = [maturity_date]
    current = maturity_date - period
    while current > first_coupon and current >= _MIN_COUPON_DATE:
        grid.append(current)
        current -= period
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
        "Built coupon schedule: period_days=%d, payments=%d, maturity=%s",
        coupon_period_days,
        len(payments),
        maturity_date,
    )
    return payments
