"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from bond_accounting.account_operations.service import AccountOperationService
from bond_accounting.analytics import AnalyticsService
from bond_accounting.api import api_router, build_api_dependencies, register_exception_handlers
from bond_accounting.auth import AuthService, JwtService, PasswordHasher
from bond_accounting.bonds.service import BondService
from bond_accounting.brokers.service import BrokerService
from bond_accounting.config.settings import ENV_PREFIX, AuthConfig, DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.event_bus import AsyncQueueEventBus
from bond_accounting.market_data import BondReferenceService, MoexClient
from bond_accounting.portfolio import PortfolioService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Project root (where ``alembic.ini`` lives).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: JWT-секрет, используемый только UI-тестами.
#: Фиктивный, но длинный (≥32 байта): короткие HMAC-ключи триггерят
#: ``InsecureKeyLengthWarning`` в PyJWT (RFC 7518 Section 3.2).
UI_JWT_SECRET = "ui-test-jwt-secret-0123456789abcdef0123456789abcdef"

#: JWT-секрет REST-стека фикстур (см. ``jwt_secret``).
REST_JWT_SECRET = "rest-test-jwt-secret-0123456789abcdef0123456789"


@pytest.fixture(autouse=True)
def _clean_bond_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient ``BOND_*`` env vars so tests do not depend on the host.

    Individual tests re-set the variables they need via ``monkeypatch``.
    """
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


@pytest.fixture
def auth_config() -> AuthConfig:
    """Auth-конфиг с тестовым секретом (без env-переменных)."""
    return AuthConfig(jwt_secret=UI_JWT_SECRET)


@pytest.fixture
def jwt_service(auth_config: AuthConfig) -> JwtService:
    """Реальный JwtService: страницы проверяют подпись настоящего токена."""
    return JwtService(auth_config)


# --------------------------------------------------------------------------- #
# MOEX bond-reference fixtures (in-memory ISS responder)
# --------------------------------------------------------------------------- #

#: Lowercase columns of the global ISS securities search response
#: (``GET /iss/securities.json``); see ``MoexClient._search_candidates``.
_MOEX_SEARCH_COLUMNS = ["secid", "isin", "shortname", "name", "group", "type", "emitent_title"]

#: UPPERCASE columns of the bond-detail response
#: (``GET /iss/engines/stock/markets/bonds/securities/{secid}.json``).
_MOEX_DETAIL_COLUMNS = [
    "SECID",
    "ISIN",
    "SHORTNAME",
    "SECNAME",
    "FACEVALUE",
    "COUPONPERCENT",
    "COUPONPERIOD",
    "MATDATE",
    "ISSUER",
]


class _MoexMock:
    """Configurable in-memory responder for the MOEX ISS endpoints.

    Default state: an empty bond registry and no errors, so the default
    ``bond_reference_service`` is harmless for tests that never touch the
    reference endpoints. Tests reconfigure the attributes before issuing
    requests: the same instance is closed over by the ``moex_client``
    transport, so changes take effect immediately.

    A "bond" is one dict carrying both search-level fields (lowercase keys)
    and bond-detail fields (UPPERCASE keys); see ``_MOEX_SEARCH_COLUMNS`` /
    ``_MOEX_DETAIL_COLUMNS``.
    """

    def __init__(self) -> None:
        #: Transport-level error to raise instead of answering; raised from
        #: ``httpx.MockTransport`` it maps to ProviderUnavailableError /
        #: ProviderTimeoutError inside ``MoexClient._get_json``.
        self.error: Exception | None = None
        #: Known bonds; returned by the global search regardless of ``q``
        #: (server-side filtering is MOEX's job, not the mock's).
        self.bonds: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        path = request.url.path
        if path == "/iss/securities.json":
            return self._securities_block(_MOEX_SEARCH_COLUMNS)
        if path.startswith("/iss/engines/stock/markets/bonds/securities/"):
            secid = path.removesuffix(".json").rsplit("/", maxsplit=1)[-1]
            rows = [
                [bond.get(column) for column in _MOEX_DETAIL_COLUMNS]
                for bond in self.bonds
                if bond.get("secid") == secid
            ]
            return httpx.Response(
                200, json={"securities": {"columns": _MOEX_DETAIL_COLUMNS, "data": rows}}
            )
        if path.startswith("/iss/securities/"):
            # Description fallback (matured bonds without an active board row);
            # not exercised by the current tests, answered with an empty block.
            return httpx.Response(
                200, json={"description": {"columns": ["name", "value"], "data": []}}
            )
        return httpx.Response(404, json={"error": {"code": f"unexpected path {path!r}"}})

    def _securities_block(self, columns: list[str]) -> httpx.Response:
        rows = [[bond.get(column) for column in columns] for bond in self.bonds]
        return httpx.Response(200, json={"securities": {"columns": columns, "data": rows}})


@pytest.fixture
def moex_mock_handler() -> _MoexMock:
    """MOEX ISS responder with an empty bond registry by default."""
    return _MoexMock()


@pytest.fixture
async def moex_client(moex_mock_handler: _MoexMock) -> AsyncGenerator[MoexClient]:
    """A real ``MoexClient`` whose HTTP transport is the in-memory mock.

    ``MoexClient`` owns its ``httpx.AsyncClient`` and offers no injection
    point, so the module-owned client is replaced with one whose transport
    routes to ``moex_mock_handler``; no socket is opened.
    """
    client = MoexClient(base_url="https://iss.moex.com", timeout_s=5.0)
    client._http = httpx.AsyncClient(  # test seam: no public injection point
        transport=httpx.MockTransport(moex_mock_handler), base_url="https://iss.moex.com"
    )
    yield client
    await client.aclose()


@pytest.fixture
def bond_reference_service(moex_client: MoexClient) -> BondReferenceService:
    """Bond-reference service over the mocked MOEX client (enabled)."""
    return BondReferenceService(client=moex_client)


# --------------------------------------------------------------------------- #
# REST API fixture stack (migrated temp DB + event bus + FastAPI app + client)
# --------------------------------------------------------------------------- #


@pytest.fixture
def jwt_secret() -> str:
    """JWT secret shared by the REST fixture stack; override to vary per module.

    Fake, but ≥32 bytes: shorter HMAC keys trigger PyJWT's
    ``InsecureKeyLengthWarning`` (RFC 7518 Section 3.2).
    """
    return REST_JWT_SECRET


@pytest.fixture
def db_filename() -> str:
    """SQLite filename inside ``tmp_path``; override to vary per module."""
    return "test.db"


@pytest.fixture
def migrated_db_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_filename: str,
    jwt_secret: str,
) -> str:
    """Create an empty migrated SQLite DB via alembic; return its file path."""
    db_path = tmp_path / db_filename
    monkeypatch.setenv("BOND_DATABASE__SQLITE_PATH", str(db_path))
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", jwt_secret)
    alembic_cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")
    return str(db_path)


@pytest.fixture
async def session_factory(
    migrated_db_url: str,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Async session factory bound to the migrated temp database."""
    engine = create_engine_from_settings(DatabaseConfig(sqlite_path=migrated_db_url))
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
async def event_bus() -> AsyncGenerator[AsyncQueueEventBus]:
    """A started event bus, stopped after the test."""
    bus = AsyncQueueEventBus(EventBusConfig(max_queue_size=100))
    await bus.start()
    yield bus
    await bus.stop()


