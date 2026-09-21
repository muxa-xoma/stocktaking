"""Tests for the pydantic-settings based configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from bond_accounting.config.settings import (
    _SQLITE_PRAGMAS,
    ConfigError,
    DatabaseConfig,
    LoggingConfig,
    load_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

SQLITE_YAML = """\
app:
  host: "127.0.0.1"
  port: 8080
  debug: false

database:
  driver: "sqlite"
  sqlite_path: "data/bond_accounting.db"

auth:
  jwt_secret: "yaml-secret"
  jwt_algorithm: "HS256"
  jwt_expires_minutes: 1440

event_bus:
  max_queue_size: 10000
"""

POSTGRES_YAML = """\
auth:
  jwt_secret: "yaml-secret"

database:
  driver: "postgresql"
  host: "localhost"
  port: 5432
  user: "bond"
  password: "secret"
  dbname: "bond_accounting"
"""


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_load_settings_from_yaml_sqlite(tmp_path: Path) -> None:
    """A YAML file with the sqlite driver yields correct values and defaults."""
    config = _write_yaml(tmp_path / "config.yaml", SQLITE_YAML)
    settings = load_settings(str(config))

    assert settings.app.host == "127.0.0.1"
    assert settings.app.port == 8080
    assert settings.app.debug is False
    assert settings.database.driver == "sqlite"
    assert settings.database.url == "sqlite+aiosqlite:///data/bond_accounting.db"
    # PostgreSQL fields keep their class defaults even though the yaml only sets sqlite.
    assert settings.database.host == "localhost"
    assert settings.auth.jwt_secret == "yaml-secret"
    assert settings.auth.jwt_algorithm == "HS256"
    assert settings.auth.jwt_expires_minutes == 1440
    assert settings.event_bus.max_queue_size == 10000
    assert settings.logging.level == "INFO"
    assert settings.logging.format == "json"


def test_load_settings_from_yaml_postgresql(tmp_path: Path) -> None:
    """A YAML file with the postgresql driver builds the psycopg URL."""
    config = _write_yaml(tmp_path / "config.yaml", POSTGRES_YAML)
    settings = load_settings(str(config))

    assert settings.database.driver == "postgresql"
    assert (
        settings.database.url == "postgresql+psycopg://bond:secret@localhost:5432/bond_accounting"
    )


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested env vars (``BOND_`` prefix, ``__`` delimiter) beat the YAML file."""
    config = _write_yaml(tmp_path / "config.yaml", SQLITE_YAML)
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", "env-secret")
    monkeypatch.setenv("BOND_DATABASE__SQLITE_PATH", "other/bonds.db")
    monkeypatch.setenv("BOND_EVENT_BUS__MAX_QUEUE_SIZE", "5000")

    settings = load_settings(str(config))

    assert settings.auth.jwt_secret == "env-secret"
    assert settings.database.url == "sqlite+aiosqlite:///other/bonds.db"
    assert settings.event_bus.max_queue_size == 5000
    # Values not overridden still come from the YAML file.
    assert settings.app.port == 8080
    assert settings.database.driver == "sqlite"


def test_env_overrides_postgres_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single env override merges into the YAML's postgres section."""
    config = _write_yaml(tmp_path / "config.yaml", POSTGRES_YAML)
    monkeypatch.setenv("BOND_DATABASE__HOST", "db.example.com")

    settings = load_settings(str(config))

    assert settings.database.host == "db.example.com"
    assert (
        settings.database.url
        == "postgresql+psycopg://bond:secret@db.example.com:5432/bond_accounting"
    )


