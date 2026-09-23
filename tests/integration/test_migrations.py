"""Migration tests for the squashed ``0001_initial_schema.py``.

Verifies the single-migration target schema (spec §6.5, adjusted for the
squash per SC-1/epic §Shared Contracts):

* ``upgrade head`` creates ``bonds`` directly with ``coupon_period_days``
  (DB-level server default 182) and **no** ``coupon_frequency`` column or
  ``ck_bonds_coupon_frequency`` CHECK constraint.
* ``bond_coupons`` exists with ``UNIQUE(bond_id, coupon_date)`` and a
  CASCADE FK to ``bonds``.
* ``downgrade base`` drops every table (empty schema).
* The insert path works: a ``Bond`` with ``coupon_period_days`` and
  ``BondCoupon`` rows can be persisted, and the unique constraint rejects
  duplicate ``(bond_id, coupon_date)`` pairs.

Tests run ``alembic`` against a temporary SQLite file (mirroring the
``test_db.py`` pattern); no ``Base.metadata.create_all`` anywhere — schema
ownership belongs to Alembic.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bond_accounting.config.settings import DatabaseConfig
from bond_accounting.db import (
    Bond,
    BondCoupon,
    User,
    create_engine_from_settings,
    create_session_factory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Tables the squashed migration must create.
EXPECTED_TABLES = {
    "users",
    "brokers",
    "bonds",
    "bond_coupons",
    "broker_accounts",
    "transactions",
    "account_operations",
}

ALL_TABLES = {
    "account_operations",
    "transactions",
    "broker_accounts",
    "bond_coupons",
    "bonds",
    "brokers",
    "users",
}


def _run_alembic(db_path: Path, *args: str) -> None:
    """Run ``uv run alembic <args>`` against a temp sqlite file, asserting success."""
    env = {
        **os.environ,
        "BOND_DATABASE__SQLITE_PATH": str(db_path),
        "BOND_AUTH__JWT_SECRET": "test-secret",
    }
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _sqlite_tables(db_path: Path) -> set[str]:
    """Return the set of table names in a sqlite database file."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _table_sql(db_path: Path, table: str) -> str:
    """Return the ``CREATE TABLE`` SQL for ``table`` as stored by sqlite."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"table {table!r} not found"
    return row[0]


def _table_info(db_path: Path, table: str) -> list[tuple[int, str, str, int, str | None, int]]:
    """Return ``PRAGMA table_info`` rows: (cid, name, type, notnull, dflt_value, pk)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return rows  # type: ignore[return-value]


def _table_columns(db_path: Path, table: str) -> set[str]:
    """Return the set of column names of ``table``."""
    return {row[1] for row in _table_info(db_path, table)}


