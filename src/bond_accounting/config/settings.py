"""Application settings built on ``pydantic-settings``: YAML + env overrides.

Configuration is assembled by ``pydantic-settings`` from several sources.
Priority (highest first, i.e. earlier sources win):

1. Init kwargs, e.g. ``Settings(auth={"jwt_secret": ...})``.
2. Environment variables: ``BOND_`` prefix + ``__`` nested delimiter.
3. YAML file: ``config.yaml`` in the working directory by default, or the
   path passed to :func:`load_settings`.
4. Dotenv file (only if ``env_file`` is configured in ``model_config``).
5. Model field defaults.

Environment variable naming (``__`` separates nesting levels):

    BOND_APP__HOST, BOND_APP__PORT, BOND_APP__DEBUG
    BOND_DATABASE__DRIVER, BOND_DATABASE__SQLITE_PATH,
    BOND_DATABASE__HOST, BOND_DATABASE__PORT, BOND_DATABASE__USER,
    BOND_DATABASE__PASSWORD, BOND_DATABASE__DBNAME
    BOND_AUTH__JWT_SECRET, BOND_AUTH__JWT_ALGORITHM, BOND_AUTH__JWT_EXPIRES_MINUTES
    BOND_EVENT_BUS__MAX_QUEUE_SIZE
    BOND_LOGGING__LEVEL, BOND_LOGGING__FORMAT

Breaking change (internal convention): the old flat names
``BOND_DATABASE_URL`` and ``BOND_AUTH_JWT_SECRET`` no longer work; use the
nested syntax above (e.g. ``BOND_AUTH__JWT_SECRET`` and, depending on the
driver, ``BOND_DATABASE__SQLITE_PATH`` or ``BOND_DATABASE__HOST``).

Error contract:
    * :func:`load_settings` raises :class:`ConfigError` when the resulting
      configuration is invalid (in particular when ``auth.jwt_secret`` is
      missing everywhere) or when the YAML file is unreadable/malformed.
    * Instantiating :class:`Settings` directly raises
      ``pydantic.ValidationError`` instead.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from sqlalchemy.engine import URL

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

logger = logging.getLogger(__name__)

#: Prefix for environment variables that override configuration values.
ENV_PREFIX = "BOND_"

#: Delimiter between nesting levels in ``BOND_``-prefixed env variables.
ENV_NESTED_DELIMITER = "__"

#: YAML file used when :func:`load_settings` is called without a path.
DEFAULT_CONFIG_PATH = "config.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded or is invalid."""


#: Default SQLite pragmas merged into :attr:`DatabaseConfig.resolved_connect_args`
#: for ``driver="sqlite"``; user ``connect_args`` entries with the same key
#: override these (user wins).
_SQLITE_PRAGMAS = {
    "auto_vacuum": 1,
    "journal_mode": "wal",
    "synchronous": 1,
    "temp_store": 2,
    "cache_size": -64000,
    "foreign_keys": 1,
}


class AppConfig(BaseModel):
    """HTTP application settings."""

    host: str = "127.0.0.1"
    port: int = 8080
    debug: bool = False


