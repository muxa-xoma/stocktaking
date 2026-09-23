# bond-accounting

**Available in:** [English](README.md) | [Русский](README_ru.md)

Bond portfolio accounting service (Python 3.14+).

## What it is

A service for bond portfolio accounting: bond instruments and operations,
portfolio management, yield calculations, analytics, JWT-based auth, and a
web UI / HTTP API. The application is served by a single entry point that
applies Alembic migrations, wires up the services and the internal event bus,
and serves the NiceGUI UI and the REST API on one port.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
```

## Run

```bash
uv run bond-accounting
```

The command loads the configuration (see below), applies pending Alembic
migrations, starts the in-process event bus, and serves the NiceGUI UI and the
REST API on one port. OpenAPI docs are available at `/docs` and `/redoc`.
Press Ctrl+C for a graceful shutdown. Pass `--config path/to/file.yaml` to use
a non-default configuration file.

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

market_data:
  enabled: true                                  # false disables the MOEX integration
  base_url: "https://iss.moex.com"               # MOEX ISS API base URL
  timeout_s: 5.0                                 # per-request HTTP timeout
  cache_ttl_s: 300                               # in-memory search cache TTL
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
Known third-party loggers (uvicorn server + access, nicegui, sqlalchemy,
alembic, fastapi) are forcibly reset so their records flow through the single
root handler in the configured format and level (including JSON access logs
with `client_addr` / `request_line` / `status_code` extras); PyJWT's
`InsecureKeyLengthWarning` is captured to the `bond_accounting.pyjwt_warnings`
logger while all other warnings keep their default behaviour.

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
BOND_MARKET_DATA__ENABLED="false"
```

> Note: the old flat names `BOND_DATABASE_URL` and `BOND_AUTH_JWT_SECRET` are
> no longer supported; use the nested syntax shown above.

`auth.jwt_secret` is required: either set it in the config file or export
`BOND_AUTH__JWT_SECRET`. `load_settings()` raises `ConfigError` when the
configuration is invalid or the YAML file is malformed.

## Bond reference search (MOEX)

The `market_data` module (`bond_accounting.market_data`) integrates with the
[MOEX ISS](https://www.moex.com/) bond reference API over `httpx` (a runtime
dependency) so bonds can be looked up by ISIN or name instead of being typed
manually:

- `GET /api/bonds/reference/search?q=<query>&limit=10` — search bond
  references by ISIN or name (`q` required, 1–100 chars; `limit` default 10,
  max 50); returns `200 {"results": [...]}` (possibly empty), `422` for an
  invalid query, or `503` when the provider is down or the integration is
  disabled.
- `GET /api/bonds/{isin}/coupons` — read the saved coupon schedule
  (`bond_coupons`) for a bond, ordered by date; returns `200`
  `[{"coupon_date": "<ISO date>", "coupon_amount": <float>}]` (an empty list
  is a valid result), `404` when the ISIN is unknown, or `422` for an invalid
  ISIN. Reads the database only — no provider round-trip.
- `POST /api/bonds/{isin}/sync-coupons` — respawn the bond's coupon schedule
  from the MOEX bondization service as a background job; returns
  `202 {"task_id": "<uuid4().hex>"}` (no task status is persisted), `404` for
  an unknown bond, `422` for an invalid ISIN, or `503` when the integration
  is disabled.

All endpoints are JWT-protected like all other endpoints. When the
integration is disabled (`market_data.enabled: false`) the REST endpoints
return `503` and the UI shows a static hint; when MOEX is unavailable the
UI shows a non-intrusive hint — manual bond entry keeps working in both
cases.

## Database

The database layer lives in `bond_accounting.db` (`src/bond_accounting/db/`):

- `Base`, `User`, `Bond`, `BondCoupon`, `Transaction`, `Broker`,
  `BrokerAccount`, `AccountOperation` — SQLAlchemy 2.0 models
  (`Mapped`/`mapped_column`), with CHECK constraints on `transactions.type`
  (`BUY`/`SELL`/`MATURE`), `account_operations.type`
  (`DEPOSIT`/`WITHDRAWAL`/`TAX`), and `bonds.coupon_period_days` (Integer,
  NOT NULL, default 182, `0` = zero-coupon bond; calendar days between
  coupon payments).
  `Broker.commission` is stored as **percent** (5.0 = 5%);
  `Broker.min_commission` is interpreted according to
  `Broker.min_commission_type` (`PERCENT` or `RUBLES`);
  `BrokerAccount.broker_id` is mandatory (NOT NULL).
