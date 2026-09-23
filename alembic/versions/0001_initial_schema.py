"""initial schema: users, brokers, bonds, bond_coupons, broker_accounts,
transactions, account_operations

Revision ID: 0001
Create Date: 2026-09-23

Single squashed migration (replaces the former 0001–0002 chain; user
decision 2026-09-22) that creates the complete target schema in one go:
``users``, ``brokers``, ``bonds`` (with ``coupon_period_days`` and no
``coupon_frequency``), ``bond_coupons``, ``broker_accounts``,
``transactions`` and ``account_operations`` with their uniqueness, foreign
key and CHECK constraints. Works on both SQLite and PostgreSQL: plain
``create_table`` ops only — no ``batch_alter_table`` is needed since this
is a fresh single migration.

Notes on column semantics:

* ``bonds.owner_id`` — NOT NULL FK → ``users.id`` (indexed); every bond
  belongs to exactly one user.
* ``bonds.nominal`` — NOT NULL with DB-level ``DEFAULT 1000`` so rows
  inserted outside the ORM (raw SQL, other clients) also get 1000 when
  the column is omitted, matching ``BondCreate.nominal``.
* ``bonds.coupon_period_days`` — NOT NULL with DB-level ``DEFAULT 182``;
  calendar days between coupon payments (MOEX ``couponperiod`` stored
  as-is); ``0`` means a zero-coupon bond.
* ``bond_coupons`` — optional actual coupon schedule; ``UNIQUE(bond_id,
  coupon_date)``; rows are deleted together with the bond (FK ON DELETE
  CASCADE); ``bond_id`` is indexed.
* ``broker_accounts.account_type`` — restricted to ``STANDARD`` / ``IIS`` /
  ``LTD`` by a CHECK constraint.
* ``transactions.broker_account_id`` — NOT NULL FK → ``broker_accounts.id``
  (indexed); every transaction is executed on a broker account.
* ``brokers.min_commission_type`` — NOT NULL with DB-level ``DEFAULT
  'PERCENT'`` so rows inserted outside the ORM also get 'PERCENT' when the
  column is omitted (same pattern as ``bonds.nominal``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "brokers",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("min_commission", sa.Float(), nullable=True),
        sa.Column(
            "min_commission_type",
            sa.String(length=10),
            server_default="PERCENT",
            nullable=False,
        ),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.CheckConstraint(
            "min_commission_type IN ('PERCENT', 'RUBLES')",
            name="ck_brokers_min_commission_type",
        ),
    )
    op.create_table(
        "bonds",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("nominal", sa.Integer(), server_default="1000", nullable=False),
        sa.Column("coupon_rate", sa.Float(), nullable=False),
        sa.Column("coupon_period_days", sa.Integer(), server_default="182", nullable=False),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.UniqueConstraint("isin"),
    )
    op.create_index("ix_bonds_owner_id", "bonds", ["owner_id"])
    op.create_table(
        "bond_coupons",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("bond_id", sa.Integer(), nullable=False),
        sa.Column("coupon_date", sa.Date(), nullable=False),
        sa.Column("coupon_amount", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["bond_id"], ["bonds.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("bond_id", "coupon_date"),
    )
    op.create_index("ix_bond_coupons_bond_id", "bond_coupons", ["bond_id"])
    op.create_table(
        "broker_accounts",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("broker_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("account_number", sa.String(length=100), nullable=True),
        sa.Column("account_type", sa.String(length=20), nullable=False),
        sa.Column("opened_at", sa.Date(), nullable=True),
        sa.Column("closed_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["broker_id"], ["brokers.id"]),
        sa.CheckConstraint(
            "account_type IN ('STANDARD', 'IIS', 'LTD')",
            name="ck_broker_accounts_type",
        ),
    )
    op.create_index("ix_broker_accounts_user_id", "broker_accounts", ["user_id"])
    op.create_index("ix_broker_accounts_broker_id", "broker_accounts", ["broker_id"])
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("bond_id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["bond_id"], ["bonds.id"]),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("type IN ('BUY', 'SELL', 'MATURE')", name="ck_transactions_type"),
    )
    op.create_index("ix_transactions_broker_account_id", "transactions", ["broker_account_id"])
    op.create_table(
        "account_operations",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "type IN ('DEPOSIT', 'WITHDRAWAL', 'TAX')",
            name="ck_account_operations_type",
        ),
    )
    op.create_index("ix_account_operations_user_id", "account_operations", ["user_id"])
    op.create_index(
        "ix_account_operations_broker_account_id",
        "account_operations",
        ["broker_account_id"],
    )


def downgrade() -> None:
    op.drop_table("account_operations")
    op.drop_table("transactions")
    op.drop_table("broker_accounts")
    op.drop_table("bond_coupons")
    op.drop_table("bonds")
    op.drop_table("brokers")
    op.drop_table("users")
