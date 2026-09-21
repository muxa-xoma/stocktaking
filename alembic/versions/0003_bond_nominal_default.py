"""bonds.nominal: add DB-level DEFAULT 1000

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21

Adds ``DEFAULT 1000`` to ``bonds.nominal`` (column stays NOT NULL), mirroring
the Python-side default that was already applied by the ORM. Existing rows are
untouched: a server default only fires for new rows where the column is
omitted from the INSERT. The ``BondCreate`` DTO defaults ``nominal`` to 1000
as well, so application inserts already behaved this way; the point of the
DB-level default is to keep rows consistent when ``bonds`` is written by
anything other than the application (raw SQL, manual fixes, future loaders).

SQLite cannot ALTER a column in place, so the change is made inside
``batch_alter_table`` (table recreate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("bonds") as batch:
        batch.alter_column(
            "nominal",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=sa.text("1000"),
        )


def downgrade() -> None:
    with op.batch_alter_table("bonds") as batch:
        batch.alter_column(
            "nominal",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=None,
        )
