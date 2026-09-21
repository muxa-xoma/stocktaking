"""Average-cost position netting over a bond's transaction history.

Quantity semantics (mirrors :mod:`bond_accounting.db.models`, unchanged):

    quantity = sum(BUY.quantity) - sum(SELL.quantity) - sum(MATURE.quantity)

Two public functions share one **average-cost state machine** (chosen
over FIFO because it is simpler and conceptually closest to the previous
weighted-average figure): BUYs add ``price * quantity`` to the cost
basis, SELL/MATURE reduce the cost basis proportionally to the closed
quantity, and a full close (open quantity reaching 0) resets the basis
so subsequent BUYs form a new average.

* :func:`net_position` derives the *currently open* position —
  ``(quantity, avg_buy_price)``.
* :func:`realized_pnl` values every SELL/MATURE against the running
  average cost at the time of the exit. Because the state resets on a
  full close, an exit after a reopen is priced against the post-reopen
  average only — never blended with the pre-close history.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bond_accounting.db.models import Transaction

logger = logging.getLogger(__name__)


class _NettingState(NamedTuple):
    """Final state of the shared average-cost walk (see :func:`_netting`)."""

    #: Held quantity: BUY minus SELL minus MATURE (possibly negative for a corrupt history).
    quantity: int
    #: Currently open quantity (clamped on defensive closes).
    open_quantity: int
    #: Cost basis of the currently open position.
    cost_basis: float
    #: Realized PnL summed over SELL exits.
    sells: float
    #: Realized PnL summed over MATURE exits.
    maturities: float
    #: Commissions summed over SELL/MATURE rows.
    commissions: float


def _netting(txns: Sequence[Transaction]) -> _NettingState:
    """Walk ``txns`` through the average-cost state machine shared by both
    :func:`net_position` and :func:`realized_pnl`.

    The transactions are processed chronologically; the caller must supply
    them ordered by ``(date, id)`` — ``id`` (primary key / insertion
    order) is the deterministic tie-break for equal dates.

    Algorithm (average-cost, not FIFO):

    * ``BUY``: ``cost_basis += price * quantity``; quantities grow.
    * ``SELL``/``MATURE`` with ``q <= open quantity``: the closed part is
      valued at the running average, i.e.
      ``pnl = (price - cost_basis / open_quantity) * q``, and the cost
      basis drops proportionally, so the remaining average is unchanged.
    * A close that exceeds the open quantity cannot normally happen
      (``InsufficientPositionError`` guards inserts), but is handled
      defensively: the close is clamped to the open quantity and a warning
      is logged instead of raising.
    * An exit with no open position (no preceding BUY since the last full
      close) contributes 0 PnL; a warning is logged.
    * When the open quantity reaches 0 the cost basis is reset to 0, so
      BUYs made after the full close start a new average instead of being
      blended into the pre-close history.

    Args:
        txns: One bond's transactions, ordered by ``(date, id)``.

    Returns:
        The final state of the walk (see :class:`_NettingState`).
    """
    quantity = 0
    open_quantity = 0
    cost_basis = 0.0
    sells = 0.0
    maturities = 0.0
    commissions = 0.0
    for txn in txns:
        if txn.type == "BUY":
            quantity += txn.quantity
            open_quantity += txn.quantity
            cost_basis += txn.price * txn.quantity
            continue

        # SELL / MATURE (CHECK-constrained in the DB).
        quantity -= txn.quantity
        commissions += txn.commission
        if open_quantity <= 0:
            logger.warning(
                "Transaction id=%s type=%s bond_id=%s has no preceding BUY; PnL contribution is 0",
                txn.id,
                txn.type,
                txn.bond_id,
            )
            continue
        if txn.quantity > open_quantity:
            logger.warning(
                "Transaction id=%s type=%s bond_id=%s closes %s unit(s) but only %s are open; clamping to the open quantity",
                txn.id,
                txn.type,
                txn.bond_id,
                txn.quantity,
                open_quantity,
            )
            closed = open_quantity
        else:
            closed = txn.quantity
        pnl = (txn.price - cost_basis / open_quantity) * closed
        if txn.type == "SELL":
            sells += pnl
        else:  # MATURE
            maturities += pnl
        cost_basis -= cost_basis * (closed / open_quantity)
        open_quantity -= closed
        if open_quantity == 0:
            # Avoid a floating-point residue after a full close.
            cost_basis = 0.0
    return _NettingState(quantity, open_quantity, cost_basis, sells, maturities, commissions)


def net_position(txns: Sequence[Transaction]) -> tuple[int, float | None]:
    """Derive ``(quantity, avg_buy_price)`` from a bond's transactions.

    Thin projection over the shared average-cost walk (see
    :func:`_netting`); the algorithm, clamping and full-close reset are
    documented there.

    Worked example::

        BUY 10 @ 100          -> open qty 10, cost basis 1000, avg 100
        SELL 5 @ any price    -> open qty  5, cost basis  500, avg 100
        BUY 5 @ 110           -> open qty 10, cost basis 1050, avg 105
        SELL 10               -> open qty  0, avg None
        BUY 5 @ 120           -> open qty  5, avg 120 (fresh average)

    Args:
        txns: One bond's transactions, ordered by ``(date, id)``.

    Returns:
        Tuple of the held quantity (BUY minus SELL minus MATURE, possibly
        negative for a corrupt history) and the average-cost price of the
        currently open position — ``None`` when the position is flat.
    """
    state = _netting(txns)
    avg_buy_price = state.cost_basis / state.open_quantity if state.open_quantity > 0 else None
    return state.quantity, avg_buy_price


def realized_pnl(txns: Sequence[Transaction]) -> tuple[float, float, float]:
    """Realized PnL contributions ``(sells, maturities, commissions)``.

    Computed on the same average-cost basis as :func:`net_position`
    (see :func:`_netting`): each SELL/MATURE is valued against the
    running average cost of the position open at that moment, and a full
    close resets the basis, so exits after a reopen are priced against
    the post-reopen average only.

    Worked example::

        BUY 10 @ 100, SELL 10 @ 120  -> sells = (120 - 100) * 10 = 200
        BUY 5 @ 200,  SELL 5 @ 210   -> sells += (210 - 200) * 5 = 250 total
        (the second BUY forms a fresh average of 200 after the full close)

    Args:
        txns: One bond's transactions, ordered by ``(date, id)``.

    Returns:
        Tuple ``(sells, maturities, commissions)`` where ``sells`` is the
        sum of ``(price - avg_cost_at_that_time) * closed_quantity`` over
        SELL transactions, ``maturities`` is the same over MATURE, and
        ``commissions`` is the sum of ``commission`` over all SELL/MATURE
        rows (summed regardless of the open position).
    """
    state = _netting(txns)
    return state.sells, state.maturities, state.commissions
