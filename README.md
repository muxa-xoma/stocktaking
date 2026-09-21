# bond-accounting

Bond portfolio accounting service (Python 3.12+).

## What it is

A service for bond portfolio accounting: bond instruments and operations,
portfolio management, yield calculations, analytics, JWT-based auth, and a
web UI / HTTP API. This repository is at the skeleton stage — packages are
stubs, only the configuration system is functional.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
```

## Run

```bash
uv run bond-accounting
```

(Placeholder for now: prints a startup message and exits; full application
bootstrap is implemented in a later task.)

## Configuration

Configuration is assembled by
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) from
several sources. Precedence, highest first:

1. init kwargs (`Settings(auth={...})`),
2. `BOND_`-prefixed environment variables (`__` as the nested delimiter),
3. YAML file (`config.yaml` in the working directory by default, or the path
   passed to `load_settings(config_path)`),
4. model defaults.

The example `config.yaml` at the repository root shows the full structure:

```yaml
app:
  host: "127.0.0.1"
  port: 8080
  debug: false

database:
  driver: "sqlite"                # "sqlite" | "postgresql"
  sqlite_path: "data/bond_accounting.db"
  # connect_args override the default SQLite pragmas (user wins):
  # connect_args:
  #   journal_mode: "delete"
  # PostgreSQL:
  # driver: "postgresql"
  # host: "localhost"
  # port: 5432
  # user: "bond"
  # password: "secret"
  # dbname: "bond_accounting"
  # connect_args:
  #   sslmode: "disable"

auth:
  jwt_secret: "change-me-in-production"
  jwt_algorithm: "HS256"
  jwt_expires_minutes: 1440

logging:
  level: "INFO"
  format: "json"   # "json" | "text"

event_bus:
  max_queue_size: 10000
```

The database connection is configured structurally; the async SQLAlchemy
URL is built from these fields (special characters in the password are
URL-encoded automatically):

- SQLite: `sqlite+aiosqlite:///data/bond_accounting.db` (async driver only)
- PostgreSQL: `postgresql+psycopg://user:password@host:port/dbname?param=value`
  (psycopg 3 works with `create_async_engine`)

For SQLite a set of default pragmas is applied on every connection
(`auto_vacuum=1`, `journal_mode=wal`, `synchronous=1`, `temp_store=2`,
`cache_size=-64000`, `foreign_keys=1`). Entries under `database.connect_args`
override these defaults — the user wins, so e.g. setting
`connect_args: {journal_mode: delete}` replaces `wal` while the other pragmas
stay. For PostgreSQL, `connect_args` are rendered into the URL query string
(e.g. `sslmode`).

