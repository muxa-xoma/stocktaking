"""ORM models for the bond accounting service.

Tables:
    * ``users`` — application users (JWT auth).
    * ``bonds`` — bond instruments.
    * ``transactions`` — buy/sell/maturity operations on bonds.

Position semantics (no ``position`` column is stored — it is always derived):

    position = sum(BUY.quantity) - sum(SELL.quantity) - sum(MATURE.quantity)

A position of zero means the bond is fully closed (all bought units were
sold or matured).
"""

from __future__ import annotations

# Runtime import on purpose: SQLAlchemy resolves the postponed `Mapped[datetime.date]`
# annotations against the module namespace, so `datetime` must NOT live under TYPE_CHECKING.
import datetime  # noqa: TC003

from sqlalchemy import CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bond_accounting.db.base import Base

#: Allowed values for :attr:`Transaction.type` (enforced by a CHECK constraint).
TRANSACTION_TYPES = ("BUY", "SELL", "MATURE")

#: Allowed values for :attr:`Bond.coupon_frequency` (enforced by a CHECK constraint).
COUPON_FREQUENCIES = ("ANNUAL", "SEMI_ANNUAL", "QUARTERLY")


class User(Base):
    """Application user authenticated via JWT."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="user",
        # All operations are deleted together with the user.
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, username={self.username!r})"


class Bond(Base):
    """Bond instrument."""

    __tablename__ = "bonds"
    __table_args__ = (
        CheckConstraint(
            "coupon_frequency IN ('ANNUAL', 'SEMI_ANNUAL', 'QUARTERLY')",
            name="ck_bonds_coupon_frequency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    isin: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    # Python-side ``default`` plus a DB-level ``server_default`` (migration
    # 0003): rows inserted outside the ORM (raw SQL, other clients) also get
    # 1000 when the column is omitted, matching ``BondCreate.nominal``.
    nominal: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000")
    coupon_rate: Mapped[float] = mapped_column(Float)
    coupon_frequency: Mapped[str] = mapped_column(String(20))
    maturity_date: Mapped[datetime.date] = mapped_column(Date)
    issuer: Mapped[str | None] = mapped_column(String(255), default=None)

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="bond",
        # All operations are deleted together with the bond.
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"Bond(id={self.id!r}, owner_id={self.owner_id!r}, isin={self.isin!r}, "
            f"name={self.name!r}, "
            f"nominal={self.nominal!r}, coupon_rate={self.coupon_rate!r}, "
            f"coupon_frequency={self.coupon_frequency!r}, "
            f"maturity_date={self.maturity_date!r}, issuer={self.issuer!r})"
        )


class Transaction(Base):
    """A single operation on a bond: buy, sell, or maturity redemption.

    Position semantics (see the module docstring):
    ``position = sum(BUY.quantity) - sum(SELL.quantity) - sum(MATURE.quantity)``.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('BUY', 'SELL', 'MATURE')",
            name="ck_transactions_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    bond_id: Mapped[int] = mapped_column(ForeignKey("bonds.id"))
    type: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    date: Mapped[datetime.date] = mapped_column(Date)
    commission: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="transactions")
    bond: Mapped[Bond] = relationship(back_populates="transactions")

    def __repr__(self) -> str:
        return (
            f"Transaction(id={self.id!r}, user_id={self.user_id!r}, bond_id={self.bond_id!r}, "
            f"type={self.type!r}, quantity={self.quantity!r}, price={self.price!r}, "
            f"date={self.date!r}, commission={self.commission!r})"
        )
