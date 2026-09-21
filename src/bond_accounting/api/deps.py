"""FastAPI dependency wiring for the REST API.

The API module never constructs services itself. The application entry
point creates the services and passes them to :func:`build_api_dependencies`,
which returns ready-to-use FastAPI dependency providers bound to those
services (:class:`ApiDependencies`). Endpoints in
:mod:`bond_accounting.api.router` depend on the module-level provider
stubs below; the application installs the bound providers over the stubs
with ``app.dependency_overrides.update(deps.overrides())``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Service classes below stay as runtime imports on purpose: FastAPI resolves
# dependency annotations (including return types) with get_type_hints() when
# routes are registered, so TYPE_CHECKING-only imports would break the router.
from bond_accounting.analytics.service import AnalyticsService  # noqa: TC001
from bond_accounting.auth import AuthService, JwtError, JwtService
from bond_accounting.bonds.service import BondService  # noqa: TC001
from bond_accounting.portfolio.service import PortfolioService  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


def get_auth_service() -> AuthService:
    """Return the :class:`AuthService` bound by the application.

    Raises:
        RuntimeError: Always, unless the application installed the providers
            from :meth:`ApiDependencies.overrides` via
            ``app.dependency_overrides``.
    """
    raise RuntimeError(
        "AuthService is not configured; install ApiDependencies.overrides() "
        "on the application via app.dependency_overrides"
    )


def get_jwt_service() -> JwtService:
    """Return the :class:`JwtService` bound by the application.

    Raises:
        RuntimeError: Always, unless the application installed the providers
            from :meth:`ApiDependencies.overrides` via
            ``app.dependency_overrides``.
    """
    raise RuntimeError(
        "JwtService is not configured; install ApiDependencies.overrides() "
        "on the application via app.dependency_overrides"
    )


def get_bond_service() -> BondService:
    """Return the :class:`BondService` bound by the application.

    Raises:
        RuntimeError: Always, unless the application installed the providers
            from :meth:`ApiDependencies.overrides` via
            ``app.dependency_overrides``.
    """
    raise RuntimeError(
        "BondService is not configured; install ApiDependencies.overrides() "
        "on the application via app.dependency_overrides"
    )


def get_portfolio_service() -> PortfolioService:
    """Return the :class:`PortfolioService` bound by the application.

    Raises:
        RuntimeError: Always, unless the application installed the providers
            from :meth:`ApiDependencies.overrides` via
            ``app.dependency_overrides``.
    """
    raise RuntimeError(
        "PortfolioService is not configured; install ApiDependencies.overrides() "
        "on the application via app.dependency_overrides"
    )


def get_analytics_service() -> AnalyticsService:
    """Return the :class:`AnalyticsService` bound by the application.

    Raises:
        RuntimeError: Always, unless the application installed the providers
            from :meth:`ApiDependencies.overrides` via
            ``app.dependency_overrides``.
    """
    raise RuntimeError(
        "AnalyticsService is not configured; install ApiDependencies.overrides() "
        "on the application via app.dependency_overrides"
    )


def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    jwt_service: Annotated[JwtService, Depends(get_jwt_service)],
) -> int:
    """Resolve the authenticated caller from ``Authorization: Bearer <jwt>``.

    Args:
        credentials: Parsed bearer credentials (``None`` when the header is
            missing or uses another scheme).
        jwt_service: JWT verifier provided by the application.

    Returns:
        The authenticated user's id.

    Raises:
        HTTPException: 401 when the header is missing or the token fails
            verification.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt_service.verify_token(credentials.credentials)
    except JwtError as exc:
        logger.info("Rejected bearer token: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    return payload.user_id


@dataclass(frozen=True)
class ApiDependencies:
    """FastAPI dependency providers bound to concrete service instances.

    Attributes:
        get_auth_service: Provider returning the application's AuthService.
        get_jwt_service: Provider returning the application's JwtService.
        get_bond_service: Provider returning the application's BondService.
        get_portfolio_service: Provider returning the application's
            PortfolioService.
        get_analytics_service: Provider returning the application's
            AnalyticsService.
    """

    get_auth_service: Callable[[], AuthService]
    get_jwt_service: Callable[[], JwtService]
    get_bond_service: Callable[[], BondService]
    get_portfolio_service: Callable[[], PortfolioService]
    get_analytics_service: Callable[[], AnalyticsService]

    def overrides(self) -> dict[Callable[..., Any], Callable[..., Any]]:
        """Map the provider stubs to the bound providers.

        Returns:
            A dict suitable for ``app.dependency_overrides.update(...)``.
        """
        return {
            get_auth_service: self.get_auth_service,
            get_jwt_service: self.get_jwt_service,
            get_bond_service: self.get_bond_service,
            get_portfolio_service: self.get_portfolio_service,
            get_analytics_service: self.get_analytics_service,
        }


def build_api_dependencies(
    auth_service: AuthService,
    jwt_service: JwtService,
    bond_service: BondService,
    portfolio_service: PortfolioService,
    analytics_service: AnalyticsService,
) -> ApiDependencies:
    """Bind concrete services into ready FastAPI dependency providers.

    Args:
        auth_service: Authentication service (register/login).
        jwt_service: JWT creator/verifier used for bearer-token checks.
        bond_service: Bond CRUD service.
        portfolio_service: Transaction and position service.
        analytics_service: Portfolio analytics service.

    Returns:
        An :class:`ApiDependencies` whose callables are ready FastAPI
        dependencies; install them with
        ``app.dependency_overrides.update(deps.overrides())``.
    """

    def _auth_service() -> AuthService:
        return auth_service

    def _jwt_service() -> JwtService:
        return jwt_service

    def _bond_service() -> BondService:
        return bond_service

    def _portfolio_service() -> PortfolioService:
        return portfolio_service

    def _analytics_service() -> AnalyticsService:
        return analytics_service

    return ApiDependencies(
        get_auth_service=_auth_service,
        get_jwt_service=_jwt_service,
        get_bond_service=_bond_service,
        get_portfolio_service=_portfolio_service,
        get_analytics_service=_analytics_service,
    )