@pytest.fixture(scope="module")
def migrated_schema(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A temp sqlite database at the squashed ``head`` revision."""
    path = tmp_path_factory.mktemp("migrations") / "schema.db"
    _run_alembic(path, "upgrade", "head")
    return path


# --------------------------------------------------------------------------- #
# Upgrade
# --------------------------------------------------------------------------- #


def test_upgrade_creates_all_tables(migrated_schema: Path) -> None:
    """``upgrade head`` materializes the complete target schema."""
    assert _sqlite_tables(migrated_schema) >= EXPECTED_TABLES


def test_bonds_has_coupon_period_days_and_no_coupon_frequency(migrated_schema: Path) -> None:
    """``bonds`` carries ``coupon_period_days`` but not ``coupon_frequency``.

    Assertions:
    * ``coupon_period_days`` column exists and is NOT NULL.
    * No ``coupon_frequency`` column.
    * No ``ck_bonds_coupon_frequency`` CHECK constraint in the DDL.
    """
    columns = _table_columns(migrated_schema, "bonds")
    assert "coupon_period_days" in columns
    assert "coupon_frequency" not in columns
    assert "ck_bonds_coupon_frequency" not in _table_sql(migrated_schema, "bonds")


def test_bonds_coupon_period_days_not_null_and_server_default(migrated_schema: Path) -> None:
    """``coupon_period_days`` is NOT NULL with a DB-level server default of 182."""
    info = _table_info(migrated_schema, "bonds")
    column = next(row for row in info if row[1] == "coupon_period_days")
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk).
    assert column[3] == 1, "coupon_period_days must be NOT NULL"
    assert column[4] in ("182", "'182'"), f"unexpected server default: {column[4]!r}"


def test_bond_coupons_table_constraints(migrated_schema: Path) -> None:
    """``bond_coupons`` has the expected schema and UNIQUE(bond_id, coupon_date)."""
    sql = _table_sql(migrated_schema, "bond_coupons")
    assert "bond_id" in _table_columns(migrated_schema, "bond_coupons")
    assert "coupon_date" in _table_columns(migrated_schema, "bond_coupons")
    assert "coupon_amount" in _table_columns(migrated_schema, "bond_coupons")
    assert "UNIQUE (bond_id, coupon_date)" in sql or "UNIQUE(bond_id, coupon_date)" in sql
    # CASCADE FK to bonds (sqlite renders it as ``REFERENCES bonds (id)``):
    assert "REFERENCES bonds" in sql
    assert "CASCADE" in sql


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #


def test_downgrade_drops_all_tables(tmp_path: Path) -> None:
    """``downgrade base`` leaves an empty schema (every table dropped)."""
    db_path = tmp_path / "down.db"
    _run_alembic(db_path, "upgrade", "head")
    _run_alembic(db_path, "downgrade", "base")
    assert _sqlite_tables(db_path) & ALL_TABLES == set()


# --------------------------------------------------------------------------- #
# Insert path
# --------------------------------------------------------------------------- #


async def test_insert_bond_with_coupon_period_days(migrated_schema: Path) -> None:
    """A ``Bond`` row can be inserted with ``coupon_period_days=182``."""
    config = DatabaseConfig(driver="sqlite", sqlite_path=str(migrated_schema))
    engine = create_engine_from_settings(config)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            async with session.begin():
                user = User(username="migration-user-1", password_hash="hash")
                session.add(user)
                await session.flush()
                bond = Bond(
                    isin="RU000A0JXTX8",
                    name="Migration bond",
                    coupon_rate=7.0,
                    coupon_period_days=182,
                    maturity_date=datetime.date(2030, 1, 1),
                    owner_id=user.id,
                )
                session.add(bond)
            bond_id = (
                await session.execute(select(Bond.id).where(Bond.isin == "RU000A0JXTX8"))
            ).scalar_one()
            assert bond_id is not None
    finally:
        await engine.dispose()


async def test_bond_coupon_unique_constraint_rejects_duplicates(migrated_schema: Path) -> None:
    """``UNIQUE(bond_id, coupon_date)`` rejects duplicate coupon rows."""
    config = DatabaseConfig(driver="sqlite", sqlite_path=str(migrated_schema))
    engine = create_engine_from_settings(config)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            user = User(username="migration-user-2", password_hash="hash")
            session.add(user)
            await session.flush()
            bond = Bond(
                isin="RU000A0JXUV9",
                name="Unique constraint bond",
                coupon_rate=6.0,
                coupon_period_days=182,
                maturity_date=datetime.date(2031, 1, 1),
                owner_id=user.id,
            )
            session.add(bond)
            await session.flush()
            session.add(
                BondCoupon(
                    bond_id=bond.id, coupon_date=datetime.date(2027, 1, 1), coupon_amount=30.0
                )
            )
            await session.flush()
            # The duplicate (same bond, same coupon_date) must violate UNIQUE.
            session.add(
                BondCoupon(
                    bond_id=bond.id, coupon_date=datetime.date(2027, 1, 1), coupon_amount=30.0
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()
