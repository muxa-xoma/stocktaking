"""Pydantic DTOs for the bonds module.

Input models (:class:`BondCreate`, :class:`BondUpdate`) validate data at the
service boundary; :class:`BondDTO` is the canonical read shape returned by
:class:`~bond_accounting.bonds.service.BondService` and the payload of the
``bond.*`` event-bus topics.
"""

from __future__ import annotations

# Runtime import on purpose: pydantic resolves the postponed `datetime.date`
# annotations against the module namespace (same as db/models.py).
import datetime  # noqa: TC003
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from bond_accounting.db.models import Bond

#: ISIN format: exactly 12 uppercase alphanumeric characters.
ISIN_PATTERN = r"^[A-Z0-9]{12}$"


class BondCreate(BaseModel):
    """Input for creating a bond.

    Raises:
        pydantic.ValidationError: If any field is invalid — in particular
            when ``isin`` is not exactly 12 uppercase alphanumeric characters.
    """

    isin: str = Field(
        pattern=ISIN_PATTERN,
        description="ISIN: exactly 12 uppercase alphanumeric characters.",
    )
    name: str = Field(description="Human-readable bond name.")
    # 1000 mirrors the DB-level DEFAULT on ``bonds.nominal`` (migration 0003),
    # so the same default applies whether the row is inserted by the app or
    # by anything else writing to the table. No upper bound on purpose:
    # large-denomination bonds are valid. Note that YTM / current-yield
    # analytics require the transaction price (per bond unit) to be within
    # [1, 10 * nominal]; outside that range ``calculate_ytm`` raises and the
    # position's ``ytm`` / ``current_yield`` become None.
    nominal: int = Field(
        default=1000,
        gt=0,
        description=(
            "Face value in currency units. Must be positive; defaults to 1000. "
            "YTM/current-yield analytics require the transaction price per bond unit "
            "to be within [1, 10 * nominal]; outside that range the position's "
            "ytm/current_yield become None."
        ),
    )
    # ``ge=0``: a 0% coupon bond (zero-coupon) is a valid instrument, so only
    # negative rates are rejected.
    coupon_rate: float = Field(
        ge=0,
        description=(
            "Annual coupon rate in percent, e.g. 12.5 means 12.5%. "
            "Zero is valid (zero-coupon bond)."
        ),
    )
    coupon_period_days: int = Field(
        ge=0,
        description="Days between coupon payments; 0 = zero-coupon bond.",
    )
    maturity_date: datetime.date = Field(description="Date the principal is repaid.")
    issuer: str | None = Field(default=None, description="Optional issuer name.")


class BondUpdate(BaseModel):
    """Partial update input: only explicitly provided fields are applied.

    The service dumps this model with ``exclude_unset=True``, so omitted
    fields leave the corresponding columns untouched. ``None`` is *not*
    accepted as a value for most fields: a ``None`` for a non-nullable column
    is silently ignored (treated as "not provided" by the service). The only
    exception is ``issuer`` (see :attr:`NULLABLE_FIELDS`): an explicitly
    provided ``null`` clears the column in the database.
    """

    #: Fields whose columns are nullable — the only ones where an explicit
    #: ``null`` in an update payload is meaningful and clears the column.
    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"issuer"})

    name: str | None = None
    # Positive-only: ``nominal`` has no default here (defaults only make
    # sense on create) and an explicit ``null`` is ignored by the service.
    nominal: int | None = Field(default=None, gt=0)
    coupon_rate: float | None = Field(default=None, ge=0)
    coupon_period_days: int | None = Field(default=None, ge=0)
    maturity_date: datetime.date | None = None
    issuer: str | None = None


class BondDTO(BaseModel):
    """Read shape of a bond, also used as the ``bond.created`` / ``bond.updated`` event payload.

    ``owner_id`` is read-only: it is set by the service from the creating
    user and never taken from request bodies. For ``bond.updated`` /
    ``bond.deleted`` events the service additionally stamps the affected
    holder's ``user_id`` on top of this shape (see
    :meth:`~bond_accounting.bonds.service.BondService._publish_for_holders`).
    """

    id: int
    owner_id: int
    isin: str
    name: str
    nominal: int
    coupon_rate: float
    coupon_period_days: int
    maturity_date: datetime.date
    issuer: str | None = None

    @classmethod
    def from_orm(cls, obj: Bond) -> BondDTO:
        """Build a DTO from a :class:`~bond_accounting.db.models.Bond` ORM object."""
        return cls(
            id=obj.id,
            owner_id=obj.owner_id,
            isin=obj.isin,
            name=obj.name,
            nominal=obj.nominal,
            coupon_rate=obj.coupon_rate,
            coupon_period_days=obj.coupon_period_days,
            maturity_date=obj.maturity_date,
            issuer=obj.issuer,
        )
