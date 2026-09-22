"""Minimal UI tests: import and page registration of ``create_ui_app``.

Only the composition contract is covered — ``create_ui_app`` registers all
NiceGUI pages without starting a browser or a server. E2E UI flows are out
of scope. The services are mocks: they are captured by the page closures
but never called during registration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nicegui import app as nicegui_app

from bond_accounting.ui import create_ui_app

#: Routes that ``create_ui_app`` must register on the NiceGUI application.
EXPECTED_ROUTES = {
    "/login",
    "/register",
    "/",
    "/bonds",
    "/brokers",
    "/accounts",
    "/transactions",
    "/operations",
    "/analytics",
}


def test_create_ui_app_registers_all_pages() -> None:
    """Calling ``create_ui_app`` with mock services registers every page route."""
    create_ui_app(
        auth_service=MagicMock(),
        jwt_service=MagicMock(),
        bond_service=MagicMock(),
        broker_service=MagicMock(),
        portfolio_service=MagicMock(),
        analytics_service=MagicMock(),
        account_operation_service=MagicMock(),
    )

    registered = {getattr(route, "path", None) for route in nicegui_app.routes}
    missing = EXPECTED_ROUTES - registered
    assert not missing, f"Routes not registered by create_ui_app: {sorted(missing)}"


def test_create_ui_app_returns_none() -> None:
    """The function is a side-effect-only composition helper."""
    result = create_ui_app(  # type: ignore[func-returns-value]  # contract: verifies the None return
        auth_service=MagicMock(),
        jwt_service=MagicMock(),
        bond_service=MagicMock(),
        broker_service=MagicMock(),
        portfolio_service=MagicMock(),
        analytics_service=MagicMock(),
        account_operation_service=MagicMock(),
    )
    assert result is None
