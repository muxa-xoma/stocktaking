"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from bond_accounting.analytics import AnalyticsService
from bond_accounting.api import api_router, build_api_dependencies, register_exception_handlers
from bond_accounting.auth import AuthService, JwtService, PasswordHasher
from bond_accounting.bonds.service import BondService
from bond_accounting.brokers.service import BrokerService
from bond_accounting.config.settings import ENV_PREFIX, AuthConfig, DatabaseConfig, EventBusConfig
from bond_accounting.db.engine import create_engine_from_settings, create_session_factory
from bond_accounting.event_bus import AsyncQueueEventBus
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
        ).overrides()
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """An httpx client talking to the app in-process, without a socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
