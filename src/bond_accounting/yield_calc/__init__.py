"""Stateless bond yield calculations: YTM, current yield, accrued coupon (НКД),
and coupon payment schedules.

This module contains pure-math functions only — no database, no event bus.
"""

from bond_accounting.yield_calc.accrued_coupon import calculate_accrued_coupon
from bond_accounting.yield_calc.coupon_schedule import (
    CouponPayment,
    build_coupon_schedule,
)
from bond_accounting.yield_calc.current_yield import calculate_current_yield
from bond_accounting.yield_calc.ytm import YtmCalculationError, calculate_ytm

__all__ = [
    "CouponPayment",
    "YtmCalculationError",
    "build_coupon_schedule",
    "calculate_accrued_coupon",
    "calculate_current_yield",
    "calculate_ytm",
]