The engine is created with `create_engine_from_settings(db_config)` from
`bond_accounting.db.engine` (see [Database](#database)). For SQLite the pragmas
are applied on every new connection by a `connect` event listener that runs
`PRAGMA key = value` statements — not via engine `connect_args` (the aiosqlite
dialect forwards `connect_args` verbatim to `sqlite3.connect`, which does not
accept pragma keys).

Logging is configured with `setup_logging(settings.logging)` from
`bond_accounting.config.logging_setup`: `format: "json"` emits
machine-readable JSON logs (via `python-json-logger`'s msgspec backend) with
all standard fields plus any `extra=` kwargs; `format: "text"` emits
human-readable lines like
`2026-09-20 12:00:00 | INFO | bond_accounting.bonds | message | key=value`.

Environment variables use the `BOND_` prefix and `__` to separate nesting
levels:

```bash
BOND_AUTH__JWT_SECRET="super-secret"
BOND_APP__PORT="9000"
BOND_DATABASE__DRIVER="postgresql"
BOND_DATABASE__HOST="db.example.com"
BOND_DATABASE__SQLITE_PATH="/var/lib/bonds.db"
BOND_EVENT_BUS__MAX_QUEUE_SIZE="5000"
BOND_LOGGING__LEVEL="DEBUG"
BOND_LOGGING__FORMAT="text"
```

> Note: the old flat names `BOND_DATABASE_URL` and `BOND_AUTH_JWT_SECRET` are
> no longer supported; use the nested syntax shown above.

`auth.jwt_secret` is required: either set it in the config file or export
`BOND_AUTH__JWT_SECRET`. `load_settings()` raises `ConfigError` when the
configuration is invalid or the YAML file is malformed.

## Database

The database layer lives in `bond_accounting.db` (`src/bond_accounting/db/`):

- `Base`, `User`, `Bond`, `Transaction` — SQLAlchemy 2.0 models
  (`Mapped`/`mapped_column`), with CHECK constraints on `transactions.type`
  (`BUY`/`SELL`/`MATURE`) and `bonds.coupon_frequency`
  (`ANNUAL`/`SEMI_ANNUAL`/`QUARTERLY`).
- `create_engine_from_settings(db_config)` — async engine factory
  (`create_async_engine`); SQLite pragmas are applied via a `connect` event
  listener (see above).
- `create_session_factory(engine)` — `AsyncSession` factory with
  `expire_on_commit=False`.

The bond position is not stored: it is always derived as
`sum(BUY.quantity) - sum(SELL.quantity) - sum(MATURE.quantity)`.

Application code never creates tables (`Base.metadata.create_all` is not
used): the schema is managed exclusively by [Alembic](https://alembic.sqlalchemy.org/)
migrations in `alembic/versions/`. The migration URL is **not** stored in
`alembic.ini` — `alembic/env.py` builds the async engine from the application
configuration (`config.yaml` or `BOND_*` environment variables), so migrations
run against exactly the database the application uses:

```bash
uv run alembic revision --autogenerate -m "<description>"  # create a migration
uv run alembic upgrade head                               # apply all migrations
uv run alembic downgrade -1                               # roll back one revision
uv run alembic downgrade base                             # roll back everything
```

Example — migrate a specific SQLite file without editing the config (run from
the project root so `config.yaml` provides `auth.jwt_secret`, or export
`BOND_AUTH__JWT_SECRET`):

```bash
BOND_DATABASE__SQLITE_PATH=/tmp/new.db uv run alembic upgrade head
```

## Project layout

```
src/
  bond_accounting/
    main.py        # entry point (placeholder)
    config/       # Pydantic + YAML settings
    db/           # async SQLAlchemy models, engine/session factory
    event_bus/    # internal event bus (stub)
    auth/         # JWT auth (stub)
    bonds/        # bond instruments (stub)
    portfolio/    # portfolio management (stub)
    yield_calc/   # yield calculations (stub)
    analytics/    # analytics (stub)
    ui/           # web UI (stub)
    api/          # HTTP API (stub)
    external_bus/ # external bus (future)
alembic/          # Alembic migrations (async env.py)
alembic.ini       # Alembic configuration (no database URL)
tests/           # pytest tests
config.yaml     # example configuration
data/           # SQLite database files (runtime)
```

The project uses the standard uv `src/` layout: application code lives
under `src/bond_accounting/`.

## Development

```bash
uv sync --all-extras --group dev  # install runtime + dev dependencies
uv run pytest             # tests
uv run ruff check .       # lint
uv run mypy src           # type check
```

## Code Quality

The project uses four linters/type checkers, all run through `uv`. Ruff and
mypy are the enforced gates (CI must pass); `ty` and `pyrefly` are
additional type checkers whose findings are currently being triaged (see
open questions in the tooling task tracker) — they are not yet blocking.

| Tool | Purpose | Command |
| --- | --- | --- |
| [Ruff](https://docs.astral.sh/ruff/) | Linter + formatter | `uv run ruff check .` |
| | | `uv run ruff format --check .` |
| [mypy](https://mypy-lang.org/) | Type checker (gate) | `uv run mypy src` |
| [ty](https://docs.astral.sh/ty/) | Type checker (Astral) | `uv run ty check src tests` |
| [Pyrefly](https://pyrefly.org/) | Type checker (Meta) | `uv run pyrefly check` |

Configuration lives in `pyproject.toml`: `[tool.ruff]`, `[[tool.mypy.overrides]]`,
`[tool.ty]`, and `[tool.pyrefly]`. `ty` checks `src/` and `tests/` (config
`[tool.ty.src] include`); `pyrefly` runs in project mode picking up
`[tool.pyrefly]` (includes `src/**` and `tests/**`).

Run everything at once:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run ty check src tests
uv run pyrefly check
uv run pytest -q
```
