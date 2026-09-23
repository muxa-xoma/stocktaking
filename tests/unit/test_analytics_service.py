"""Unit tests for the analytics module's pure calculation helpers.

The helpers under test are module-level functions in
``bond_accounting.analytics.service``; they operate on plain ORM instances
(no database) with known-good expected values.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest

from bond_accounting.analytics.service import (
    _build_position_analytics,
    _coupon_per_period,
    _future_coupon_dates,
    _last_coupon_date,
    _position_of,
    _realized_pnl_of,
)
from bond_accounting.db.models import Bond, Transaction
from bond_accounting.yield_calc import CouponPayment

if TYPE_CHECKING:
    from collections.abc import Sequence

TODAY = datetime.date(2026, 2, 1)


def _txn(
    type_: str,
    quantity: int,
    price: float,
    day: int,
    commission: float = 0.0,
) -> Transaction:
    """Build an unpersisted Transaction dated 2026-01-<day>."""
    return Transaction(
        type=type_,
        quantity=quantity,
        price=price,
        date=datetime.date(2026, 1, day),
        commission=commission,
    )


def _bond(**overrides: object) -> Bond:
    """Build an unpersisted Bond with test defaults, overridden by ``overrides``."""
    defaults: dict[str, object] = {
        "id": 1,
        "isin": "RU000A0JX0J2",
        "name": "OFLZ 2030",
        "nominal": 1000,
        "coupon_rate": 7.0,
        "coupon_period_days": 365,
        "maturity_date": datetime.date(2030, 1, 1),
    }
    defaults.update(overrides)
    return Bond(**defaults)


# --------------------------------------------------------------------- #
# _position_of


def test_position_of_single_buy() -> None:
    quantity, avg = _position_of([_txn("BUY", 10, 1000.0, 10)])
    assert quantity == 10
    assert avg == pytest.approx(1000.0)


def test_position_of_buy_then_sell_keeps_buy_average() -> None:
    """The SELL price must not affect the BUY-only weighted average."""
    quantity, avg = _position_of([_txn("BUY", 10, 1000.0, 10), _txn("SELL", 4, 1050.0, 15)])
    assert quantity == 6
    assert avg == pytest.approx(1000.0)


def test_position_of_buy_then_mature() -> None:
    quantity, avg = _position_of([_txn("BUY", 10, 1000.0, 10), _txn("MATURE", 4, 1000.0, 20)])
    assert quantity == 6
    assert avg == pytest.approx(1000.0)


def test_position_of_weighted_average_of_multiple_buys() -> None:
    quantity, avg = _position_of([_txn("BUY", 5, 1000.0, 10), _txn("BUY", 5, 1200.0, 12)])
    assert quantity == 10
    assert avg == pytest.approx(1100.0)


def test_position_of_no_buys_returns_none_average() -> None:
    quantity, avg = _position_of([_txn("SELL", 3, 1050.0, 10)])
    assert quantity == -3
    assert avg is None


def test_position_of_empty_history() -> None:
    assert _position_of([]) == (0, None)


def test_position_of_average_cost_worked_example() -> None:
    """Worked example: buy 10@100, sell 5, buy 5@110 -> open 10 @ 105."""
    quantity, avg = _position_of(
        [
            _txn("BUY", 10, 100.0, 10),
            _txn("SELL", 5, 150.0, 15),
            _txn("BUY", 5, 110.0, 20),
        ]
    )
    assert quantity == 10
    assert avg == pytest.approx(105.0)


def test_position_of_partial_close_keeps_running_average() -> None:
    """A partial close reduces the basis proportionally, keeping the average."""
    quantity, avg = _position_of(
        [
            _txn("BUY", 5, 1000.0, 10),
            _txn("BUY", 5, 1200.0, 12),
            _txn("SELL", 5, 1100.0, 15),
        ]
    )
    assert quantity == 5
    assert avg == pytest.approx(1100.0)


def test_position_of_full_close_resets_average() -> None:
    """After a full close, new BUYs form a fresh average."""
    quantity, avg = _position_of(
        [
            _txn("BUY", 10, 1000.0, 10),
            _txn("SELL", 10, 1050.0, 15),
            _txn("BUY", 5, 1100.0, 20),
        ]
    )
    assert quantity == 5
    assert avg == pytest.approx(1100.0)


def test_position_of_full_close_via_mature_resets_average() -> None:
    """A MATURE that fully closes the position also resets the average."""
    quantity, avg = _position_of(
        [
            _txn("BUY", 10, 1000.0, 10),
            _txn("MATURE", 10, 1000.0, 15),
            _txn("BUY", 5, 900.0, 20),
        ]
    )
    assert quantity == 5
    assert avg == pytest.approx(900.0)


# --------------------------------------------------------------------- #
# _realized_pnl_of


def test_realized_pnl_sell_known_good() -> None:
    """Task-8 known-good case: BUY 10@1000 + SELL 4@1050 (commission 5)."""
    sells, maturities, commissions = _realized_pnl_of(
        [_txn("BUY", 10, 1000.0, 10), _txn("SELL", 4, 1050.0, 15, commission=5.0)]
    )
    assert sells == pytest.approx(200.0)
    assert maturities == pytest.approx(0.0)
    assert commissions == pytest.approx(5.0)


def test_realized_pnl_mature_known_good() -> None:
    """Task-8 known-good case: BUY 10@1000 + MATURE 4@1050 → maturities=200."""
    sells, maturities, commissions = _realized_pnl_of(
        [_txn("BUY", 10, 1000.0, 10), _txn("MATURE", 4, 1050.0, 20, commission=5.0)]
    )
    assert sells == pytest.approx(0.0)
    assert maturities == pytest.approx(200.0)
    assert commissions == pytest.approx(5.0)


def test_realized_pnl_uses_cost_basis_at_time_of_exit() -> None:
    """Buys after the exit must not change that exit's cost basis."""
    sells, _, _ = _realized_pnl_of(
        [
            _txn("BUY", 5, 1000.0, 1),
            _txn("BUY", 5, 1200.0, 2),
            _txn("SELL", 5, 1100.0, 3),  # avg basis at this point is 1100
            _txn("BUY", 5, 2000.0, 4),
        ]
    )
    assert sells == pytest.approx(0.0)


