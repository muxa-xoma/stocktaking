"""DTOs for the portfolio module: transaction input/output and positions.

Position semantics (mirrors :mod:`bond_accounting.db.models`):

    quantity = sum(BUY.quantity) - sum(SELL.quantity) - sum(MATURE.quantity)

``avg_buy_price`` is the average-cost price of the *currently open*
position (see :func:`bond_accounting.portfolio.netting.net_position`):
BUYs add to the cost basis, SELL/MATURE reduce it proportionally (so a
partial close leaves the average unchanged), and a full close resets it
so subsequent BUYs form a new average. It is ``None`` when nothing is
currently held, and ``total_invested`` is that average times the
currently held quantity.

Validation: ``price`` must be strictly positive — ``POST /api/transactions``
with ``price <= 0`` is rejected with HTTP 422 (a non-positive price is a
data-entry error and would produce meaningless analytics).
"""

from __future__ import annotations

# Runtime import on purpose: Pydantic resolves the postponed annotations
# against the module namespace when building the model schema, so `datetime`
# must NOT live under TYPE_CHECKING.
import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from bond_accounting.db.models import Transaction

#: Allowed transaction types (mirrors ``TRANSACTION_TYPES`` in ``db.models``).
TransactionType = Literal["BUY", "SELL", "MATURE"]


class TransactionCreate(BaseModel):
    """Input payload for recording a new transaction.

    Attributes:
        bond_id: Target bond.
        broker_account_id: Broker account the transaction is executed on;
            must be a positive identifier.
        type: Operation kind; one of ``BUY``, ``SELL``, ``MATURE``.
        quantity: Number of bond units; must be strictly positive.
        price: Price per unit; must be strictly positive.
        date: Operation date.
        commission: Broker commission for the operation.
    """

    bond_id: int
    broker_account_id: int = Field(
        gt=0,
        description="Broker account the transaction is executed on.",
    )
    type: TransactionType
    quantity: int = Field(gt=0, description="Number of bond units; must be positive.")
    price: float = Field(gt=0, description="Price per unit; must be positive.")
    date: datetime.date
    commission: float = Field(default=0.0, description="Broker commission.")


class TransactionDTO(BaseModel):
    """A recorded transaction as returned by the portfolio service.

    Attributes:
        id: Database identifier.
        user_id: Owning user.
        bond_id: Target bond.
        broker_account_id: Broker account the transaction was executed on.
        type: Operation kind (``BUY`` / ``SELL`` / ``MATURE``).
        quantity: Number of bond units.
        price: Price per unit.
        date: Operation date.
        commission: Broker commission.
        created_at: Row creation timestamp.
    """

    id: int
    user_id: int
    bond_id: int
    broker_account_id: int
    type: str
    quantity: int
    price: float
    date: datetime.date
    commission: float
    created_at: datetime.datetime

    @classmethod
    def from_orm(cls, obj: Transaction) -> TransactionDTO:
        """Build the DTO from a :class:`~bond_accounting.db.models.Transaction` row.

        Args:
            obj: ORM object (must be persistent — ``id`` and ``created_at``
                populated; the session factory uses ``expire_on_commit=False``,
                so a committed object works without a refresh).

        Returns:
            The corresponding :class:`TransactionDTO`.
        """
        return cls(
            id=obj.id,
            user_id=obj.user_id,
            bond_id=obj.bond_id,
            broker_account_id=obj.broker_account_id,
            type=obj.type,
            quantity=obj.quantity,
            price=obj.price,
            date=obj.date,
            commission=obj.commission,
            created_at=obj.created_at,
        )


class PositionDTO(BaseModel):
    """Current position for one user-bond pair.

    Attributes:
        user_id: Owning user.
        bond_id: Target bond.
        quantity: Currently held units; ``0`` when the position is closed.
        avg_buy_price: Average-cost price of the currently open position
            (``None`` when nothing is currently held; after a full close
            subsequent BUYs form a new average).
        total_invested: ``avg_buy_price * quantity`` for the current holding.
    """

    user_id: int
    bond_id: int
    quantity: int
    avg_buy_price: float | None = None
    total_invested: float
