"""ORM models for the bond accounting service.

Tables:
    * ``users`` — application users (JWT auth).
    * ``brokers`` — brokerage companies (commission settings).
    * ``bonds`` — bond instruments.
    * ``bond_coupons`` — actual coupon payment schedule rows per bond
      (optional override for the period-based derivation).
    * ``broker_accounts`` — a user's account at a specific broker.
    * ``transactions`` — buy/sell/maturity operations on bonds.
    * ``account_operations`` — non-trading money movements on a broker
      account (deposits, withdrawals, taxes).

Position semantics (no ``position`` column is stored — it is always derived):

    position = sum(BUY.quantity) - sum(SELL.quantity) - sum(MATURE.quantity)

A position of zero means the bond is fully closed (all bought units were
sold or matured).
"""

from __future__ import annotations

# Runtime import on purpose: SQLAlchemy resolves the postponed `Mapped[datetime.date]`
# annotations against the module namespace, so `datetime` must NOT live under TYPE_CHECKING.
import datetime  # noqa: TC003

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bond_accounting.db.base import Base

#: Allowed values for :attr:`Transaction.type` (enforced by a CHECK constraint).
TRANSACTION_TYPES = ("BUY", "SELL", "MATURE")

#: Allowed values for :attr:`BrokerAccount.account_type` (enforced by a CHECK constraint).
BROKER_ACCOUNT_TYPES = ("STANDARD", "IIS", "LTD")

#: Allowed values for :attr:`AccountOperation.type` (enforced by a CHECK constraint).
ACCOUNT_OPERATION_TYPES = ("DEPOSIT", "WITHDRAWAL", "TAX")

#: Allowed values for :attr:`Broker.min_commission_type` (enforced by a CHECK constraint).
MIN_COMMISSION_TYPES = ("PERCENT", "RUBLES")


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
    accounts: Mapped[list[BrokerAccount]] = relationship(
        back_populates="user",
        # Broker accounts are deleted together with the user.
        cascade="all, delete-orphan",
    )
    account_operations: Mapped[list[AccountOperation]] = relationship(
        back_populates="user",
        # All operations are deleted together with the user.
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, username={self.username!r})"


class Broker(Base):
    """Brokerage company with its commission settings."""

    __tablename__ = "brokers"
    __table_args__ = (
        CheckConstraint(
            "min_commission_type IN ('PERCENT', 'RUBLES')",
            name="ck_brokers_min_commission_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    min_commission: Mapped[float | None] = mapped_column(Float, default=None)
    # Python-side ``default`` plus a DB-level ``server_default`` so rows
    # inserted outside the ORM also get 'PERCENT' when the column is omitted
    # (same pattern as ``Bond.nominal``).
    min_commission_type: Mapped[str] = mapped_column(
        String(10),
        default="PERCENT",
        server_default="PERCENT",
    )
    description: Mapped[str | None] = mapped_column(String(1000), default=None)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )

    accounts: Mapped[list[BrokerAccount]] = relationship(
        back_populates="broker",
        # Accounts are deleted together with the broker.
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"Broker(id={self.id!r}, name={self.name!r}, "
            f"commission={self.commission!r}, min_commission={self.min_commission!r})"
        )


class Bond(Base):
    """Bond instrument."""

    __tablename__ = "bonds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    isin: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    # Python-side ``default`` plus a DB-level ``server_default``: rows
    # inserted outside the ORM (raw SQL, other clients) also get 1000 when
    # the column is omitted, matching ``BondCreate.nominal``.
    nominal: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000")
    coupon_rate: Mapped[float] = mapped_column(Float)
    # Calendar days between coupon payments (MOEX ``couponperiod`` stored as-is);
    # 0 = zero-coupon bond. Python-side ``default`` plus a DB-level
    # ``server_default``: rows inserted outside the ORM also get 182 when the
    # column is omitted (same pattern as ``Bond.nominal``).
    coupon_period_days: Mapped[int] = mapped_column(
        Integer,
        default=182,
        server_default="182",
    )
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
            f"coupon_period_days={self.coupon_period_days!r}, "
            f"maturity_date={self.maturity_date!r}, issuer={self.issuer!r})"
        )


