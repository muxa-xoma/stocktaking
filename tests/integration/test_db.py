"""Tests for the database layer: models, engine/session factory, migrations.

The schema is never created with ``Base.metadata.create_all`` (that is
Alembic's job): tests that need tables run ``alembic upgrade head`` against a
temporary SQLite file first.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from bond_accounting.config.settings import DatabaseConfig
from bond_accounting.db import (
    Base,
    Bond,
    Transaction,
    User,
    create_engine_from_settings,
    create_session_factory,
)
from bond_accounting.db.models import Broker, BrokerAccount

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_COLUMNS = {
    "users": {"id", "username", "password_hash"},
    "bonds": {
        "id",
        "owner_id",
        "isin",
        "name",
        "nominal",
        "coupon_rate",
        "coupon_period_days",
        "maturity_date",
        "issuer",
    },
    "bond_coupons": {
        "id",
        "bond_id",
        "coupon_date",
        "coupon_amount",
    },
    "transactions": {
        "id",
        "user_id",
        "bond_id",
        "broker_account_id",
        "type",
        "quantity",
        "price",
        "date",
        "commission",
        "created_at",
    },
    "brokers": {
        "id",
        "name",
        "commission",
        "min_commission",
        "min_commission_type",
        "description",
        "created_at",
    },
    "broker_accounts": {
        "id",
        "user_id",
        "broker_id",
        "name",
        "account_number",
        "account_type",
        "opened_at",
        "closed_at",
        "created_at",
    },
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


@pytest.fixture(scope="module")
def migrated_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Path to a temp sqlite database with all migrations applied."""
    path = tmp_path_factory.mktemp("db") / "migrated.db"
    _run_alembic(path, "upgrade", "head")
    return path


def test_models_registered_on_metadata() -> None:
    """All model tables and columns are present on the shared metadata."""
    assert set(Base.metadata.tables) >= {
        "users",
        "bonds",
        "transactions",
        "brokers",
        "broker_accounts",
    }
    for table, columns in EXPECTED_COLUMNS.items():
        assert set(Base.metadata.tables[table].columns.keys()) == columns, table


def test_alembic_upgrade_and_downgrade(tmp_path: Path) -> None:
    """``alembic upgrade head`` creates the tables, ``downgrade base`` drops them."""
    db_path = tmp_path / "migrations.db"

    _run_alembic(db_path, "upgrade", "head")
    tables = _sqlite_tables(db_path)
    assert {"users", "bonds", "transactions", "brokers", "broker_accounts"} <= tables

    _run_alembic(db_path, "downgrade", "base")
    tables = _sqlite_tables(db_path)
    assert not ({"users", "bonds", "transactions", "brokers", "broker_accounts"} & tables)


async def test_sqlite_pragmas_applied_by_listener(tmp_path: Path) -> None:
    """The connect event listener applies the configured pragmas (e.g. WAL)."""
    config = DatabaseConfig(driver="sqlite", sqlite_path=str(tmp_path / "pragmas.db"))
    engine = create_engine_from_settings(config)
    try:
        async with engine.connect() as conn:
            journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        assert journal_mode == "wal"
        assert foreign_keys == 1
    finally:
        await engine.dispose()


