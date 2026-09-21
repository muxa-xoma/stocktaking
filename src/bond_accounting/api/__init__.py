"""Public REST API: router, dependency wiring, and auth request models."""

from __future__ import annotations

from bond_accounting.api.deps import ApiDependencies, build_api_dependencies
from bond_accounting.api.router import (
    LoginRequest,
    RegisterRequest,
    api_router,
    register_exception_handlers,
)

__all__ = [
    "ApiDependencies",
    "LoginRequest",
    "RegisterRequest",
    "api_router",
    "build_api_dependencies",
    "register_exception_handlers",
]
