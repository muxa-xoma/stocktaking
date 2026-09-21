"""Tests for the yield_calc module: coupon schedule, YTM, current yield, accrued coupon."""

from __future__ import annotations

from datetime import date

import pytest

from bond_accounting.yield_calc import (
    build_coupon_schedule,
    calculate_accrued_coupon,
    calculate_current_yield,
    calculate_ytm,
)
from bond_accounting.yield_calc.coupon_schedule import _add_months
from bond_accounting.yield_calc.ytm import YtmCalculationError


class TestAddMonths:
    """Month-arithmetic helper with day clamping."""

    def test_clamp_to_leap_february(self) -> None:
        assert _add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)

    def test_clamp_to_non_leap_february(self) -> None:
        assert _add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)

    def test_year_rollover(self) -> None:
        assert _add_months(date(2024, 11, 15), 3) == date(2025, 2, 15)

    def test_subtract_months(self) -> None:
        assert _add_months(date(2030, 6, 30), -6) == date(2029, 12, 30)
        assert _add_months(date(2030, 1, 1), -12) == date(2029, 1, 1)

    def test_clamp_31_to_30_day_month(self) -> None:
        assert _add_months(date(2030, 3, 31), 3) == date(2030, 6, 30)


class TestBuildCouponSchedule:
    """Coupon payment schedules."""

    def test_annual_default_start(self) -> None:
        """Default start = maturity - one period -> exactly 2 payments."""
        payments = build_coupon_schedule(
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2030, 1, 1),
            nominal=1000,
        )
        assert [(p.payment_date, p.amount, p.is_final) for p in payments] == [
            (date(2029, 1, 1), 50.0, False),
            (date(2030, 1, 1), 1050.0, True),
        ]

    def test_semi_annual_default_start(self) -> None:
        payments = build_coupon_schedule(
            coupon_rate=4.0,
            coupon_frequency="SEMI_ANNUAL",
            maturity_date=date(2030, 6, 30),
        )
        assert [(p.payment_date, p.amount, p.is_final) for p in payments] == [
            (date(2029, 12, 30), 20.0, False),
            (date(2030, 6, 30), 1020.0, True),
        ]

    def test_semi_annual_custom_start_four_payments(self) -> None:
        """4 payments: 3 coupons of 20 and a final 20 + 1000 = 1020."""
        payments = build_coupon_schedule(
            coupon_rate=4.0,
            coupon_frequency="SEMI_ANNUAL",
            maturity_date=date(2030, 6, 30),
            nominal=1000,
            start_date=date(2028, 12, 31),
        )
        assert [(p.payment_date, p.amount, p.is_final) for p in payments] == [
            (date(2028, 12, 31), 20.0, False),
            (date(2029, 6, 30), 20.0, False),
            (date(2029, 12, 30), 20.0, False),
            (date(2030, 6, 30), 1020.0, True),
        ]

    def test_quarterly_four_coupons_plus_final(self) -> None:
        """4 coupon payments of 20 each, then the final 20 + 1000 = 1020."""
        payments = build_coupon_schedule(
            coupon_rate=8.0,
            coupon_frequency="QUARTERLY",
            maturity_date=date(2030, 3, 31),
            nominal=1000,
            start_date=date(2029, 3, 31),
        )
        assert [(p.payment_date, p.amount, p.is_final) for p in payments] == [
            (date(2029, 3, 31), 20.0, False),
            (date(2029, 6, 30), 20.0, False),
            (date(2029, 9, 30), 20.0, False),
            (date(2029, 12, 31), 20.0, False),
            (date(2030, 3, 31), 1020.0, True),
        ]

    def test_custom_start_overrides_default(self) -> None:
        """Explicit start_date becomes the first payment; the rest of the grid
        is anchored on the maturity date."""
        payments = build_coupon_schedule(
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2030, 1, 1),
            start_date=date(2027, 6, 1),
        )
        assert [p.payment_date for p in payments] == [
            date(2027, 6, 1),
            date(2028, 1, 1),
            date(2029, 1, 1),
            date(2030, 1, 1),  # maturity is always the final payment
        ]
        assert payments[-1].is_final is True
        assert payments[-1].amount == 1050.0
        assert all(p.is_final is False for p in payments[:-1])

    def test_zero_coupon_schedule(self) -> None:
        payments = build_coupon_schedule(
            coupon_rate=0.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2030, 1, 1),
        )
        assert [(p.payment_date, p.amount, p.is_final) for p in payments] == [
            (date(2029, 1, 1), 0.0, False),
            (date(2030, 1, 1), 1000.0, True),
        ]

    def test_invalid_frequency_raises(self) -> None:
        with pytest.raises(ValueError, match="frequency"):
            build_coupon_schedule(
                coupon_rate=5.0,
                coupon_frequency="MONTHLY",
                maturity_date=date(2030, 1, 1),
            )

    def test_start_not_before_maturity_raises(self) -> None:
        with pytest.raises(ValueError, match="before maturity"):
            build_coupon_schedule(
                coupon_rate=5.0,
                coupon_frequency="ANNUAL",
                maturity_date=date(2030, 1, 1),
                start_date=date(2030, 1, 1),
            )


