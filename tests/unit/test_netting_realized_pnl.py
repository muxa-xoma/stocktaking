"""Unit tests for ``portfolio.netting.realized_pnl`` (average-cost semantics).

Regression tests for the fix-loop finding F1 (Task 50): realized PnL must
be computed on the same average-cost basis as ``net_position`` — each
SELL/MATURE is valued against the running average of the position open at
that moment, and a full close resets the basis so an exit after a reopen
is priced against the post-reopen average only (never blended with the
pre-close history).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest

from bond_accounting.analytics.service import _realized_pnl_of
from bond_accounting.db.models import Transaction
from bond_accounting.portfolio.netting import net_position, realized_pnl

if TYPE_CHECKING:
    from collections.abc import Sequence

#: (type, quantity, price, day, commission) — one row per transaction.
Scenario = list[tuple[str, int, float, int, float]]

_REOPEN_SCENARIO: Scenario = [
    ("BUY", 10, 100.0, 1, 0.0),
    ("SELL", 10, 120.0, 5, 0.0),
    ("BUY", 5, 200.0, 10, 0.0),
    ("SELL", 5, 210.0, 15, 0.0),
]


def _txns(rows: Sequence[tuple[str, int, float, int, float]]) -> list[Transaction]:
    """Build in-memory ``Transaction`` rows dated 2026-01-<day>, in order."""
    return [
        Transaction(
            id=index,
            user_id=1,
            bond_id=1,
            type=type_,
            quantity=quantity,
            price=price,
            date=datetime.date(2026, 1, day),
            commission=commission,
        )
        for index, (type_, quantity, price, day, commission) in enumerate(rows, start=1)
    ]


# --------------------------------------------------------------------- #
# realized_pnl: average-cost semantics


def test_full_close_resets_average_for_realized_pnl() -> None:
    """Key regression: BUY 10@100, SELL 10@120, BUY 5@200, SELL 5@210 -> 250.

    The old all-history-average formula would price the second SELL against
    (10*100 + 5*200) / 15 = 133.33 and report 583.33; the post-close BUY forms
    a fresh average of 200, so the answer is 200 + 50 = 250.
    """
    sells, maturities, commissions = realized_pnl(_txns(_REOPEN_SCENARIO))

    assert sells == pytest.approx(250.0)
    assert maturities == pytest.approx(0.0)
    assert commissions == pytest.approx(0.0)


def test_partial_close_keeps_running_average() -> None:
    """BUY 10@100, SELL 5@110, SELL 5@120 -> 50 + 100 = 150."""
    rows: Scenario = [
        ("BUY", 10, 100.0, 1, 0.0),
        ("SELL", 5, 110.0, 5, 0.0),
        ("SELL", 5, 120.0, 10, 0.0),
    ]

    sells, maturities, commissions = realized_pnl(_txns(rows))

    assert sells == pytest.approx(150.0)
    assert maturities == pytest.approx(0.0)
    assert commissions == pytest.approx(0.0)


def test_mature_pnl_valued_against_average_cost() -> None:
    """BUY 10@100, MATURE 10@1000 -> maturities = (1000 - 100) * 10 = 9000."""
    rows: Scenario = [
        ("BUY", 10, 100.0, 1, 0.0),
        ("MATURE", 10, 1000.0, 20, 0.0),
    ]

    sells, maturities, commissions = realized_pnl(_txns(rows))

    assert maturities == pytest.approx(9000.0)
    assert sells == pytest.approx(0.0)
    assert commissions == pytest.approx(0.0)


def test_commissions_summed_over_exit_rows_only() -> None:
    """Commissions of SELL/MATURE rows are summed; BUY commissions excluded."""
    rows: Scenario = [
        ("BUY", 10, 100.0, 1, 5.0),
        ("SELL", 5, 120.0, 5, 2.0),
        ("MATURE", 5, 1000.0, 10, 1.5),
    ]

    sells, maturities, commissions = realized_pnl(_txns(rows))

    assert commissions == pytest.approx(3.5)
    assert sells == pytest.approx(100.0)
    assert maturities == pytest.approx(4500.0)


def test_exit_without_open_position_contributes_zero() -> None:
    """SELL with no preceding BUY contributes 0 PnL and does not crash."""
    rows: Scenario = [("SELL", 5, 120.0, 1, 3.0)]

    sells, maturities, commissions = realized_pnl(_txns(rows))

    assert sells == pytest.approx(0.0)
    assert maturities == pytest.approx(0.0)
    assert commissions == pytest.approx(3.0)


def test_close_exceeding_open_quantity_is_clamped() -> None:
    """A corrupt history closing more than open is clamped to the open quantity.

    BUY 5@100, SELL 10@120: only the 5 open units count, so sells = (120 - 100)*5;
    ``net_position`` mirrors the same defensive clamping (flat open position,
    negative book quantity).
    """
    rows: Scenario = [
        ("BUY", 5, 100.0, 1, 0.0),
        ("SELL", 10, 120.0, 5, 0.0),
    ]
    txns = _txns(rows)

    sells, maturities, commissions = realized_pnl(txns)

    assert sells == pytest.approx(100.0)
    assert maturities == pytest.approx(0.0)
    assert commissions == pytest.approx(0.0)
    assert net_position(txns) == (-5, None)


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(_REOPEN_SCENARIO, id="reopen-after-full-close"),
        pytest.param(
            [
                ("BUY", 10, 100.0, 1, 0.0),
                ("SELL", 5, 110.0, 5, 0.0),
                ("SELL", 5, 120.0, 10, 0.0),
            ],
            id="partial-closes",
        ),
        pytest.param(
            [
                ("BUY", 10, 100.0, 1, 0.0),
                ("MATURE", 10, 1000.0, 20, 0.0),
            ],
            id="maturity",
        ),
        pytest.param([("SELL", 5, 120.0, 1, 3.0)], id="exit-without-open-position"),
    ],
)
def test_analytics_wrapper_matches_netting(rows: Scenario) -> None:
    """``analytics._realized_pnl_of`` is a thin wrapper over ``realized_pnl``.

    The analytics-side aggregation must not diverge from the netting walk.
    """
    txns = _txns(rows)

    assert _realized_pnl_of(txns) == realized_pnl(txns)
