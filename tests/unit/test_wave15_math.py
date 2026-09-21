"""Wave 15 regression tests for pure YTM math (Tasks 25, 26).

Split out of the former monolithic ``test_wave15.py``: pure ``yield_calc``
math with hand-computed expectations, no DB, no REST.

* T25 — YTM fractional (ACT/365) discounting, hand-computed values.
* T26 — YTM hard price bounds ``[1, 10*nominal]`` (nominal-relative since Task 52).
"""

from __future__ import annotations

import datetime

import pytest

from bond_accounting.yield_calc import YtmCalculationError, calculate_ytm

# =========================================================================== #
# 1. YTM fractional discounting (Task 25)
# =========================================================================== #


def test_ytm_coupon_one_day_away_discounted_fractionally() -> None:
    """A coupon 1 day away counts as 1 day, not a whole period.

    today=2026-12-29, maturity=2027-06-30, SEMI_ANNUAL, coupon 8% -> payments
    (1 day, 40) and (183 days, 1040). Hand-computed with an independent
    bisection on the ACT/365 fractional formula: r = 0.1438881367.
    """
    ytm = calculate_ytm(
        price=1010.0,
        coupon_rate=8.0,
        coupon_frequency="SEMI_ANNUAL",
        maturity_date=datetime.date(2027, 6, 30),
        nominal=1000,
        today=datetime.date(2026, 12, 29),
    )
    assert ytm is not None
    assert ytm == pytest.approx(0.1438881367, abs=1e-6)


def test_ytm_two_coupon_bond_hand_computed() -> None:
    """Simple 2-coupon bond, hand-computed with fractional discounting.

    today=2026-01-01, maturity=2027-01-01, SEMI_ANNUAL, coupon 10% -> payments
    (181 days, 50) and (365 days, 1050); par price 1000.

    With whole-period discounting the answer would be exactly 0.10 (par bond,
    periods aligned); the fractional ACT/365 formula gives 0.1000205481.
    """
    ytm = calculate_ytm(
        price=1000.0,
        coupon_rate=10.0,
        coupon_frequency="SEMI_ANNUAL",
        maturity_date=datetime.date(2027, 1, 1),
        nominal=1000,
        today=datetime.date(2026, 1, 1),
    )
    assert ytm is not None
    assert ytm == pytest.approx(0.1000205481, abs=1e-6)
    # Discriminating check: whole-period discounting would return exactly 0.10.
    assert abs(ytm - 0.10) > 1e-6


# =========================================================================== #
# 2. YTM price bound (Task 26)
# =========================================================================== #


@pytest.mark.parametrize("price", [0.5, 10000.01])
def test_ytm_price_outside_bounds_raises(price: float) -> None:
    with pytest.raises(YtmCalculationError, match="price"):
        calculate_ytm(
            price=price,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=datetime.date(2031, 1, 1),
            today=datetime.date(2026, 1, 1),
        )


@pytest.mark.parametrize("price", [1.0, 9999.0])
def test_ytm_price_boundaries_are_valid(price: float) -> None:
    """Prices exactly at the [1, 9999] bounds pass validation (no raise)."""
    result = calculate_ytm(
        price=price,
        coupon_rate=0.0,  # closed form: always a number within bounds
        coupon_frequency="ANNUAL",
        maturity_date=datetime.date(2027, 1, 1),
        nominal=1000,
        today=datetime.date(2026, 1, 1),
    )
    assert isinstance(result, float)