def test_env_only_no_yaml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing config file logs a warning and falls back to defaults + env."""
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", "env-secret")
    settings = load_settings(str(tmp_path / "does-not-exist.yaml"))

    assert settings.auth.jwt_secret == "env-secret"
    assert settings.app.host == "127.0.0.1"
    assert settings.database.driver == "sqlite"
    assert settings.database.url == "sqlite+aiosqlite:///data/bond_accounting.db"
    assert settings.event_bus.max_queue_size == 10000


def test_missing_jwt_secret_raises_config_error(tmp_path: Path) -> None:
    """No YAML file, no env secret -> ConfigError with an actionable hint."""
    with pytest.raises(ConfigError, match="BOND_AUTH__JWT_SECRET"):
        load_settings(str(tmp_path / "does-not-exist.yaml"))


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    """Malformed YAML is reported as ConfigError."""
    config = _write_yaml(tmp_path / "config.yaml", "auth: [unclosed")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_settings(str(config))


def test_non_mapping_yaml_raises_config_error(tmp_path: Path) -> None:
    """A YAML document whose top level is not a mapping is rejected."""
    config = _write_yaml(tmp_path / "config.yaml", "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_settings(str(config))


def test_url_encoding_special_password() -> None:
    """Special characters in the password are percent-encoded in the URL."""
    cfg = DatabaseConfig(
        driver="postgresql",
        host="localhost",
        port=5432,
        user="bond",
        password="p@ss:word/1",
        dbname="bond_accounting",
    )
    assert cfg.url == "postgresql+psycopg://bond:p%40ss%3Aword%2F1@localhost:5432/bond_accounting"


def test_connect_args_rendered_in_query_string() -> None:
    """``connect_args`` become URL query parameters."""
    cfg = DatabaseConfig(
        driver="postgresql",
        host="localhost",
        port=5432,
        user="bond",
        password="secret",
        dbname="bond_accounting",
        connect_args={"sslmode": "disable", "connect_timeout": 10},
    )
    assert make_url(cfg.url).query == {"sslmode": "disable", "connect_timeout": "10"}
    assert "sslmode=disable" in cfg.url


def test_empty_password_omitted_from_url() -> None:
    """An empty password renders without the ``:`` separator."""
    cfg = DatabaseConfig(driver="postgresql", user="postgres", password="", dbname="db")
    assert cfg.url == "postgresql+psycopg://postgres@localhost:5432/db"


def test_sqlite_absolute_path_url() -> None:
    """An absolute sqlite path renders with four slashes."""
    cfg = DatabaseConfig(driver="sqlite", sqlite_path="/var/lib/bonds.db")
    assert cfg.url == "sqlite+aiosqlite:////var/lib/bonds.db"


def test_sqlite_resolved_connect_args_defaults() -> None:
    """sqlite resolved_connect_args contains all default pragmas (as a copy)."""
    cfg = DatabaseConfig(driver="sqlite")
    resolved = cfg.resolved_connect_args

    assert resolved == _SQLITE_PRAGMAS
    assert resolved is not cfg.connect_args
    assert resolved["journal_mode"] == "wal"
    assert resolved["foreign_keys"] == 1


def test_sqlite_resolved_connect_args_user_wins() -> None:
    """User connect_args override the default SQLite pragmas."""
    cfg = DatabaseConfig(
        driver="sqlite",
        connect_args={"journal_mode": "delete", "synchronous": 0},
    )
    resolved = cfg.resolved_connect_args

    assert resolved["journal_mode"] == "delete"
    assert resolved["synchronous"] == 0
    # Non-overridden defaults survive the merge.
    assert resolved["foreign_keys"] == 1
    assert resolved["cache_size"] == -64000
    # The user's dict itself is not mutated.
    assert cfg.connect_args == {"journal_mode": "delete", "synchronous": 0}


def test_postgres_resolved_connect_args_passthrough() -> None:
    """postgres resolved_connect_args equals the user's connect_args only."""
    cfg = DatabaseConfig(driver="postgresql", connect_args={"sslmode": "disable"})

    assert cfg.resolved_connect_args == {"sslmode": "disable"}
    # No SQLite pragmas leak into the postgres driver.
    assert not (set(cfg.resolved_connect_args) & set(_SQLITE_PRAGMAS))


def test_logging_config_defaults() -> None:
    """LoggingConfig defaults: INFO level, json format."""
    cfg = LoggingConfig()

    assert cfg.level == "INFO"
    assert cfg.format == "json"


def test_logging_config_rejects_invalid_level() -> None:
    """An unknown level name fails validation."""
    with pytest.raises(ValidationError, match="level"):
        LoggingConfig(level="NOTALEVEL")


def test_logging_config_normalizes_level_case() -> None:
    """The level is normalized to upper case."""
    assert LoggingConfig(level="debug").level == "DEBUG"


def test_logging_config_rejects_invalid_format() -> None:
    """Only "json" and "text" are valid formats."""
    with pytest.raises(ValidationError, match="format"):
        LoggingConfig(format="xml")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # pyrefly: ignore[bad-argument-type]  # invalid on purpose: negative test of pydantic validation