def test_realized_pnl_sell_at_loss() -> None:
    sells, _, _ = _realized_pnl_of([_txn("BUY", 10, 1000.0, 10), _txn("SELL", 10, 900.0, 15)])
    assert sells == pytest.approx(-1000.0)


def test_realized_pnl_exit_without_preceding_buy_contributes_zero_pnl() -> None:
    sells, maturities, commissions = _realized_pnl_of([_txn("SELL", 2, 1050.0, 10, commission=3.0)])
    assert sells == pytest.approx(0.0)
    assert maturities == pytest.approx(0.0)
    assert commissions == pytest.approx(3.0)


def test_realized_pnl_buys_only() -> None:
    assert _realized_pnl_of([_txn("BUY", 10, 1000.0, 10)]) == (0.0, 0.0, 0.0)


# --------------------------------------------------------------------- #
# _future_coupon_dates / _last_coupon_date


def test_future_coupon_dates_annual_grid() -> None:
    """365-day grid anchored on maturity 2030-01-01, stepped back in calendar days.

    Stepping 365 days back from 2030-01-01 crosses the 2028 leap year, so the
    intermediate grid dates drift by one day: 2030-01-01 -> 2029-01-01 ->
    2028-01-02 -> 2027-01-02 -> 2026-01-03 (<= today, stops).
    """
    dates = _future_coupon_dates(datetime.date(2030, 1, 1), 365, TODAY)
    assert dates == [
        datetime.date(2027, 1, 2),
        datetime.date(2028, 1, 2),
        datetime.date(2029, 1, 1),
        datetime.date(2030, 1, 1),
    ]


def test_future_coupon_dates_semi_annual_grid() -> None:
    """182-day grid anchored on maturity 2027-01-01: 2026-07-03 and 2027-01-01."""
    dates = _future_coupon_dates(datetime.date(2027, 1, 1), 182, TODAY)
    assert dates == [datetime.date(2026, 7, 3), datetime.date(2027, 1, 1)]


def test_future_coupon_dates_matured_bond_is_empty() -> None:
    assert _future_coupon_dates(datetime.date(2026, 1, 1), 365, TODAY) == []


def test_future_coupon_dates_today_equals_maturity_is_empty() -> None:
    """Dates are strictly after today; maturity today means no future coupons."""
    assert _future_coupon_dates(TODAY, 365, TODAY) == []


def test_future_coupon_dates_zero_period_is_empty() -> None:
    """A zero-coupon bond (period_days == 0) has no coupon grid at all."""
    assert _future_coupon_dates(datetime.date(2030, 1, 1), 0, TODAY) == []


def test_last_coupon_date_annual_grid() -> None:
    """Grid anchored on 2030-01-01: the latest grid date strictly before
    2026-02-01 is 2026-01-02 (365-day steps drift across the 2028 leap year)."""
    assert _last_coupon_date(datetime.date(2030, 1, 1), 365, TODAY) == datetime.date(2026, 1, 2)


def test_last_coupon_date_semi_annual_grid() -> None:
    """Grid anchored on 2027-01-01: the last grid date before Feb 2026 is 2026-01-02."""
    assert _last_coupon_date(datetime.date(2027, 1, 1), 182, TODAY) == datetime.date(2026, 1, 2)


def test_last_coupon_date_steps_below_maturity() -> None:
    """The last coupon may lie before the bond was even issued — that is fine."""
    assert _last_coupon_date(datetime.date(2026, 6, 1), 365, TODAY) == datetime.date(2025, 6, 1)


def test_last_coupon_date_zero_period_is_none() -> None:
    """A zero-coupon bond has no last coupon date; accrued defaults to 0.0."""
    assert _last_coupon_date(datetime.date(2030, 1, 1), 0, TODAY) is None


def test_last_coupon_date_degenerate_step_returns_none() -> None:
    """Stepping below the 1900 floor bails out with None instead of looping."""
    assert _last_coupon_date(datetime.date(2026, 6, 1), 100_000, TODAY) is None


