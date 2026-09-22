"""Pydantic DTOs for the account operations module.

Input models (:class:`AccountOperationCreate`, :class:`AccountOperationUpdate`)
validate data at the service boundary; :class:`AccountOperationDTO` is the
canonical read shape returned by
:class:`~bond_accounting.account_operations.service.AccountOperationService`.
"""

from __future__ import annotations

# Runtime import on purpose: pydantic resolves the postponed `datetime.date`
# annotations against the module namespace (same as db/models.py).
import datetime  # noqa: TC003
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from bond_accounting.db.models import AccountOperation

#: Allowed account operation types; matches the DB CHECK constraint
#: (``ck_account_operations_type``) and :data:`ACCOUNT_OPERATION_TYPES`.
AccountOperationType = Literal["DEPOSIT", "WITHDRAWAL", "TAX"]


class AccountOperationCreate(BaseModel):
    """Input for creating an account operation owned by the calling user.

    Raises:
        pydantic.ValidationError: If any field is invalid — in particular
            when ``type`` is not one of ``DEPOSIT``, ``WITHDRAWAL``, ``TAX``
            or ``amount`` is not positive.
    """

    broker_account_id: int = Field(description="Id of the caller's broker account.")
    type: AccountOperationType = Field(description="Operation type: DEPOSIT, WITHDRAWAL or TAX.")
    amount: float = Field(gt=0, description="Operation amount; must be positive.")
    date: datetime.date = Field(description="Operation date.")
    note: str | None = Field(default=None, max_length=500, description="Optional free-form note.")


class AccountOperationUpdate(BaseModel):
    """Partial update input: only explicitly provided fields are applied.

    The service dumps this model with ``exclude_unset=True``, so omitted
    fields leave the corresponding columns untouched. An explicit ``null``
    is *not* accepted as a value for most fields: a ``None`` for a
    non-nullable column is silently ignored (treated as "not provided" by
    the service). The exception is :attr:`NULLABLE_FIELDS`: an explicitly
    provided ``null`` for those fields clears the column in the database.
    """

    #: Fields whose columns are nullable — the only ones where an explicit
    #: ``null`` in an update payload is meaningful and clears the column.
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"note"})

    broker_account_id: int | None = Field(
        default=None, description="Move the operation to another of the caller's accounts."
    )
    type: AccountOperationType | None = Field(
        default=None, description="Operation type: DEPOSIT, WITHDRAWAL or TAX."
    )
    amount: float | None = Field(default=None, gt=0, description="Operation amount; positive.")
    date: datetime.date | None = Field(default=None, description="Operation date.")
    note: str | None = Field(default=None, max_length=500, description="Optional free-form note.")


class AccountOperationDTO(BaseModel):
    """Read shape of an account operation."""

    id: int
    user_id: int
    broker_account_id: int
    type: str
    amount: float
    date: datetime.date
    note: str | None
    created_at: datetime.datetime

    @classmethod
    def from_orm(cls, obj: AccountOperation) -> AccountOperationDTO:
        """Build a DTO from an :class:`~bond_accounting.db.models.AccountOperation` ORM object."""
        return cls(
            id=obj.id,
            user_id=obj.user_id,
            broker_account_id=obj.broker_account_id,
            type=obj.type,
            amount=obj.amount,
            date=obj.date,
            note=obj.note,
            created_at=obj.created_at,
        )