def _reprice(
    ytm: float,
    coupon_rate: float,
    periods_per_year: int,
    maturity_date: date,
    today: date,
    nominal: int = 1000,
) -> float:
    """Independently re-price the bond at the given YTM (test helper).

    Mirrors the exact fractional-period (ACT/365) discounting used by
    ``calculate_ytm``: each payment is discounted by
    ``(1 + ytm / periods_per_year) ** (days * periods_per_year / 365)`` where
    ``days`` is the actual number of days from ``today`` to the payment.
    """
    months = 12 // periods_per_year
    payment_dates: list[date] = []
    current = maturity_date
    while current > today:
        payment_dates.append(current)
        current = _add_months(current, -months)
    coupon = nominal * coupon_rate / 100 / periods_per_year
    factor = 1.0 + ytm / periods_per_year
    pv = 0.0
    for payment_date in payment_dates:
        days = (payment_date - today).days
        amount = coupon + (nominal if payment_date == maturity_date else 0.0)
        pv += amount / factor ** (days * periods_per_year / 365)
    return pv


class TestCalculateYtm:
    """Yield to Maturity."""

    def test_zero_coupon_one_year(self) -> None:
        ytm = calculate_ytm(
            price=950.0,
            coupon_rate=0.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm == pytest.approx(1000 / 950 - 1, abs=1e-9)

    def test_par_bond_ytm_equals_coupon(self) -> None:
        ytm = calculate_ytm(
            price=1000.0,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2031, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        assert ytm == pytest.approx(0.05, abs=1e-4)

    def test_price_above_nominal_ytm_below_coupon(self) -> None:
        ytm = calculate_ytm(
            price=1050.0,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2031, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        assert ytm < 0.05

    def test_price_below_nominal_ytm_above_coupon(self) -> None:
        ytm = calculate_ytm(
            price=950.0,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2031, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        assert ytm > 0.05

    def test_ten_year_semi_annual_convergence(self) -> None:
        """A 10-year semi-annual bond: known par value and re-priced check."""
        # Par bond: YTM equals the coupon rate exactly.
        par_ytm = calculate_ytm(
            price=1000.0,
            coupon_rate=5.0,
            coupon_frequency="SEMI_ANNUAL",
            maturity_date=date(2036, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert par_ytm is not None
        assert par_ytm == pytest.approx(0.05, abs=1e-4)

        # Discount bond: converged YTM must re-price back to the input price.
        ytm = calculate_ytm(
            price=950.0,
            coupon_rate=5.0,
            coupon_frequency="SEMI_ANNUAL",
            maturity_date=date(2036, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        assert 0.05 < ytm < 0.06
        assert _reprice(
            ytm, 5.0, periods_per_year=2, maturity_date=date(2036, 1, 1), today=date(2026, 1, 1)
        ) == pytest.approx(950.0, abs=1e-6)

    def test_quarterly_convergence(self) -> None:
        ytm = calculate_ytm(
            price=980.0,
            coupon_rate=8.0,
            coupon_frequency="QUARTERLY",
            maturity_date=date(2031, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        assert _reprice(
            ytm, 8.0, periods_per_year=4, maturity_date=date(2031, 1, 1), today=date(2026, 1, 1)
        ) == pytest.approx(980.0, abs=1e-6)

    def test_today_between_coupon_dates(self) -> None:
        """Valuation between coupon dates: fractional-period discounting.

        With exact ACT/365 fractional discounting the next coupon (107 days
        away) counts as 107/365 of a year, not a whole period; for a par
        (nominal) price between coupon dates the YTM is therefore above the
        coupon rate. Expected value verified with an independent bisection.
        """
        ytm = calculate_ytm(
            price=1000.0,
            coupon_rate=6.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2031, 6, 30),
            nominal=1000,
            today=date(2026, 3, 15),
        )
        assert ytm is not None
        assert ytm == pytest.approx(0.0697189117, abs=1e-6)

    def test_price_zero_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="price"):
            calculate_ytm(
                price=0.0,
                coupon_rate=5.0,
                coupon_frequency="ANNUAL",
                maturity_date=date(2031, 1, 1),
                today=date(2026, 1, 1),
            )

    def test_price_negative_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="price"):
            calculate_ytm(
                price=-10.0,
                coupon_rate=5.0,
                coupon_frequency="ANNUAL",
                maturity_date=date(2031, 1, 1),
                today=date(2026, 1, 1),
            )

    def test_maturity_on_or_before_today_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="maturity"):
            calculate_ytm(
                price=1000.0,
                coupon_rate=5.0,
                coupon_frequency="ANNUAL",
                maturity_date=date(2026, 1, 1),
                today=date(2026, 1, 1),
            )
        with pytest.raises(YtmCalculationError, match="maturity"):
            calculate_ytm(
                price=1000.0,
                coupon_rate=5.0,
                coupon_frequency="ANNUAL",
                maturity_date=date(2025, 1, 1),
                today=date(2026, 1, 1),
            )

    def test_invalid_frequency_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="frequency"):
            calculate_ytm(
                price=1000.0,
                coupon_rate=5.0,
                coupon_frequency="WEEKLY",
                maturity_date=date(2031, 1, 1),
                today=date(2026, 1, 1),
            )

    def test_price_beyond_bounds_returns_none(self) -> None:
        """A price too high to be matched within [-0.5, 2.0] yields None."""
        ytm = calculate_ytm(
            price=9999.0,
            coupon_rate=1.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is None

    def test_par_bond_with_large_nominal_accepted(self) -> None:
        """Key regression: a par bond with nominal > 9999 is valid.

        The upper bound is relative to the nominal (10x), so price=10000 with
        nominal=10000 passes validation and prices at the coupon rate.
        """
        ytm = calculate_ytm(
            price=10000.0,
            coupon_rate=5.0,
            coupon_frequency="ANNUAL",
            maturity_date=date(2031, 1, 1),
            nominal=10000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        assert ytm == pytest.approx(0.05, abs=1e-4)

    def test_upper_bound_is_nominal_relative(self) -> None:
        """nominal=1000, price=15000 (> 10 * 1000) is out of bounds.

        The error message mentions the nominal-relative limit (10000 for a
        nominal of 1000), not the old absolute 9999 cap.
        """
        with pytest.raises(YtmCalculationError, match="price") as exc_info:
            calculate_ytm(
                price=15000.0,
                coupon_rate=5.0,
                coupon_frequency="ANNUAL",
                maturity_date=date(2031, 1, 1),
                nominal=1000,
                today=date(2026, 1, 1),
            )
        assert "10000" in str(exc_info.value)

    def test_lower_bound_sub_unit_prices_rejected(self) -> None:
        """Prices below 1 currency unit are rejected regardless of nominal."""
        for price in (0.0, 0.5):
            with pytest.raises(YtmCalculationError, match="price"):
                calculate_ytm(
                    price=price,
                    coupon_rate=5.0,
                    coupon_frequency="ANNUAL",
                    maturity_date=date(2031, 1, 1),
                    nominal=1000,
                    today=date(2026, 1, 1),
                )

    def test_lower_bound_exactly_one_is_valid(self) -> None:
        """price=1 is the inclusive lower bound: no bounds error."""
        ytm = calculate_ytm(
            price=1.0,
            coupon_rate=0.0,  # closed form: always a number within bounds
            coupon_frequency="ANNUAL",
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert isinstance(ytm, float)

    def test_upper_bound_exactly_ten_nominal_is_valid(self) -> None:
        """nominal=1000, price=10000 (= 10 * nominal) is the inclusive edge."""
        ytm = calculate_ytm(
            price=10000.0,
            coupon_rate=0.0,  # closed form: no bracket convergence needed
            coupon_frequency="ANNUAL",
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        # No bounds error; the closed-form result itself is not asserted
        # (1000/10000 over one year falls outside the solver bracket).
        assert isinstance(ytm, float)


class TestCalculateCurrentYield:
    """Current yield."""

    def test_at_par(self) -> None:
        assert calculate_current_yield(price=1000, coupon_rate=5.0) == pytest.approx(0.05)

    def test_par_price_with_large_nominal_accepted(self) -> None:
        """A par price > 9999 with a large nominal is accepted (positivity check only)."""
        assert calculate_current_yield(price=10000, coupon_rate=5.0, nominal=10000) == (
            pytest.approx(0.05)
        )

    def test_below_par(self) -> None:
        assert calculate_current_yield(price=950, coupon_rate=5.0) == pytest.approx(
            0.0526315789, abs=1e-9
        )

    def test_zero_price_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="price"):
            calculate_current_yield(price=0, coupon_rate=5.0)

    def test_negative_price_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="price"):
            calculate_current_yield(price=-100, coupon_rate=5.0)


class TestCalculateAccruedCoupon:
    """Accrued coupon (НКД)."""

    def test_semi_annual_partial_period(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=5.0,
            coupon_frequency="SEMI_ANNUAL",
            last_coupon_date=date(2025, 1, 1),
            today=date(2025, 6, 1),
            nominal=1000,
        )
        # days_since = 151, days_in_period = 182, coupon_per_period = 25
        assert accrued == pytest.approx(25 * 151 / 182, abs=1e-9)

    def test_today_before_last_coupon_returns_zero(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=5.0,
            coupon_frequency="SEMI_ANNUAL",
            last_coupon_date=date(2025, 6, 1),
            today=date(2025, 1, 1),
        )
        assert accrued == 0.0

    def test_capped_at_full_period(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=5.0,
            coupon_frequency="SEMI_ANNUAL",
            last_coupon_date=date(2025, 1, 1),
            today=date(2026, 6, 1),
            nominal=1000,
        )
        assert accrued == 25.0

    def test_annual_partial_period(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=10.0,
            coupon_frequency="ANNUAL",
            last_coupon_date=date(2025, 1, 1),
            today=date(2025, 7, 1),
            nominal=1000,
        )
        # days = 181, period = 365, coupon = 100
        assert accrued == pytest.approx(100 * 181 / 365, abs=1e-9)

    def test_quarterly_partial_period(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=8.0,
            coupon_frequency="QUARTERLY",
            last_coupon_date=date(2025, 1, 1),
            today=date(2025, 2, 1),
            nominal=1000,
        )
        # coupon_per_period = 20, days_since = 31, days_in_period = 91
        assert accrued == pytest.approx(20 * 31 / 91, abs=1e-9)

    def test_invalid_frequency_raises(self) -> None:
        with pytest.raises(ValueError, match="frequency"):
            calculate_accrued_coupon(
                coupon_rate=5.0,
                coupon_frequency="DAILY",
                last_coupon_date=date(2025, 1, 1),
            )
