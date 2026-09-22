"""Pydantic DTOs for the brokers module.

Input models (:class:`BrokerCreate`, :class:`BrokerUpdate`,
:class:`BrokerAccountCreate`, :class:`BrokerAccountUpdate`) validate data at
the service boundary; :class:`BrokerDTO` / :class:`BrokerAccountDTO` are the
canonical read shapes returned by
:class:`~bond_accounting.brokers.service.BrokerService` and the payloads of
the ``broker.*`` / ``broker_account.*`` event-bus topics.
"""

from __future__ import annotations

# Runtime import on purpose: pydantic resolves the postponed `datetime.date`
# annotations against the module namespace (same as db/models.py).
import datetime  # noqa: TC003
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from bond_accounting.db.models import Broker, BrokerAccount

#: Allowed broker account types; matches the DB CHECK constraint
#: (``ck_broker_accounts_type``) and :data:`BROKER_ACCOUNT_TYPES`.
BrokerAccountType = Literal["STANDARD", "IIS", "LTD"]

#: How :attr:`Broker.min_commission` is interpreted; matches the DB CHECK
#: constraint (``ck_brokers_min_commission_type``) and
#: :data:`MIN_COMMISSION_TYPES`.
MinCommissionType = Literal["PERCENT", "RUBLES"]


class BrokerCreate(BaseModel):
    """Input for creating a broker.

    Raises:
        pydantic.ValidationError: If any field is invalid — in particular
            when ``commission`` / ``min_commission`` are negative or
            ``min_commission_type`` is not one of ``PERCENT``, ``RUBLES``.
    """

    name: str = Field(description="Unique broker name.")
    commission: float = Field(
        ge=0,
        default=0.0,
        description="Commission percent (e.g. 5.0 = 5%).",
    )
    min_commission: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional minimum commission (percent or fixed rubles depending "
            "on min_commission_type)."
        ),
    )
    min_commission_type: MinCommissionType = Field(
        default="PERCENT",
        description="Interpretation of min_commission: percent or fixed rubles.",
    )
    description: str | None = Field(default=None, description="Optional free-form description.")


class BrokerUpdate(BaseModel):
    """Partial update input: only explicitly provided fields are applied.

    The service dumps this model with ``exclude_unset=True``, so omitted
    fields leave the corresponding columns untouched. ``None`` is *not*
    accepted as a value for most fields: a ``None`` for a non-nullable
    column is silently ignored (treated as "not provided" by the service).
    The exception is :attr:`NULLABLE_FIELDS`: an explicitly provided
    ``null`` for those fields clears the column in the database.
    """

    #: Fields whose columns are nullable — the only ones where an explicit
    #: ``null`` in an update payload is meaningful and clears the column.
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"min_commission", "description"})

    name: str | None = None
    commission: float | None = Field(
        default=None, ge=0, description="Commission percent (e.g. 5.0 = 5%)."
    )
    min_commission: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Minimum commission (percent or fixed rubles depending on min_commission_type)."
        ),
    )
    min_commission_type: MinCommissionType | None = Field(
        default=None,
        description="Interpretation of min_commission: percent or fixed rubles.",
    )
    description: str | None = None


class BrokerDTO(BaseModel):
    """
    Read shape of a broker, also used as the ``broker.created`` / ``broker.updated`` event payload.
    """

    id: int
    name: str
    commission: float
    min_commission: float | None
    min_commission_type: str
    description: str | None
    created_at: datetime.datetime

    @classmethod
    def from_orm(cls, obj: Broker) -> BrokerDTO:
        """Build a DTO from a :class:`~bond_accounting.db.models.Broker` ORM object."""
        return cls(
            id=obj.id,
            name=obj.name,
            commission=obj.commission,
            min_commission=obj.min_commission,
            min_commission_type=obj.min_commission_type,
            description=obj.description,
            created_at=obj.created_at,
        )


class BrokerAccountCreate(BaseModel):
    """Input for creating a broker account owned by the calling user.

    Raises:
        pydantic.ValidationError: If any field is invalid — in particular
            when ``account_type`` is not one of ``STANDARD``, ``IIS``, ``LTD``.
    """

    broker_id: int = Field(description="Id of the broker the account is opened with.")
    name: str = Field(description="Human-readable account name.")
    account_number: str | None = Field(
        default=None, description="Optional account number at the broker."
    )
    account_type: BrokerAccountType = Field(description="Account type: STANDARD, IIS or LTD.")
    opened_at: datetime.date | None = Field(default=None, description="Optional opening date.")


class BrokerAccountUpdate(BaseModel):
    """Partial update input: only explicitly provided fields are applied.

    Same semantics as :class:`BrokerUpdate` — see its docstring; the
    nullable fields are listed in :attr:`NULLABLE_FIELDS`.
    """

    #: Fields whose columns are nullable — the only ones where an explicit
    #: ``null`` in an update payload is meaningful and clears the column.
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"account_number", "opened_at", "closed_at"}
    )

    name: str | None = None
    account_number: str | None = None
    account_type: BrokerAccountType | None = None
    opened_at: datetime.date | None = None
    closed_at: datetime.date | None = None


class BrokerAccountDTO(BaseModel):
    """
    Read shape of a broker account,
    also used as the ``broker_account.created`` / ``broker_account.updated`` event payload.
    """

    id: int
    user_id: int
    broker_id: int
    name: str
    account_number: str | None
    account_type: str
    opened_at: datetime.date | None
    closed_at: datetime.date | None
    created_at: datetime.datetime
    broker_name: str

    @classmethod
    def from_orm(cls, obj: BrokerAccount, *, broker_name: str = "") -> BrokerAccountDTO:
        """Build a DTO from a :class:`~bond_accounting.db.models.BrokerAccount` ORM object.

        ``broker_name`` is taken from the joined ``Broker`` row (the
        service always queries accounts together with their broker) and
        defaults to an empty string for callers that have no broker at
        hand.
        """
        return cls(
            id=obj.id,
            user_id=obj.user_id,
            broker_id=obj.broker_id,
            name=obj.name,
            account_number=obj.account_number,
            account_type=obj.account_type,
            opened_at=obj.opened_at,
            closed_at=obj.closed_at,
            created_at=obj.created_at,
            broker_name=broker_name,
        )
