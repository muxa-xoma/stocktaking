"""Application entry point: composition root, startup and graceful shutdown.

This is the single place where all modules of the application are assembled:

1. Load settings (``--config`` YAML + ``BOND_*`` environment variables) and
   configure logging.
2. Create the async engine and session factory, then apply Alembic
   migrations (``upgrade head``).
3. Start the in-process event bus.
4. Build the services (auth, bonds, brokers, portfolio, analytics, account
   operations) and subscribe the analytics service to the bus.
5. Register the NiceGUI pages and mount the REST API.
6. Serve UI and REST on a single port; stop gracefully on SIGINT/SIGTERM.

Design decisions
----------------
* **NiceGUI + FastAPI on one port**: ``ui.run`` in NiceGUI 3.x always serves
  NiceGUI's own application object (``nicegui.app``, an instance of a
  ``FastAPI`` subclass) and has no parameter for an external app. The REST
  router, exception handlers and dependency overrides are therefore installed
  directly on ``nicegui.app`` rather than on a separately instantiated
  ``FastAPI()`` object: mounting a second app as a sub-application would
  route API errors around the registered exception handlers and complicate
  the dependency wiring. ``ui.run(fastapi_docs=True)`` enables ``/docs``,
  ``/redoc`` and ``/openapi.json``.
* **Migrations**: ``alembic upgrade head`` runs in-process via
  ``alembic.command.upgrade``. ``alembic/env.py`` resolves the database URL
  itself through :func:`~bond_accounting.config.settings.load_settings`;
  because its online mode calls ``asyncio.run``, the command is executed in
  a worker thread. A custom ``--config`` path is propagated to ``env.py``
  through the settings module's YAML-path context variable, which
  ``asyncio.to_thread`` copies into the worker thread.
* **Event-loop layout**: ``main()`` owns the main thread's event loop;
  ``ui.run`` (blocking) runs in a worker thread. SIGINT/SIGTERM handlers are
  installed on the main loop, request a NiceGUI shutdown, and the ``finally``
  block then stops the event bus and disposes of the engine.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from alembic import command
from alembic.config import Config
from nicegui import app as nicegui_app
from nicegui import ui

# Private on purpose: the only way to make alembic/env.py's load_settings()
# see the --config path without modifying env.py (see module docstring).
import bond_accounting.config.settings as settings_module
from bond_accounting.account_operations.service import AccountOperationService
from bond_accounting.analytics import AnalyticsService, attach_to_event_bus
from bond_accounting.api import api_router, build_api_dependencies, register_exception_handlers
from bond_accounting.auth import AuthService, JwtService, PasswordHasher
from bond_accounting.bonds.service import BondService
from bond_accounting.brokers.service import BrokerService
from bond_accounting.config.logging_setup import setup_logging
from bond_accounting.config.settings import load_settings, warn_if_weak_jwt_secret
from bond_accounting.db import create_engine_from_settings, create_session_factory
from bond_accounting.event_bus import AsyncQueueEventBus
from bond_accounting.portfolio.service import PortfolioService
from bond_accounting.ui import create_ui_app

__all__ = ["main", "run"]

logger = logging.getLogger(__name__)

#: How long to wait for the NiceGUI/uvicorn server thread to finish after a
# shutdown request before giving up on a graceful stop.
_SERVER_SHUTDOWN_TIMEOUT_S = 15.0

#: Level names accepted by uvicorn's ``LOG_LEVELS`` lookup (uvicorn/config.py);
# note that ``trace`` is uvicorn-only and ``NOTSET`` is Python-logging-only.
_UVICORN_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")


def _uvicorn_level(level: str) -> str:
    """Map a Python logging level name to a valid uvicorn ``log_level``.

    uvicorn looks up ``LOG_LEVELS[name.lower()]`` and raises ``KeyError`` for
    unknown names, and ``LoggingConfig.level`` may legitimately be ``NOTSET``
    (valid for Python logging, unknown to uvicorn), so fall back to ``info``
    with a warning instead of crashing server startup.
    """
    lowered = level.lower()
    if lowered in _UVICORN_LEVELS:
        return lowered
    logger.warning("logging.level %r is not a valid uvicorn level; falling back to 'info'", level)
    return "info"


def _find_alembic_dir() -> Path | None:
    """Locate the Alembic migrations directory.

    Checked in order: the current working directory (so the app can be
    started from the project root) and the source tree root relative to this
    file (``<project>/src/bond_accounting/main.py`` -> ``<project>/alembic``).
    """
    candidates = [
        Path.cwd() / "alembic",
        Path(__file__).resolve().parents[2] / "alembic",
    ]
    for candidate in candidates:
        if (candidate / "env.py").is_file():
            return candidate
    return None


def _upgrade_database_sync(alembic_dir: Path) -> None:
    """Run ``alembic upgrade head`` (executed in a worker thread)."""
    # An empty Config (no .ini file) on purpose: alembic/env.py builds the
    # database URL from the application settings, and skipping the ini file
    # also skips its fileConfig() call, which would clobber setup_logging().
    alembic_config = Config()
    alembic_config.set_main_option("script_location", str(alembic_dir))
    command.upgrade(alembic_config, "head")


async def _upgrade_database(config_path: str | None) -> None:
    """Apply pending migrations against the configured database."""
    alembic_dir = _find_alembic_dir()
    if alembic_dir is None:
        logger.warning(
            "Alembic migrations directory not found (looked in the working directory "
            "and next to the package); run 'alembic upgrade head' manually"
        )
        return

    # Propagate --config into alembic/env.py's own load_settings() call.
    token = settings_module._yaml_config_path.set(Path(config_path)) if config_path else None
    try:
        await asyncio.to_thread(_upgrade_database_sync, alembic_dir)
    finally:
        if token is not None:
            settings_module._yaml_config_path.reset(token)
    logger.info("Database migrations applied (alembic upgrade head)")


async def main(config_path: str | None = None) -> None:
    """Initialize every component and serve the application until shutdown.

    Args:
        config_path: Optional path to a YAML configuration file; ``BOND_*``
            environment variables always take precedence over it.
    """
    settings = load_settings(config_path)
    setup_logging(settings.logging)
    warn_if_weak_jwt_secret(settings)
    logger.info(
        "Starting bond-accounting (host=%s, port=%s, debug=%s)",
        settings.app.host,
        settings.app.port,
        settings.app.debug,
    )

    engine = create_engine_from_settings(settings.database)
    try:
        session_factory = create_session_factory(engine)
        await _upgrade_database(config_path)

        event_bus = AsyncQueueEventBus(settings.event_bus)
        await event_bus.start()
        try:
            password_hasher = PasswordHasher()
            jwt_service = JwtService(settings.auth)
            auth_service = AuthService(session_factory, password_hasher, jwt_service)
            bond_service = BondService(session_factory, event_bus)
            broker_service = BrokerService(session_factory, event_bus)
            portfolio_service = PortfolioService(session_factory, event_bus)
            analytics_service = AnalyticsService(session_factory, event_bus)
            account_operation_service = AccountOperationService(session_factory)
            attach_to_event_bus(analytics_service, event_bus)

            create_ui_app(
                auth_service,
                jwt_service,
                bond_service,
                broker_service,
                portfolio_service,
                analytics_service,
                account_operation_service,
            )

            api_dependencies = build_api_dependencies(
                auth_service,
                jwt_service,
                bond_service,
                broker_service,
                portfolio_service,
                analytics_service,
                account_operation_service=account_operation_service,
            )
            nicegui_app.include_router(api_router)
            register_exception_handlers(nicegui_app)
            nicegui_app.dependency_overrides.update(api_dependencies.overrides())

            stop_requested = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, stop_requested.set)
                except NotImplementedError:  # pragma: no cover - not on the main thread
                    logger.warning("Cannot install a handler for %s on this platform", signum)

            logger.info(
                "Serving UI and REST API on http://%s:%s (docs: /docs)",
                settings.app.host,
                settings.app.port,
            )
            server_task = asyncio.create_task(
                asyncio.to_thread(
                    ui.run,
                    host=settings.app.host,
                    port=settings.app.port,
                    title="Home Stocktaking",
                    fastapi_docs=True,
                    reload=False,
                    show=False,
                    # log_config=None is a stock uvicorn.Config parameter (not a
                    # NiceGUI API) forwarded via ui.run(**kwargs): uvicorn skips
                    # its own dictConfig, so uvicorn/nicegui records propagate
                    # into the root handler set up by setup_logging().
                    log_config=None,
                    uvicorn_logging_level=_uvicorn_level(settings.logging.level),
                    show_welcome_message=False,
                )
            )
            stop_task = asyncio.create_task(stop_requested.wait())
            done, _ = await asyncio.wait(
                {server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            stop_task.cancel()

            if stop_task in done and not server_task.done():
                logger.info("Shutdown signal received; stopping the server")
                try:
                    nicegui_app.shutdown()
                except Exception:
                    logger.exception("Failed to request a NiceGUI shutdown")
                try:
                    await asyncio.wait_for(server_task, timeout=_SERVER_SHUTDOWN_TIMEOUT_S)
                except TimeoutError:
                    logger.warning(
                        "Server did not stop within %.0f s; continuing shutdown",
                        _SERVER_SHUTDOWN_TIMEOUT_S,
                    )
            if server_task.done() and not server_task.cancelled():
                server_error = server_task.exception()
                if server_error is not None:
                    raise server_error
        finally:
            await event_bus.stop()
    finally:
        await engine.dispose()
        logger.info("bond-accounting stopped")


def run() -> None:
    """Console entry point (``bond-accounting``): parse arguments and start."""
    parser = argparse.ArgumentParser(
        prog="bond-accounting",
        description="Bond portfolio accounting service (NiceGUI UI + REST API).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a YAML configuration file (default: ./config.yaml plus BOND_* env vars)",
    )
    args = parser.parse_args()
    # The graceful path is handled inside main(); this only covers Ctrl+C
    # during startup, before the signal handlers are installed.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.config))


if __name__ == "__main__":
    run()