@pytest.fixture
def app(
    session_factory: async_sessionmaker[AsyncSession],
    event_bus: AsyncQueueEventBus,
    jwt_secret: str,
    bond_reference_service: BondReferenceService,
) -> FastAPI:
    """The API application assembled like ``main.py``, with real services.

    A plain ``FastAPI()`` instance (instead of ``nicegui.app``): the router is
    included, domain exception handlers are registered, and real services are
    wired in through ``build_api_dependencies``.
    """
    jwt_service = JwtService(AuthConfig(jwt_secret=jwt_secret))
    auth_service = AuthService(session_factory, PasswordHasher(), jwt_service)
    bond_service = BondService(session_factory, event_bus)
    broker_service = BrokerService(session_factory, event_bus)
    portfolio_service = PortfolioService(session_factory, event_bus)
    analytics_service = AnalyticsService(session_factory, event_bus)
    account_operation_service = AccountOperationService(session_factory)

    application = FastAPI()
    application.include_router(api_router)
    register_exception_handlers(application)
    application.dependency_overrides.update(
        build_api_dependencies(
            auth_service,
            jwt_service,
            bond_service,
            broker_service,
            portfolio_service,
            analytics_service,
            account_operation_service,
            bond_reference_service=bond_reference_service,
        ).overrides()
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An httpx client talking to the app in-process, without a socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
