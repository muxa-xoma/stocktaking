"""Wave 15 regression tests for pure YTM math (Tasks 25, 26).

Split out of the former monolithic ``test_wave15.py``: pure ``yield_calc``
math with hand-computed expectations, no DB, no REST.

* T25 — YTM fractional (ACT/365) discounting, recomputed by an independent
  bisection after the ``coupon_frequency`` -> ``coupon_period_days`` refactor
  (epic ``bond-reference-and-coupon-refactor``): the day-stepped coupon grid
  anchored on the maturity date shifted the payment dates, so the expected
  YTMs are recomputed, not carried over.
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
    today=2026-12-29, maturity=2027-06-30, coupon_period_days=182, coupon
    8% -> payments (1 day, 39.8904) and (183 days, 1039.8904), where the
    coupon amount is 1000 * 8% * 182/365 = 39.8904... Recomputed with an
    independent bisection on the ACT/365 fractional formula:
    r = 0.1434077827.
    """
    ytm = calculate_ytm(
        price=1010.0,
        coupon_rate=8.0,
        coupon_period_days=182,
        maturity_date=datetime.date(2027, 6, 30),
        nominal=1000,
        today=datetime.date(2026, 12, 29),
    )
    assert ytm is not None
    assert ytm == pytest.approx(0.1434077827, abs=1e-6)


def test_ytm_two_coupon_bond_hand_computed() -> None:
    """Short bond on a 182-day grid, recomputed after the day-stepping change.

    today=2026-01-01, maturity=2027-01-01, coupon_period_days=182, coupon
    10% -> coupon amount 1000 * 10% * 182/365 = 49.863... The grid is now
    stepped back from maturity by calendar days, so the payments are
    (1 day, 49.863), (183 days, 49.863) and (365 days, 1049.863) — note the
    extra near-term 2026-01-02 coupon that calendar stepping produces.
    Recomputed with an independent bisection on the ACT/365 fractional
    formula: r = 0.1554249949.
    """
    ytm = calculate_ytm(
        price=1000.0,
        coupon_rate=10.0,
        coupon_period_days=182,
        maturity_date=datetime.date(2027, 1, 1),
        nominal=1000,
        today=datetime.date(2026, 1, 1),
    )
    assert ytm is not None
    assert ytm == pytest.approx(0.1554249949, abs=1e-6)
    # Discriminating check: with month-based SEMI_ANNUAL periods and whole-
    # period discounting the answer used to be ~0.10; the day-stepped grid
    # with the extra 2026-01-02 coupon moves it far away from 0.10.
    assert abs(ytm - 0.10) > 1e-3


# =========================================================================== #
# 2. YTM price bound (Task 26)
# =========================================================================== #


@pytest.mark.parametrize("price", [0.5, 10000.01])
def test_ytm_price_outside_bounds_raises(price: float) -> None:
    with pytest.raises(YtmCalculationError, match="price"):
        calculate_ytm(
            price=price,
            coupon_rate=5.0,
            coupon_period_days=365,
            maturity_date=datetime.date(2031, 1, 1),
            today=datetime.date(2026, 1, 1),
        )


@pytest.mark.parametrize("price", [1.0, 9999.0])
def test_ytm_price_boundaries_are_valid(price: float) -> None:
    """Prices exactly at the [1, 9999] bounds pass validation (no raise)."""
    result = calculate_ytm(
        price=price,
        coupon_rate=0.0,  # closed form: always a number within bounds
        coupon_period_days=365,
        maturity_date=datetime.date(2027, 1, 1),
        nominal=1000,
        today=datetime.date(2026, 1, 1),
    )
    assert isinstance(result, float)