class DatabaseConfig(BaseModel):
    """Structured database connection settings for SQLite and PostgreSQL.

    Async drivers only: the SQLAlchemy URL is built from these fields via
    :attr:`url` (``sqlite+aiosqlite`` or ``postgresql+psycopg`` — psycopg 3
    works with ``create_async_engine``); passwords and query parameters are
    percent-encoded automatically.

    For ``driver="sqlite"`` only :attr:`sqlite_path` matters and
    :attr:`resolved_connect_args` merges :data:`_SQLITE_PRAGMAS` defaults with
    the user's :attr:`connect_args` (user wins). For ``driver="postgresql"``
    the host/port/user/password/dbname fields and :attr:`connect_args` are
    used, and :attr:`resolved_connect_args` is a pass-through.

    Consumers should build the engine as::

        create_async_engine(db_config.url, connect_args=db_config.resolved_connect_args)

    For SQLite the pragmas are passed via engine ``connect_args`` (not the
    URL query string); PostgreSQL ``connect_args`` are rendered into the URL
    query string.
    """

    driver: Literal["sqlite", "postgresql"] = "sqlite"
    #: SQLite database file path (relative or absolute); used when driver is "sqlite".
    sqlite_path: str = "data/bond_accounting.db"
    #: PostgreSQL connection settings; used when driver is "postgresql".
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = ""
    dbname: str = "bond_accounting"
    #: Connection parameters: pragmas to override for sqlite, URL query
    #: entries for postgresql.
    connect_args: dict[str, Any] = Field(default_factory=dict)

    @property
    def url(self) -> str:
        """Async SQLAlchemy URL built from the structured fields.

        Examples:
            ``sqlite+aiosqlite:///data/bond_accounting.db`` and
            ``postgresql+psycopg://user:password@host:port/dbname?param=value``.

        Empty ``user``/``password`` are omitted from the URL; special
        characters are percent-encoded by ``URL.create``.
        """
        if self.driver == "sqlite":
            return URL.create("sqlite+aiosqlite", database=self.sqlite_path).render_as_string()

        query = {key: str(value) for key, value in self.connect_args.items()}
        return URL.create(
            "postgresql+psycopg",
            username=self.user or None,
            password=self.password or None,
            host=self.host,
            port=self.port,
            database=self.dbname,
            query=query,
        ).render_as_string(hide_password=False)

    @property
    def resolved_connect_args(self) -> dict[str, Any]:
        """Connection arguments for ``create_async_engine(connect_args=...)``.

        For ``driver="sqlite"`` this is :data:`_SQLITE_PRAGMAS` merged with
        the user's :attr:`connect_args` — user entries win, so e.g.
        ``connect_args={"journal_mode": "delete"}`` overrides the default
        ``"wal"``. For ``driver="postgresql"`` the user's
        :attr:`connect_args` are returned unchanged (postgres parameters are
        already rendered into the URL query string).
        """
        if self.driver == "sqlite":
            return {**_SQLITE_PRAGMAS, **self.connect_args}
        return self.connect_args


class AuthConfig(BaseModel):
    """JWT authentication settings.

    ``jwt_secret`` has no default on purpose: it must be provided via the
    config file, init kwargs, or the ``BOND_AUTH__JWT_SECRET`` environment
    variable.
    """

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 1440


class EventBusConfig(BaseModel):
    """Internal event bus settings."""

    max_queue_size: int = 10000


class LoggingConfig(BaseModel):
    """Logging settings: root logger level and output format.

    ``level`` must be a valid ``logging`` level name (case-insensitive,
    normalized to upper case). ``format`` selects machine-readable JSON or
    human-readable text; see ``bond_accounting.config.logging_setup``.
    """

    level: str = "INFO"
    format: Literal["json", "text"] = "json"

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        """Normalize and validate the level name against ``logging``'s registry.

        ``logging.getLevelNamesMapping`` (public API since Python 3.11) maps
        level names to their numeric values; a name absent from it is unknown.
        """
        upper = value.upper()
        if upper not in logging.getLevelNamesMapping():
            raise ValueError(
                f"Unknown logging level {value!r}; expected one of "
                "DEBUG, INFO, WARNING, ERROR, CRITICAL (or NOTSET)"
            )
        return upper


