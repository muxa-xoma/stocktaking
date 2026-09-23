"""Tests for the ``yield_calc`` module: coupon schedule, YTM, current yield, accrued coupon.

Rewritten for the 2026 coupon-model refactor (SC-2): all three public
functions take ``coupon_period_days`` (calendar days between coupons, 0 =
zero-coupon) instead of a ``coupon_frequency`` enum, ``calculate_ytm`` also
accepts explicit ``cashflows`` (which take priority over the derived grid),
and future coupon dates are stepped BACKWARDS from ``maturity_date`` by
whole ``coupon_period_days`` day steps rather than with ``_add_months``.

All YTM goldens were recomputed with an independent bisection on the ACT/365
fractional formula (``.agents/data/bond-reference-and-coupon-refactor/goldens/``),
not copied from the pre-refactor expectations.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from bond_accounting.yield_calc import (
    CouponPayment,
    build_coupon_schedule,
    calculate_accrued_coupon,
    calculate_current_yield,
    calculate_ytm,
)
from bond_accounting.yield_calc.ytm import YtmCalculationError


def _coupon_per_period(nominal: int, coupon_rate: float, period_days: int) -> float:
    """Coupon cash per period (ACT/365), matching ``yield_calc``."""
    return nominal * coupon_rate / 100 * (period_days / 365)


class TestBuildCouponSchedule:
    """Coupon payment schedules (day-stepped grids)."""

    def test_zero_coupon_period_zero_single_final_payment(self) -> None:
        """period_days == 0 -> a single final payment of the nominal at maturity."""
        payments = build_coupon_schedule(
            coupon_rate=5.0,
            coupon_period_days=0,
            maturity_date=date(2030, 1, 1),
            nominal=1000,
        )
        assert payments == [CouponPayment(date(2030, 1, 1), 1000.0, is_final=True)]

    def test_annual_default_start(self) -> None:
        """period 365, no start_date -> maturity minus one period plus maturity."""
        payments = build_coupon_schedule(
            coupon_rate=5.0,
            coupon_period_days=365,
            maturity_date=date(2030, 1, 1),
            nominal=1000,
        )
        assert [(p.payment_date, p.amount, p.is_final) for p in payments] == [
            (date(2029, 1, 1), 50.0, False),
            (date(2030, 1, 1), 1050.0, True),
        ]

    def test_semi_annual_default_start_recomputed(self) -> None:
        """period 182: default start = maturity - 182 days; amount scales 4%*182/365."""
        coupon = _coupon_per_period(1000, 4.0, 182)
        payments = build_coupon_schedule(
            coupon_rate=4.0,
            coupon_period_days=182,
            maturity_date=date(2030, 6, 30),
        )
        assert [(p.payment_date, p.amount, p.is_final) for p in payments] == [
            (date(2029, 12, 30), pytest.approx(coupon, abs=1e-9), False),
            (date(2030, 6, 30), pytest.approx(coupon + 1000, abs=1e-9), True),
        ]

    def test_start_date_after_derived_coupon_skips_interstitial(self) -> None:
        """A start_date later than the maturity-anchored grid's interstitial dates.

        The grid only keeps whole-period dates strictly after the first
        coupon (``start_date``), so an interstitial date that would have been
        produced by the zero-anchor is dropped.
        """
        payments = build_coupon_schedule(
            coupon_rate=5.0,
            coupon_period_days=365,
            maturity_date=date(2030, 1, 1),
            start_date=date(2028, 6, 1),
        )
        # maturity-anchored grid: 2029-01-01, 2030-01-01; 2028-01-01 is not
        # strictly after start_date so it is excluded. start_date goes first.
        assert [p.payment_date for p in payments] == [
            date(2028, 6, 1),
            date(2029, 1, 1),
            date(2030, 1, 1),
        ]
        assert payments[-1].is_final is True
        assert payments[-1].amount == pytest.approx(1050.0)
        assert all(p.is_final is False for p in payments[:-1])

    def test_custom_start_overrides_default(self) -> None:
        """Explicit start_date becomes the first payment; the rest of the grid
        is anchored on the maturity date."""
        payments = build_coupon_schedule(
            coupon_rate=5.0,
            coupon_period_days=365,
            maturity_date=date(2030, 1, 1),
            start_date=date(2027, 6, 1),
        )
        assert payments[0].payment_date == date(2027, 6, 1)
        assert payments[-1].payment_date == date(2030, 1, 1)
        assert payments[-1].amount == pytest.approx(1050.0)
        assert payments[-1].is_final is True
        assert all(p.is_final is False for p in payments[:-1])

    def test_start_not_before_maturity_raises(self) -> None:
        with pytest.raises(ValueError, match="before maturity"):
            build_coupon_schedule(
                coupon_rate=5.0,
                coupon_period_days=365,
                maturity_date=date(2030, 1, 1),
                start_date=date(2030, 1, 1),
            )

    def test_negative_period_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            build_coupon_schedule(
                coupon_rate=5.0,
                coupon_period_days=-1,
                maturity_date=date(2030, 1, 1),
            )


def _reprice(
    ytm: float,
    coupon_rate: float,
    period_days: int,
    maturity_date: date,
    today: date,
    nominal: int = 1000,
) -> float:
    """Independently re-price the bond at the given YTM (day-stepped test helper).

    Mirrors the exact fractional-period (ACT/365) discounting used by
    ``calculate_ytm`` on the day-stepped grid: dates are generated by
    subtracting whole ``period_days`` from ``maturity_date``.
    """
    payment_dates: list[date] = []
    current = maturity_date
    while current > today:
        payment_dates.append(current)
        current -= timedelta(days=period_days)
    coupon = _coupon_per_period(nominal, coupon_rate, period_days)
    periods_per_year = 365 / period_days
    factor = 1.0 + ytm / periods_per_year
    pv = 0.0
    for payment_date in payment_dates:
        days = (payment_date - today).days
        amount = coupon + (nominal if payment_date == maturity_date else 0.0)
        pv += amount / factor ** (days * periods_per_year / 365)
    return pv


class TestCalculateYtm:
    """Yield to Maturity (recomputed goldens for the day-stepped grid)."""

    def test_zero_coupon_period_zero_closed_form(self) -> None:
        """period_days == 0 -> zero-coupon closed form (single maturity payment)."""
        ytm = calculate_ytm(
            price=950.0,
            coupon_rate=5.0,
            coupon_period_days=0,
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm == pytest.approx(1000 / 950 - 1, abs=1e-9)

    def test_zero_rate_with_positive_period_takes_zero_coupon_path(self) -> None:
        """OR-semantics: coupon_rate == 0 with coupon_period_days > 0 also
        takes the zero-coupon closed form (periods_per_year = 365/period)."""
        period_days = 182
        ytm = calculate_ytm(
            price=950.0,
            coupon_rate=0.0,
            coupon_period_days=period_days,
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        # Closed form with ppy = 365/182 (independently computed).
        assert ytm == pytest.approx(0.0519548710, abs=1e-6)

    def test_annual_par_ytm_equals_coupon_clean_grid(self) -> None:
        """A period-365 par bond whose coupon dates are whole periods from today.

        No February 29 falls between the valuation date and any coupon, so the
        day-stepped grid is uniform and a par price prices exactly at the
        coupon rate.
        """
        ytm = calculate_ytm(
            price=1000.0,
            coupon_rate=5.0,
            coupon_period_days=365,
            maturity_date=date(2028, 1, 1),
            nominal=1000,
            today=date(2027, 1, 1),
        )
        assert ytm is not None
        assert ytm == pytest.approx(0.05, abs=1e-6)

    def test_annual_between_coupon_dates_recomputed(self) -> None:
        """Valuation mid-period: fractional-period discounting. Recomputed for the
        365-day grid (risk R5); the value shifts slightly from the old 0.0697189117."""
        ytm = calculate_ytm(
            price=1000.0,
            coupon_rate=6.0,
            coupon_period_days=365,
            maturity_date=date(2031, 6, 30),
            nominal=1000,
            today=date(2026, 3, 15),
        )
        assert ytm is not None
        assert ytm == pytest.approx(0.0697139014, abs=1e-6)

    def test_semi_annual_par_and_discount_recomputed(self) -> None:
        """10-year semi-annual (182-day) bond. The day-stepped grid drifts off
        whole halves of a year (Feb 29), so the par YTM is 0.0530, not 0.05.
        The discount price still re-prices to its input via the helper."""
        maturity = date(2036, 1, 1)
        today = date(2026, 1, 1)
        par_ytm = calculate_ytm(
            price=1000.0,
            coupon_rate=5.0,
            coupon_period_days=182,
            maturity_date=maturity,
            nominal=1000,
            today=today,
        )
        assert par_ytm is not None
        assert par_ytm == pytest.approx(0.0530265882, abs=1e-6)

        ytm = calculate_ytm(
            price=950.0,
            coupon_rate=5.0,
            coupon_period_days=182,
            maturity_date=maturity,
            nominal=1000,
            today=today,
        )
        assert ytm is not None
        assert 0.05 < ytm < 0.07
        assert _reprice(ytm, 5.0, 182, maturity_date=maturity, today=today) == pytest.approx(
            950.0, abs=1e-6
        )

    def test_quarterly_period_91_recomputed(self) -> None:
        """Quarterly (91-day) bond, recomputed for the day-stepped grid."""
        ytm = calculate_ytm(
            price=980.0,
            coupon_rate=8.0,
            coupon_period_days=91,
            maturity_date=date(2031, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is not None
        assert _reprice(
            ytm, 8.0, 91, maturity_date=date(2031, 1, 1), today=date(2026, 1, 1)
        ) == pytest.approx(980.0, abs=1e-6)

    def test_cashflows_override_derived_dates(self) -> None:
        """Explicit ``cashflows`` take priority over the derived grid.

        The bond has coupon_period_days=182, so the derived grid from
        2027-01-01 would give near-term coupons close to ``today``; the
        explicit schedule pins the actual payment dates/amounts instead.
        Independently recomputed golden on those exact flows.
        """
        cashflows = [
            CouponPayment(payment_date=date(2026, 8, 15), amount=49.86, is_final=False),
            CouponPayment(payment_date=date(2027, 1, 1), amount=1049.86, is_final=True),
        ]
        ytm = calculate_ytm(
            price=1000.0,
            coupon_rate=10.0,
            coupon_period_days=182,
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
            cashflows=cashflows,
        )
        assert ytm is not None
        assert ytm == pytest.approx(0.0991279102, abs=1e-6)

    def test_price_zero_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="price"):
            calculate_ytm(
                price=0.0,
                coupon_rate=5.0,
                coupon_period_days=365,
                maturity_date=date(2031, 1, 1),
                today=date(2026, 1, 1),
            )

    def test_price_negative_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="price"):
            calculate_ytm(
                price=-10.0,
                coupon_rate=5.0,
                coupon_period_days=365,
                maturity_date=date(2031, 1, 1),
                today=date(2026, 1, 1),
            )

    def test_maturity_on_or_before_today_raises(self) -> None:
        for maturity in (date(2026, 1, 1), date(2025, 1, 1)):
            with pytest.raises(YtmCalculationError, match="maturity"):
                calculate_ytm(
                    price=1000.0,
                    coupon_rate=5.0,
                    coupon_period_days=365,
                    maturity_date=maturity,
                    today=date(2026, 1, 1),
                )

    def test_negative_period_raises(self) -> None:
        with pytest.raises(YtmCalculationError, match="non-negative"):
            calculate_ytm(
                price=1000.0,
                coupon_rate=5.0,
                coupon_period_days=-1,
                maturity_date=date(2031, 1, 1),
                today=date(2026, 1, 1),
            )

    def test_price_beyond_bounds_returns_none(self) -> None:
        """A price too high to be matched within [-0.5, 2.0] yields None."""
        ytm = calculate_ytm(
            price=9999.0,
            coupon_rate=1.0,
            coupon_period_days=365,
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert ytm is None

    def test_par_bond_with_large_nominal_accepted(self) -> None:
        """Key regression: a par bond with nominal > 9999 is valid.

        The upper bound is relative to the nominal (10x), so price=10000 with
        nominal=10000 passes validation. Dates are chosen so the single
        coupon grid point is exactly 365 days out (no leap-day drift): a par
        bond then yields exactly its 5% coupon rate.
        """
        ytm = calculate_ytm(
            price=10000.0,
            coupon_rate=5.0,
            coupon_period_days=365,
            maturity_date=date(2028, 1, 1),
            nominal=10000,
            today=date(2027, 1, 1),
        )
        assert ytm is not None
        assert ytm == pytest.approx(0.05, abs=1e-4)

    def test_upper_bound_is_nominal_relative(self) -> None:
        with pytest.raises(YtmCalculationError, match="price") as exc_info:
            calculate_ytm(
                price=15000.0,
                coupon_rate=5.0,
                coupon_period_days=365,
                maturity_date=date(2031, 1, 1),
                nominal=1000,
                today=date(2026, 1, 1),
            )
        assert "10000" in str(exc_info.value)

    def test_lower_bound_sub_unit_prices_rejected(self) -> None:
        for price in (0.0, 0.5):
            with pytest.raises(YtmCalculationError, match="price"):
                calculate_ytm(
                    price=price,
                    coupon_rate=5.0,
                    coupon_period_days=365,
                    maturity_date=date(2031, 1, 1),
                    nominal=1000,
                    today=date(2026, 1, 1),
                )

    def test_lower_bound_exactly_one_is_valid(self) -> None:
        ytm = calculate_ytm(
            price=1.0,
            coupon_rate=0.0,  # closed form: always a number within bounds
            coupon_period_days=365,
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert isinstance(ytm, float)

    def test_upper_bound_exactly_ten_nominal_is_valid(self) -> None:
        ytm = calculate_ytm(
            price=10000.0,
            coupon_rate=0.0,  # closed form: no bracket convergence needed
            coupon_period_days=365,
            maturity_date=date(2027, 1, 1),
            nominal=1000,
            today=date(2026, 1, 1),
        )
        assert isinstance(ytm, float)


class TestCalculateCurrentYield:
    """Current yield (signature unchanged)."""

    def test_at_par(self) -> None:
        assert calculate_current_yield(price=1000, coupon_rate=5.0) == pytest.approx(0.05)

    def test_par_price_with_large_nominal_accepted(self) -> None:
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

    def test_zero_coupon_period_zero(self) -> None:
        assert (
            calculate_accrued_coupon(
                coupon_rate=5.0,
                coupon_period_days=0,
                last_coupon_date=date(2025, 1, 1),
                today=date(2025, 6, 1),
                nominal=1000,
            )
            == 0.0
        )

    def test_semi_annual_partial_period_recomputed(self) -> None:
        """182-day period: coupon_per_period = 5% * 182/365 of the nominal."""
        accrued = calculate_accrued_coupon(
            coupon_rate=5.0,
            coupon_period_days=182,
            last_coupon_date=date(2025, 1, 1),
            today=date(2025, 6, 1),
            nominal=1000,
        )
        # days_since = 151, days_in_period = 182,
        # coupon_per_period = 1000 * 5/100 * 182/365
        assert accrued == pytest.approx(1000 * 5 / 100 * 182 / 365 * 151 / 182, abs=1e-9)

    def test_annual_partial_period_recomputed(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=10.0,
            coupon_period_days=365,
            last_coupon_date=date(2025, 1, 1),
            today=date(2025, 7, 1),
            nominal=1000,
        )
        # days = 181, period = 365, coupon_per_period = 100
        assert accrued == pytest.approx(100 * 181 / 365, abs=1e-9)

    def test_quarterly_partial_period_recomputed(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=8.0,
            coupon_period_days=91,
            last_coupon_date=date(2025, 1, 1),
            today=date(2025, 2, 1),
            nominal=1000,
        )
        # coupon_per_period = 1000*8/100*91/365, days_since=31, days_in=91
        assert accrued == pytest.approx(1000 * 8 / 100 * 91 / 365 * 31 / 91, abs=1e-9)

    def test_today_equals_last_coupon_date_returns_zero(self) -> None:
        assert (
            calculate_accrued_coupon(
                coupon_rate=5.0,
                coupon_period_days=182,
                last_coupon_date=date(2025, 1, 1),
                today=date(2025, 1, 1),
            )
            == 0.0
        )

    def test_today_before_last_coupon_returns_zero(self) -> None:
        assert (
            calculate_accrued_coupon(
                coupon_rate=5.0,
                coupon_period_days=182,
                last_coupon_date=date(2025, 6, 1),
                today=date(2025, 1, 1),
            )
            == 0.0
        )

    def test_capped_at_full_period(self) -> None:
        accrued = calculate_accrued_coupon(
            coupon_rate=5.0,
            coupon_period_days=182,
            last_coupon_date=date(2025, 1, 1),
            today=date(2026, 6, 1),
            nominal=1000,
        )
        assert accrued == pytest.approx(1000 * 5 / 100 * 182 / 365, abs=1e-9)

    def test_negative_period_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            calculate_accrued_coupon(
                coupon_rate=5.0,
                coupon_period_days=-1,
                last_coupon_date=date(2025, 1, 1),
            )
