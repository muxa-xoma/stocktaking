"""Accrued coupon (НКД) calculation for bonds.

Uses the ACT/365-style day count convention: the coupon period length is
``coupon_period_days`` calendar days (e.g. 182 for a typical semi-annual
Russian bond period, 91 for a quarterly one).
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

#: Day-count denominator for the per-period coupon fraction (ACT/365-style).
_DAYS_PER_YEAR = 365


def calculate_accrued_coupon(
    coupon_rate: float,
    coupon_period_days: int,
    last_coupon_date: date,
    today: date | None = None,
    nominal: int = 1000,
) -> float:
    """Calculate the accrued coupon (НКД) since the last coupon date.

    Accrued coupon = coupon_per_period * days_since_last / days_in_period,
    capped at one full period's coupon, where
    ``coupon_per_period = nominal * coupon_rate / 100 * (coupon_period_days / 365)``
    and ``days_in_period = coupon_period_days``.

    Args:
        coupon_rate: Annual coupon rate in percent (e.g. ``5.0`` means 5%).
        coupon_period_days: Calendar days between coupon payments;
            ``0`` denotes a zero-coupon bond.
        last_coupon_date: Date of the most recent coupon payment.
        today: Valuation date; defaults to ``date.today()``.
        nominal: Nominal (face) value per bond unit.

    Returns:
        The accrued amount per unit of nominal. Returns ``0.0`` for a
        zero-coupon bond (``coupon_period_days == 0``) or if ``today`` is
        before ``last_coupon_date``.

    Raises:
        ValueError: If the coupon period or coupon rate is negative, or
            the nominal is not positive.
    """
    if coupon_period_days < 0:
        raise ValueError(f"coupon_period_days must be non-negative, got {coupon_period_days}")
    if coupon_rate < 0:
        raise ValueError(f"coupon_rate must be non-negative, got {coupon_rate}")
    if nominal <= 0:
        raise ValueError(f"nominal must be positive, got {nominal}")

    if coupon_period_days == 0:
        logger.debug("Accrued coupon is 0: zero-coupon bond (coupon_period_days == 0)")
        return 0.0

    valuation_date = today if today is not None else date.today()

    coupon_per_period = nominal * coupon_rate / 100 * (coupon_period_days / _DAYS_PER_YEAR)
    days_in_period = coupon_period_days
    days_since_last = (valuation_date - last_coupon_date).days

    if days_since_last < 0:
        logger.debug(
            "Accrued coupon is 0: valuation %s precedes last coupon %s",
            valuation_date,
            last_coupon_date,
        )
        return 0.0
    if days_since_last >= days_in_period:
        logger.debug("Accrued coupon capped at full period: %.4f", coupon_per_period)
        return coupon_per_period

    accrued = coupon_per_period * days_since_last / days_in_period
    logger.debug(
        "Accrued coupon: %.6f (days_since=%d, days_in_period=%d)",
        accrued,
        days_since_last,
        days_in_period,
    )
    return accrued