class BondCoupon(Base):
    """Actual coupon payment schedule row for a bond (optional override).

    When the table is populated (e.g. from MOEX ``bondization`` at bond
    creation), the actual dates/amounts take priority over the schedule
    derived from :attr:`Bond.coupon_period_days`.
    """

    __tablename__ = "bond_coupons"
    __table_args__ = (UniqueConstraint("bond_id", "coupon_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bond_id: Mapped[int] = mapped_column(
        ForeignKey("bonds.id", ondelete="CASCADE"),
        index=True,
    )
    coupon_date: Mapped[datetime.date] = mapped_column(Date)
    coupon_amount: Mapped[float] = mapped_column(Float)

    def __repr__(self) -> str:
        return (
            f"BondCoupon(id={self.id!r}, bond_id={self.bond_id!r}, "
            f"coupon_date={self.coupon_date!r}, coupon_amount={self.coupon_amount!r})"
        )


class BrokerAccount(Base):
    """A user's account at a specific broker.

    ``account_type`` is restricted to :data:`BROKER_ACCOUNT_TYPES`
    (``STANDARD``, ``IIS``, ``LTD``) by a CHECK constraint.
    """

    __tablename__ = "broker_accounts"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('STANDARD', 'IIS', 'LTD')",
            name="ck_broker_accounts_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    account_number: Mapped[str | None] = mapped_column(String(100), default=None)
    account_type: Mapped[str] = mapped_column(String(20))
    opened_at: Mapped[datetime.date | None] = mapped_column(Date, default=None)
    closed_at: Mapped[datetime.date | None] = mapped_column(Date, default=None)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="accounts")
    broker: Mapped[Broker] = relationship(back_populates="accounts")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="broker_account",
        # All operations are deleted together with the account.
        cascade="all, delete-orphan",
    )
    account_operations: Mapped[list[AccountOperation]] = relationship(
        back_populates="broker_account",
        # All operations are deleted together with the account.
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"BrokerAccount(id={self.id!r}, user_id={self.user_id!r}, "
            f"broker_id={self.broker_id!r}, name={self.name!r}, "
            f"account_type={self.account_type!r})"
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
    broker_account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id"),
        index=True,
    )
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
    broker_account: Mapped[BrokerAccount] = relationship(back_populates="transactions")

    def __repr__(self) -> str:
        return (
            f"Transaction(id={self.id!r}, user_id={self.user_id!r}, bond_id={self.bond_id!r}, "
            f"broker_account_id={self.broker_account_id!r}, type={self.type!r}, "
            f"quantity={self.quantity!r}, price={self.price!r}, "
            f"date={self.date!r}, commission={self.commission!r})"
        )


class AccountOperation(Base):
    """A non-trading money movement on a broker account.

    ``type`` is restricted to :data:`ACCOUNT_OPERATION_TYPES`
    (``DEPOSIT``, ``WITHDRAWAL``, ``TAX``) by a CHECK constraint. ``amount``
    must be positive; this is validated at the API/service layer (Pydantic),
    not by the database.
    """

    __tablename__ = "account_operations"
    __table_args__ = (
        CheckConstraint(
            "type IN ('DEPOSIT', 'WITHDRAWAL', 'TAX')",
            name="ck_account_operations_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    broker_account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id"),
        index=True,
    )
    type: Mapped[str] = mapped_column(String(10))
    # Positive amount enforced at the service layer (Pydantic), not in the DB.
    amount: Mapped[float] = mapped_column(Float)
    date: Mapped[datetime.date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="account_operations")
    broker_account: Mapped[BrokerAccount] = relationship(back_populates="account_operations")

    def __repr__(self) -> str:
        return (
            f"AccountOperation(id={self.id!r}, user_id={self.user_id!r}, "
            f"broker_account_id={self.broker_account_id!r}, type={self.type!r}, "
            f"amount={self.amount!r}, date={self.date!r})"
        )