class _YamlSettingsSource(YamlConfigSettingsSource):
    """YAML source that reports read/parse failures as :class:`ConfigError`."""

    def _read_file(self, file_path: Path | Traversable) -> dict[str, Any]:
        """Parse a YAML config file, wrapping errors into :class:`ConfigError`."""
        try:
            data = super()._read_file(file_path)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in config file {file_path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Cannot read config file {file_path}: {exc}") from exc
        if data is not None and not isinstance(data, dict):
            raise ConfigError(
                f"Config file {file_path} must contain a YAML mapping at the top level, "
                f"got {type(data).__name__}"
            )
        return data


#: Path of the YAML file for the current settings instantiation, if overridden
#: via :func:`load_settings`. When unset, the default from ``model_config``
#: (``config.yaml``) is used instead.
_yaml_config_path: ContextVar[Path | None] = ContextVar("bond_yaml_config_path", default=None)

#: Known-weak JWT secret values (lowercase; comparison is case-insensitive).
#: Detected by :func:`warn_if_weak_jwt_secret` at startup — extend freely.
WEAK_JWT_SECRETS: frozenset[str] = frozenset(
    {
        "",
        "test-secret",
        "secret",
        "changeme",
        "password",
        "123456",
    }
)


class Settings(BaseSettings):
    """Root application configuration.

    Loaded from init kwargs > ``BOND_``-prefixed env vars (``__`` nesting) >
    YAML file > dotenv (if configured) > defaults. See the module docstring
    for the environment variable naming convention.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        yaml_file=DEFAULT_CONFIG_PATH,
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    app: AppConfig = AppConfig()
    database: DatabaseConfig = DatabaseConfig()
    auth: AuthConfig
    event_bus: EventBusConfig = EventBusConfig()
    logging: LoggingConfig = LoggingConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Return the settings sources ordered by priority (earlier wins).

        Order: init kwargs > env vars > YAML file > dotenv > file secrets.
        A YAML path set by :func:`load_settings` overrides the default one
        from ``model_config``.
        """
        yaml_path = _yaml_config_path.get()
        if yaml_path is None:
            yaml_source: PydanticBaseSettingsSource = _YamlSettingsSource(settings_cls)
        else:
            yaml_source = _YamlSettingsSource(settings_cls, yaml_file=yaml_path)
        return (init_settings, env_settings, yaml_source, dotenv_settings, file_secret_settings)


def load_settings(config_path: str | None = None) -> Settings:
    """Load application settings.

    Resolution order (later wins):
        1. Model field defaults.
        2. YAML file at ``config_path`` (if the file exists); when
           ``config_path`` is ``None``, the default ``config.yaml`` in the
           working directory is used instead.
        3. ``BOND_``-prefixed environment variables (``__`` nesting).
        4. Init kwargs (none are passed here).

    Args:
        config_path: Optional path to a YAML config file. If the file does
            not exist, a warning is logged and defaults are used.

    Returns:
        Parsed :class:`Settings`.

    Raises:
        ConfigError: If the YAML file is unreadable/invalid, or if the
            resulting configuration is invalid — in particular, if
            ``auth.jwt_secret`` is missing (set it in the config file or
            via the ``BOND_AUTH__JWT_SECRET`` environment variable).
    """
    token = None
    if config_path is not None:
        path = Path(config_path)
        if path.is_file():
            logger.debug("Loading configuration from %s", path)
        else:
            logger.warning("Config file %s not found, using defaults", path)
        token = _yaml_config_path.set(path)

    try:
        # pydantic-settings fills the required ``auth`` field from env vars or
        # the YAML file, which mypy's (non-pydantic-aware) call check cannot see.
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing_secret = any(error.get("loc", ())[:1] == ("auth",) for error in exc.errors())
        hint = (
            " Set 'auth.jwt_secret' in the config file or export BOND_AUTH__JWT_SECRET."
            if missing_secret
            else ""
        )
        raise ConfigError(f"Invalid configuration: {exc}{hint}") from exc
    finally:
        if token is not None:
            _yaml_config_path.reset(token)


def warn_if_weak_jwt_secret(settings: Settings) -> None:
    """Log a CRITICAL warning when ``auth.jwt_secret`` is a known-weak value.

    Only active when ``app.debug`` is ``False`` (in debug mode local/dev
    secrets are expected, so the check is skipped entirely — no warning,
    not even at DEBUG level). Never raises and never blocks startup: it is
    a runtime diagnostic, not a validation error.
    """
    if settings.app.debug:
        return
    if settings.auth.jwt_secret.lower() in WEAK_JWT_SECRETS:
        logger.critical("JWT secret is a known weak value; change it before production use.")
