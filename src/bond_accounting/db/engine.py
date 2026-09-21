"""Async engine and session factories driven by :class:`DatabaseConfig`.

SQLite pragmas cannot be passed through ``create_async_engine(connect_args=...)``:
the aiosqlite dialect forwards ``connect_args`` verbatim to ``sqlite3.connect``,
which rejects pragma keys with a ``TypeError``. Instead, pragmas are applied
as ``PRAGMA key = value`` statements inside a ``connect`` event listener on
the engine's sync engine (the listener receives the DBAPI connection).

Because those statements are built by string interpolation, pragma keys and
values are validated against a strict whitelist at engine creation (see
``_ALLOWED_PRAGMA_STRING_VALUES`` and ``_validate_sqlite_pragmas``); raw
interpolation is only considered safe after that validation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bond_accounting.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

# Whitelist of string values allowed in SQLite pragma statements. SQLite
# pragma keywords are case-insensitive, so entries are lowercase and compared
# in lowercase. Covered pragmas include:
# - ``journal_mode``: delete, truncate, persist, memory, wal, off
# - ``synchronous``: off, normal, full, extra
# - ``temp_store``: default, file, memory
_ALLOWED_PRAGMA_STRING_VALUES = frozenset(
    {
        "delete",
        "truncate",
        "persist",
        "memory",
        "wal",
        "off",
        "normal",
        "full",
        "extra",
        "default",
        "file",
    }
)


def _mask_password(url: str) -> str:
    """Render a SQLAlchemy URL with the password replaced by ``***``."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable url>"


def _validate_sqlite_pragma_value(pragma: str, value: Any) -> None:
    """Validate a pragma value against the interpolation whitelist.

    Only plain integers and the keyword strings in
    ``_ALLOWED_PRAGMA_STRING_VALUES`` are accepted. Anything else raises a
    ``ValueError`` naming the pragma and the offending value, so a misconfigured
    pragma set fails loudly at engine creation instead of being interpolated
    verbatim into a ``PRAGMA key = value`` statement.
    """
    is_valid_int = isinstance(value, int) and not isinstance(value, bool)
    is_allowed_str = isinstance(value, str) and value.lower() in _ALLOWED_PRAGMA_STRING_VALUES
    if not (is_valid_int or is_allowed_str):
        raise ValueError(
            f"Invalid value for SQLite pragma {pragma!r}: {value!r}. "
            "Allowed values are integers or one of the known pragma "
            f"keywords: {sorted(_ALLOWED_PRAGMA_STRING_VALUES)}."
        )


def _validate_sqlite_pragmas(pragmas: Mapping[str, Any]) -> None:
    """Validate pragma keys and values before any SQL is built.

    Both the pragma name and its value are interpolated into
    ``PRAGMA key = value`` statements, so both must be checked: names must be
    valid identifiers, values must pass :func:`_validate_sqlite_pragma_value`.
    """
    for pragma, value in pragmas.items():
        if not isinstance(pragma, str) or not pragma.isidentifier():
            raise ValueError(
                f"Invalid SQLite pragma name: {pragma!r}. Pragma names must be valid identifiers."
            )
        _validate_sqlite_pragma_value(pragma, value)


def _render_pragma_value(value: Any) -> str:
    """Render a pragma value for a ``PRAGMA key = value`` statement."""
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)


def create_engine_from_settings(db_config: DatabaseConfig) -> AsyncEngine:
    """Create an async engine from :class:`DatabaseConfig`.

    SQLite: pragmas from ``db_config.resolved_connect_args`` are applied via a
    connect event listener (``PRAGMA`` statements) — NOT via ``connect_args``.
    Because ``PRAGMA key = value`` statements are built by string interpolation,
    pragma keys and values are validated against a strict whitelist at engine
    creation (see :func:`_validate_sqlite_pragmas`): keys must be identifiers,
    values must be integers or one of the known keyword strings in
    ``_ALLOWED_PRAGMA_STRING_VALUES``. Raw interpolation is only considered
    safe *after* this validation — any other value raises a ``ValueError``.
    PostgreSQL: ``resolved_connect_args`` is passed as ``connect_args``.

    The returned engine does not create any tables; schema management is
    Alembic's job.
    """
    if db_config.driver == "sqlite":
        connect_args: dict[str, Any] = {}
        pragmas = db_config.resolved_connect_args
        _validate_sqlite_pragmas(pragmas)
    else:
        connect_args = dict(db_config.resolved_connect_args)

    engine = create_async_engine(db_config.url, echo=False, connect_args=connect_args)

    if db_config.driver == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            """Apply configured pragmas on every new SQLite connection."""
            cursor = dbapi_connection.cursor()
            try:
                for pragma, value in pragmas.items():
                    rendered = _render_pragma_value(value)
                    cursor.execute(f"PRAGMA {pragma} = {rendered}")
            finally:
                cursor.close()
            logger.debug("Applied SQLite pragmas: %s", pragmas)

    logger.debug(
        "Created async engine (driver=%s, url=%s)",
        db_config.driver,
        _mask_password(db_config.url),
    )
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an :class:`~sqlalchemy.ext.asyncio.AsyncSession` factory.

    Sessions are configured with ``expire_on_commit=False`` so detached
    objects remain usable after a commit.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
