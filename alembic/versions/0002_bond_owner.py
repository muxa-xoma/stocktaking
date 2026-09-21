"""add bonds.owner_id (NOT NULL, FK -> users.id, indexed)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21

Adds bond ownership: every bond belongs to exactly one user. Existing rows
are backfilled to the first registered user (lowest ``users.id``); the
assumption is that in any pre-ownership deployment all bonds were created
by the primary (first) user. If users exist, the backfill target is
``MIN(users.id)``; a deployment with bond rows but zero users is rejected
because no NOT NULL owner could be assigned.

The column is added as nullable first, backfilled, and then altered to
NOT NULL with the FK and index inside ``batch_alter_table`` — SQLite only
supports table-recreating batch mode for such changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bonds", sa.Column("owner_id", sa.Integer(), nullable=True))

    conn = op.get_bind()
    first_user_id = conn.execute(sa.text("SELECT MIN(id) FROM users")).scalar()
    stray_bonds = conn.execute(sa.text("SELECT COUNT(*) FROM bonds")).scalar()
    if first_user_id is None and stray_bonds:
        raise RuntimeError(
            "Cannot backfill bonds.owner_id: the table has rows but no users exist. "
            "Create a user first (the first user becomes the owner of existing bonds)."
        )
    conn.execute(sa.text("UPDATE bonds SET owner_id = :owner_id"), {"owner_id": first_user_id})

    with op.batch_alter_table("bonds") as batch:
        batch.alter_column("owner_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key("fk_bonds_owner_id", "users", ["owner_id"], ["id"])
        batch.create_index("ix_bonds_owner_id", ["owner_id"])


def downgrade() -> None:
    with op.batch_alter_table("bonds") as batch:
        batch.drop_index("ix_bonds_owner_id")
        batch.drop_constraint("fk_bonds_owner_id", type_="foreignkey")
        batch.drop_column("owner_id")