# --------------------------------------------------------------------- #
# _coupon_per_period


@pytest.mark.parametrize(
    ("coupon_rate", "coupon_period_days", "expected"),
    [
        (7.0, 365, 70.0),
        (8.0, 182, 1000 * 8 / 100 * 182 / 365),
        (8.0, 91, 1000 * 8 / 100 * 91 / 365),
        (7.0, 0, 0.0),  # zero-coupon: no coupon per period
    ],
)
def test_coupon_per_period(coupon_rate: float, coupon_period_days: int, expected: float) -> None:
    assert _coupon_per_period(
        _bond(coupon_rate=coupon_rate, coupon_period_days=coupon_period_days)
    ) == pytest.approx(expected)


def test_coupon_per_period_scales_with_nominal() -> None:
    assert _coupon_per_period(_bond(coupon_rate=7.0, nominal=2000)) == pytest.approx(140.0)


# --------------------------------------------------------------------- #
# _build_position_analytics


def test_build_position_analytics_known_good() -> None:
    """Open position with known accrued coupon, yields and next coupon date."""
    txns: Sequence[Transaction] = [_txn("BUY", 10, 1000.0, 10)]
    analytics = _build_position_analytics(_bond(), txns, TODAY)

    assert analytics.bond_id == 1
    assert analytics.isin == "RU000A0JX0J2"
    assert analytics.quantity == 10
    assert analytics.avg_buy_price == pytest.approx(1000.0)
    assert analytics.total_invested == pytest.approx(10_000.0)
    # 365-day grid anchored on 2030-01-01: first future date is 2027-01-02
    # (leap-year drift across 2028), the last grid date before today is
    # 2026-01-02.
    assert analytics.next_coupon_date == datetime.date(2027, 1, 2)
    # Annual 7% coupon on nominal 1000, 30 days since 2026-01-02 (365-day period).
    assert analytics.accrued_coupon == pytest.approx(70.0 * 30 / 365)
    assert analytics.current_yield == pytest.approx(0.07)
    assert analytics.ytm is not None
    assert analytics.ytm > 0


def test_build_position_analytics_uses_explicit_cashflows_for_ytm() -> None:
    """An explicit coupon schedule overrides the derived grid for the YTM.

    Bond: price 1000, nominal 1000, coupon 10%, period 365, maturity
    2027-02-01, cashflows = [CouponPayment(2027-02-01, 1050, final)] — the
    single payment is exactly 365 days out, so
    1000 = 1050 / (1 + r) ** (365/365) gives r = 0.05 exactly.
    """
    bond = _bond(
        coupon_rate=10.0,
        maturity_date=datetime.date(2027, 2, 1),
    )
    analytics = _build_position_analytics(
        bond,
        [_txn("BUY", 10, 1000.0, 10)],
        TODAY,
        cashflows=[
            CouponPayment(payment_date=datetime.date(2027, 2, 1), amount=1050.0, is_final=True)
        ],
    )
    assert analytics.ytm is not None
    assert analytics.ytm == pytest.approx(0.05, abs=1e-6)
    # The next-coupon grid still comes from coupon_period_days, not the schedule.
    assert analytics.next_coupon_date == datetime.date(2027, 2, 1)
    # Accrued: last grid date 2025-02-01 is exactly 365 days back, so the
    # accrued coupon is capped at one full period: 1000 * 10% * 365/365.
    assert analytics.accrued_coupon == pytest.approx(100.0)


def test_build_position_analytics_matured_bond_has_no_yields() -> None:
    analytics = _build_position_analytics(
        _bond(maturity_date=datetime.date(2020, 1, 1)),
        [_txn("BUY", 10, 1000.0, 10)],
        TODAY,
    )
    assert analytics.ytm is None
    assert analytics.current_yield is None
    assert analytics.next_coupon_date is None
    assert analytics.maturity_date == datetime.date(2020, 1, 1)


def test_build_position_analytics_after_partial_sell() -> None:
    analytics = _build_position_analytics(
        _bond(),
        [_txn("BUY", 10, 1000.0, 10), _txn("SELL", 4, 1050.0, 15)],
        TODAY,
    )
    assert analytics.quantity == 6
    assert analytics.total_invested == pytest.approx(6000.0)


def test_build_position_analytics_zero_coupon_rate() -> None:
    analytics = _build_position_analytics(
        _bond(coupon_rate=0.0), [_txn("BUY", 10, 900.0, 10)], TODAY
    )
    assert analytics.accrued_coupon == pytest.approx(0.0)
    assert analytics.current_yield == pytest.approx(0.0)


def test_build_position_analytics_zero_period_days() -> None:
    """Zero-coupon bond (period_days == 0): closed-form YTM, no coupon grid."""
    analytics = _build_position_analytics(
        _bond(coupon_period_days=0), [_txn("BUY", 10, 900.0, 10)], TODAY
    )
    assert analytics.accrued_coupon == pytest.approx(0.0)
    assert analytics.next_coupon_date is None
    # 900 for 1000 nominal: a discount bond always yields something positive.
    assert analytics.ytm is not None
    assert analytics.ytm > 0
