"""Accrued coupon (НКД) calculation for bonds.

Uses the ACT/365-style day count convention with fixed period lengths:
ANNUAL = 365 days, SEMI_ANNUAL = 182 days, QUARTERLY = 91 days.
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

#: Coupon periods per year for each supported frequency.
_PERIODS_PER_YEAR: dict[str, int] = {
    "ANNUAL": 1,
    "SEMI_ANNUAL": 2,
    "QUARTERLY": 4,
}

#: Fixed period length in days for each supported frequency (ACT/365-style).
_DAYS_IN_PERIOD: dict[str, int] = {
    "ANNUAL": 365,
    "SEMI_ANNUAL": 182,
    "QUARTERLY": 91,
}


def calculate_accrued_coupon(
    coupon_rate: float,
    coupon_frequency: str,
    last_coupon_date: date,
    today: date | None = None,
    nominal: int = 1000,
) -> float:
    """Calculate the accrued coupon (НКД) since the last coupon date.

    Accrued coupon = coupon_per_period * days_since_last / days_in_period,
    capped at one full period's coupon.

    Args:
        coupon_rate: Annual coupon rate in percent (e.g. ``5.0`` means 5%).
        coupon_frequency: One of ``ANNUAL``, ``SEMI_ANNUAL``, ``QUARTERLY``.
        last_coupon_date: Date of the most recent coupon payment.
        today: Valuation date; defaults to ``date.today()``.
        nominal: Nominal (face) value per bond unit.

    Returns:
        The accrued amount per unit of nominal. Returns ``0.0`` if
        ``today`` is before ``last_coupon_date``.

    Raises:
        ValueError: If the frequency is unknown, the coupon rate is
            negative, or the nominal is not positive.
    """
    valuation_date = today if today is not None else date.today()

    frequency = coupon_frequency.upper()
    if frequency not in _PERIODS_PER_YEAR:
        raise ValueError(
            f"Unsupported coupon frequency {coupon_frequency!r}; expected one of {sorted(_PERIODS_PER_YEAR)}"
        )
    if coupon_rate < 0:
        raise ValueError(f"coupon_rate must be non-negative, got {coupon_rate}")
    if nominal <= 0:
        raise ValueError(f"nominal must be positive, got {nominal}")

    coupon_per_period = nominal * coupon_rate / 100 / _PERIODS_PER_YEAR[frequency]
    days_in_period = _DAYS_IN_PERIOD[frequency]
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
