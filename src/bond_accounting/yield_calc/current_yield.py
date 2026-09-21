"""Current yield calculation for bonds."""

from __future__ import annotations

import logging

from bond_accounting.yield_calc.ytm import YtmCalculationError

logger = logging.getLogger(__name__)


def calculate_current_yield(
    price: float,
    coupon_rate: float,
    nominal: int = 1000,
) -> float:
    """Calculate the current yield of a bond.

    Current yield = annual coupon income / current price.

    Args:
        price: Current price per unit of nominal.
        coupon_rate: Annual coupon rate in percent (e.g. ``5.0`` means 5%).
        nominal: Nominal (face) value per bond unit.

    Returns:
        Current yield as a fraction (e.g. ``0.05`` for 5%).

    Raises:
        YtmCalculationError: If ``price`` or ``nominal`` is not positive, or
            the coupon rate is negative.
    """
    if price <= 0:
        raise YtmCalculationError(f"price must be positive, got {price}")
    if nominal <= 0:
        raise YtmCalculationError(f"nominal must be positive, got {nominal}")
    if coupon_rate < 0:
        raise YtmCalculationError(f"coupon_rate must be non-negative, got {coupon_rate}")

    annual_coupon = nominal * coupon_rate / 100
    current_yield = annual_coupon / price
    logger.debug(
        "Current yield: %.8f (annual_coupon=%.4f, price=%.4f)",
        current_yield,
        annual_coupon,
        price,
    )
    return current_yield