- `BondCoupon` — one row per actual coupon payment of a bond in the
  `bond_coupons` table (`bond_id` FK → `bonds.id` ON DELETE CASCADE, indexed;
  `coupon_date`, `coupon_amount`, `UNIQUE(bond_id, coupon_date)`). The table
  is optional: when empty, yield calculations derive the coupon schedule
  from `Bond.coupon_period_days`; when populated (e.g. from MOEX
  `bondization`), the actual dates and amounts take priority.
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

The whole schema lives in a single squashed migration
(`alembic/versions/0001_initial_schema.py`) that creates the complete target
schema in one `upgrade()` and drops all tables in `downgrade()`. Local
databases created under the old migration chain must be recreated (the app
is pre-production; squashing was an explicit decision).

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
    main.py        # entry point: composition root, startup/graceful shutdown
    config/       # Pydantic + YAML settings, logging setup
    db/           # async SQLAlchemy models, engine/session factory
    event_bus/    # in-process async event bus
    auth/         # JWT auth: password hashing, token issue/verify, service
    bonds/        # bond instruments CRUD
    brokers/      # broker & broker-account CRUD
    market_data/  # MOEX ISS bond reference search (httpx, TTL cache)
    account_operations/ # account operations CRUD (DEPOSIT/WITHDRAWAL/TAX)
    portfolio/    # portfolio management, position netting
    yield_calc/   # yield calculations (accrued coupon, current yield, YTM)
    analytics/    # analytics (subscribed to the event bus)
    ui/           # NiceGUI web UI (drawer navigation, dark theme, modal CRUD
                  #  forms; auth, bonds, transactions, portfolio, analytics,
                  #  brokers, accounts, operations pages)
    api/          # REST API (FastAPI router, dependencies, error handling)
    external_bus/ # external bus (future)
alembic/          # Alembic migrations (async env.py)
alembic.ini       # Alembic configuration (no database URL)
tests/           # pytest test suite (layered, see Development)
config.yaml     # example configuration
data/           # SQLite database files (runtime)
```

The project uses the standard uv `src/` layout: application code lives
under `src/bond_accounting/`.

## Development

```bash
uv sync --all-extras --group dev                # install runtime + dev dependencies
uv run pytest -q                               # all tests (unit/integration/api/functional/ui)
uv run ruff check .                             # lint
uv run mypy src                                 # type check
```

The test suite is layered: `tests/{unit,integration,api,functional,ui}/`.
Shared fixtures live in `tests/conftest.py` (the fixture stack used by all
layers); `tests/rest_utils.py` provides shared REST constants and helpers.
A single `uv run pytest -q` invocation runs every layer.

## Test Coverage

Coverage is measured with [pytest-cov](https://pytest-cov.readthedocs.io/)
(included in the `dev` dependency group):

```bash
uv run pytest -q --cov=bond_accounting --cov-report=term-missing
```

This prints per-module line coverage with the uncovered line numbers. Add
`--cov-report=html` to get a browsable HTML report in `htmlcov/`. The current
line coverage of `bond_accounting/` is about 91% (557 tests, 90.60% line
coverage). The entry point `main.py` is not exercised by tests (it is
verified by actually running the application); without `main.py` the
coverage is correspondingly higher.

## Code Quality

The project uses four linters/type checkers, all run through `uv`. All four
are enforced gates — CI must pass each of them.

| Tool | Purpose | Command |
| --- | --- | --- |
| [Ruff](https://docs.astral.sh/ruff/) | Linter + formatter | `uv run ruff check .` |
| | | `uv run ruff format --check .` |
| [mypy](https://mypy-lang.org/) | Type checker (gate) | `uv run mypy src` |
| [ty](https://docs.astral.sh/ty/) | Type checker (Astral, gate) | `uv run ty check src tests` |
| [Pyrefly](https://pyrefly.org/) | Type checker (Meta, gate) | `uv run pyrefly check --min-severity warn` |

> **Note on the pyrefly "suppressed" counter:** the summary line
> `N suppressed` is not the number of actually hidden diagnostics —
> ignore directives may contribute phantom counts. To see what is really
> suppressed, run
> `uv run pyrefly check --enabled-ignores pyre --min-severity ignore --output-format json`.

Configuration lives in `pyproject.toml`: `[tool.ruff]`, `[[tool.mypy.overrides]]`,
`[tool.ty]`, and `[tool.pyrefly]`. `ty` checks `src/` and `tests/` (config
`[tool.ty.src] include`); `pyrefly` runs in project mode picking up
`[tool.pyrefly]` (includes `src/**` and `tests/**`). Note that `pyrefly`
hides warnings by default, hence `--min-severity warn` in the table above.

Run everything at once:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run ty check src tests
uv run pyrefly check --min-severity warn
uv run pytest -q --cov=bond_accounting
```