async def test_insert_and_position_computation(migrated_db_path: Path) -> None:
    """Insert rows in one session, compute position in another.

    Position semantics: ``BUY - SELL - MATURE``; buy 10 and mature 4 → 6.
    """
    config = DatabaseConfig(driver="sqlite", sqlite_path=str(migrated_db_path))
    engine = create_engine_from_settings(config)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            async with session.begin():
                user = User(username="alice", password_hash="hash")
                session.add(user)
                await session.flush()
                bond = Bond(
                    isin="RU000A0JV4L2",
                    name="Sber OFLZ",
                    coupon_rate=7.0,
                    coupon_period_days=365,
                    maturity_date=datetime.date(2030, 1, 1),
                    owner_id=user.id,
                )
                broker = Broker(name="position-broker", commission=0.3)
                session.add(broker)
                await session.flush()
                account = BrokerAccount(
                    user_id=user.id, broker_id=broker.id, name="Основной", account_type="STANDARD"
                )
                session.add(account)
                await session.flush()
                session.add_all(
                    [
                        bond,
                        Transaction(
                            user=user,
                            bond=bond,
                            broker_account_id=account.id,
                            type="BUY",
                            quantity=10,
                            price=99.5,
                            date=datetime.date(2025, 1, 15),
                        ),
                        Transaction(
                            user=user,
                            bond=bond,
                            broker_account_id=account.id,
                            type="MATURE",
                            quantity=4,
                            price=100.0,
                            date=datetime.date(2025, 6, 20),
                        ),
                    ]
                )
            assert "alice" in repr(user)
            assert "RU000A0JV4L2" in repr(bond)

        async with session_factory() as session:
            bond = (await session.scalars(select(Bond).where(Bond.isin == "RU000A0JV4L2"))).one()
            rows = (
                await session.execute(
                    select(Transaction.type, func.sum(Transaction.quantity))
                    .where(Transaction.bond_id == bond.id)
                    .group_by(Transaction.type)
                )
            ).all()
            quantities = {row[0]: row[1] for row in rows}
            position = (
                quantities.get("BUY", 0) - quantities.get("SELL", 0) - quantities.get("MATURE", 0)
            )
            assert position == 6

            transactions = (
                await session.scalars(select(Transaction).where(Transaction.bond_id == bond.id))
            ).all()
            assert all(tx.created_at is not None for tx in transactions)
            assert all("Transaction" in repr(tx) for tx in transactions)
    finally:
        await engine.dispose()


async def test_transaction_type_check_constraint(migrated_db_path: Path) -> None:
    """Inserting a Transaction with an invalid type raises IntegrityError."""
    config = DatabaseConfig(driver="sqlite", sqlite_path=str(migrated_db_path))
    engine = create_engine_from_settings(config)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = User(username="bob", password_hash="hash")
            session.add(user)
            await session.flush()
            bond = Bond(
                isin="RU000A0JX0K8",
                name="Test bond",
                coupon_rate=5.0,
                coupon_period_days=182,
                maturity_date=datetime.date(2031, 1, 1),
                owner_id=user.id,
            )
            session.add(bond)
            await session.flush()
            broker = Broker(name="check-broker", commission=0.3)
            session.add(broker)
            await session.flush()
            account = BrokerAccount(
                user_id=user.id, broker_id=broker.id, name="Основной", account_type="STANDARD"
            )
            session.add(account)
            await session.flush()

            session.add(
                Transaction(
                    user_id=user.id,
                    bond_id=bond.id,
                    broker_account_id=account.id,
                    type="INVALID",
                    quantity=1,
                    price=100.0,
                    date=datetime.date(2025, 2, 1),
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


async def test_bond_coupon_period_days_not_null(migrated_db_path: Path) -> None:
    """A raw INSERT storing ``NULL`` for the NOT NULL ``coupon_period_days`` fails.

    ``coupon_period_days`` is NOT NULL with a server default of 182. The ORM
    model default would fill in ``182`` for an omitted value, so the NOT NULL
    constraint is exercised with a bare SQL INSERT that supplies an explicit
    ``NULL`` (bypassing the Python-side default); the DB rejects it with an
    IntegrityError.
    """
    config = DatabaseConfig(driver="sqlite", sqlite_path=str(migrated_db_path))
    engine = create_engine_from_settings(config)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = User(username="dave", password_hash="hash")
            session.add(user)
            await session.flush()

            async def _insert_null_period() -> None:
                await session.execute(
                    text(
                        "INSERT INTO bonds "
                        "(owner_id, isin, name, nominal, coupon_rate, "
                        " coupon_period_days, maturity_date) "
                        "VALUES (:owner_id, :isin, :name, 1000, 6.0, NULL, :maturity_date)"
                    ),
                    {
                        "owner_id": user.id,
                        "isin": "RU000A0JWXQ9",
                        "name": "Null period bond",
                        "maturity_date": "2032-01-01",
                    },
                )
                await session.commit()

            with pytest.raises(IntegrityError):
                await _insert_null_period()
            await session.rollback()
    finally:
        await engine.dispose()
