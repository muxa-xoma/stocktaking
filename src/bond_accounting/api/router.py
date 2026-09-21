"""REST API router for the bond accounting service.

Exposes 22 endpoints under the ``/api`` prefix. All services arrive
through FastAPI dependencies (see :mod:`bond_accounting.api.deps`); the
router never constructs services or the application itself — the app is
assembled in ``main.py`` which mounts :data:`api_router`.
"""

from __future__ import annotations

# Runtime import on purpose: pydantic resolves the postponed `datetime.date`
# annotations of the request models against the module namespace.
import datetime  # noqa: TC003
import logging
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bond_accounting.analytics.dto import (
    PortfolioQueryParams,
    PortfolioSummary,
    PositionAnalytics,
)

# AnalyticsService stays a runtime import: FastAPI resolves endpoint/dependency
# annotations with get_type_hints() at route-registration time.
from bond_accounting.analytics.service import AnalyticsService  # noqa: TC001
from bond_accounting.api.deps import (
    get_analytics_service,
    get_auth_service,
    get_bond_service,
    get_broker_service,
    get_current_user_id,
    get_portfolio_service,
)
from bond_accounting.auth import AuthError, AuthService, InvalidCredentialsError, UsernameTakenError
from bond_accounting.bonds import BondDeletionBlockedError
from bond_accounting.bonds.dto import BondCreate, BondDTO, BondUpdate
from bond_accounting.bonds.service import (
    BondIsinDuplicateError,
    BondNotFoundError,
    BondNotOwnedError,
    BondService,
)
from bond_accounting.brokers.dto import (
    BrokerAccountCreate,
    BrokerAccountDTO,
    BrokerAccountUpdate,
    BrokerCreate,
    BrokerDTO,
    BrokerUpdate,
)
from bond_accounting.brokers.service import (
    BrokerAccountHasTransactionsError,
    BrokerAccountNotFoundError,
    BrokerHasAccountsError,
    BrokerNameDuplicateError,
    BrokerNotFoundError,
    BrokerService,
)
from bond_accounting.portfolio.dto import PositionDTO, TransactionCreate, TransactionDTO
from bond_accounting.portfolio.service import (
    InsufficientPositionError,
    InvalidTransactionError,
    PortfolioService,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# request / response models (auth endpoints use their own simple shapes)
# --------------------------------------------------------------------------- #


class RegisterRequest(BaseModel):
    """Registration request body.

    Password policy is enforced by the auth service (:class:`AuthError` →
    HTTP 400), not by this model.
    """

    username: str = Field(description="Unique username.")
    password: str = Field(description="Plaintext password (min length enforced by the service).")


class LoginRequest(BaseModel):
    """Login request body."""

    username: str = Field(description="Username to authenticate.")
    password: str = Field(description="Plaintext password to verify.")


class UserResponse(BaseModel):
    """Registered user as returned by ``POST /api/auth/register``."""

    id: int
    username: str


class TokenResponse(BaseModel):
    """JWT issued by ``POST /api/auth/login``."""

    token: str


class BrokerCreateRequest(BaseModel):
    """Creation request body for ``POST /api/brokers``."""

    name: str
    commission: float = Field(ge=0, default=0.0, description="Commission percent (e.g. 5.0 = 5%).")
    min_commission: float | None = Field(
        default=None, ge=0, description="Minimum commission percent."
    )
    description: str | None = None


class BrokerUpdateRequest(BaseModel):
    """Partial-update request body for ``PUT /api/brokers/{broker_id}``."""

    name: str | None = None
    commission: float | None = Field(
        default=None, ge=0, description="Commission percent (e.g. 5.0 = 5%)."
    )
    min_commission: float | None = Field(
        default=None, ge=0, description="Minimum commission percent."
    )
    description: str | None = None


class BrokerAccountCreateRequest(BaseModel):
    """Creation request body for ``POST /api/broker-accounts``."""

    broker_id: int
    name: str
    account_number: str | None = None
    account_type: Literal["STANDARD", "IIS", "LTD"]
    opened_at: datetime.date | None = None


class BrokerAccountUpdateRequest(BaseModel):
    """Partial-update request body for ``PUT /api/broker-accounts/{account_id}``."""

    name: str | None = None
    account_number: str | None = None
    account_type: Literal["STANDARD", "IIS", "LTD"] | None = None
    opened_at: datetime.date | None = None
    closed_at: datetime.date | None = None


# --------------------------------------------------------------------------- #
# auth endpoints (public)
# --------------------------------------------------------------------------- #


@api_router.post("/auth/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def register_user(
    request: RegisterRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserResponse:
    """Register a new user.

    Raises:
        HTTPException: 409 when the username is already taken, 400 on other
            auth errors (mapped by :func:`register_exception_handlers`).
    """
    user = await auth_service.register(request.username, request.password)
    return UserResponse(id=user.id, username=user.username)


@api_router.post("/auth/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    """Exchange credentials for a signed JWT.

    Raises:
        HTTPException: 401 on unknown username or wrong password (mapped by
            :func:`register_exception_handlers`).
    """
    token = await auth_service.login(request.username, request.password)
    return TokenResponse(token=token)


# --------------------------------------------------------------------------- #
# bond endpoints (bearer auth)
# --------------------------------------------------------------------------- #


@api_router.get("/bonds", response_model=list[BondDTO])
async def list_bonds(
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_service: Annotated[BondService, Depends(get_bond_service)],
) -> list[BondDTO]:
    """List all bonds.

    The registry is shared: every authenticated user sees every bond,
    regardless of ownership.
    """
    return await bond_service.list_all()


@api_router.post("/bonds", status_code=status.HTTP_201_CREATED, response_model=BondDTO)
async def create_bond(
    data: BondCreate,
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_service: Annotated[BondService, Depends(get_bond_service)],
) -> BondDTO:
    """Create a bond owned by the calling user.

    Any authenticated user may create a bond; only its owner may later
    update or delete it.

    Raises:
        HTTPException: 409 when the ISIN already exists (mapped by
            :func:`register_exception_handlers`).
    """
    return await bond_service.create(data, user_id=user_id)


@api_router.get("/bonds/{bond_id}", response_model=BondDTO)
async def get_bond(
    bond_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_service: Annotated[BondService, Depends(get_bond_service)],
) -> BondDTO:
    """Fetch one bond by id.

    The registry is shared: any authenticated user can read any bond.

    Raises:
        HTTPException: 404 when no bond with this id exists.
    """
    bond = await bond_service.get(bond_id)
    if bond is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Bond {bond_id} not found"
        )
    return bond


@api_router.put("/bonds/{bond_id}", response_model=BondDTO)
async def update_bond(
    bond_id: int,
    data: BondUpdate,
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_service: Annotated[BondService, Depends(get_bond_service)],
) -> BondDTO:
    """Partially update a bond (``None`` fields unchanged).

    Only the bond's owner may update it.

    Raises:
        HTTPException: 404 when no bond with this id exists, 403 when the
            bond is owned by another user (mapped by
            :func:`register_exception_handlers`).
    """
    bond = await bond_service.update(bond_id, data, user_id=user_id)
    if bond is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Bond {bond_id} not found"
        )
    return bond


@api_router.delete("/bonds/{bond_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bond(
    bond_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_service: Annotated[BondService, Depends(get_bond_service)],
) -> None:
    """Delete a bond.

    Only the bond's owner may delete it.

    Raises:
        HTTPException: 404 when no bond with this id exists, 403 when the
            bond is owned by another user (mapped by
            :func:`register_exception_handlers`).
    """
    deleted = await bond_service.delete(bond_id, user_id=user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Bond {bond_id} not found"
        )


# --------------------------------------------------------------------------- #
# broker endpoints (bearer auth)
# --------------------------------------------------------------------------- #


@api_router.get("/brokers", response_model=list[BrokerDTO])
async def list_brokers(
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> list[BrokerDTO]:
    """List all brokers (shared registry)."""
    return await broker_service.list_all_brokers()


@api_router.post("/brokers", status_code=status.HTTP_201_CREATED, response_model=BrokerDTO)
async def create_broker(
    data: BrokerCreateRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> BrokerDTO:
    """Create a new broker."""
    return await broker_service.create_broker(BrokerCreate(**data.model_dump()))


@api_router.get("/brokers/{broker_id}", response_model=BrokerDTO)
async def get_broker(
    broker_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> BrokerDTO:
    """Fetch one broker by id.

    Raises:
        HTTPException: 404 when no broker with this id exists.
    """
    broker = await broker_service.get_broker(broker_id)
    if broker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Broker {broker_id} not found"
        )
    return broker


@api_router.put("/brokers/{broker_id}", response_model=BrokerDTO)
async def update_broker(
    broker_id: int,
    data: BrokerUpdateRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> BrokerDTO:
    """Partially update a broker (``None`` fields unchanged).

    Raises:
        HTTPException: 404 when no broker with this id exists.
    """
    updated = await broker_service.update_broker(broker_id, BrokerUpdate(**data.model_dump()))
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Broker {broker_id} not found"
        )
    return updated


@api_router.delete("/brokers/{broker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broker(
    broker_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> None:
    """Delete a broker.

    Raises:
        HTTPException: 404 when no broker with this id exists, 409 when the
        broker still has accounts (mapped by
        :func:`register_exception_handlers`).
    """
    deleted = await broker_service.delete_broker(broker_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Broker {broker_id} not found"
        )


# --------------------------------------------------------------------------- #
# broker-account endpoints (bearer auth)
# --------------------------------------------------------------------------- #


@api_router.get("/broker-accounts", response_model=list[BrokerAccountDTO])
async def list_broker_accounts(
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> list[BrokerAccountDTO]:
    """List the caller's broker accounts."""
    return await broker_service.list_accounts_for_user(user_id)


@api_router.post(
    "/broker-accounts", status_code=status.HTTP_201_CREATED, response_model=BrokerAccountDTO
)
async def create_broker_account(
    data: BrokerAccountCreateRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> BrokerAccountDTO:
    """Create a broker account for the caller.

    Raises:
        HTTPException: 404 when the referenced broker does not exist (mapped
            by :func:`register_exception_handlers`).
    """
    return await broker_service.create_account(
        BrokerAccountCreate(**data.model_dump()), user_id=user_id
    )


@api_router.get("/broker-accounts/{account_id}", response_model=BrokerAccountDTO)
async def get_broker_account(
    account_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> BrokerAccountDTO:
    """Fetch one broker account.

    Raises:
        HTTPException: 404 when no account with this id exists or it is
            owned by another user.
    """
    account = await broker_service.get_account(account_id, user_id=user_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Broker account {account_id} not found",
        )
    return account


@api_router.put("/broker-accounts/{account_id}", response_model=BrokerAccountDTO)
async def update_broker_account(
    account_id: int,
    data: BrokerAccountUpdateRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> BrokerAccountDTO:
    """Partially update a broker account (``None`` fields unchanged).

    Raises:
        HTTPException: 404 when no account with this id exists or it is
            owned by another user.
    """
    updated = await broker_service.update_account(
        account_id, BrokerAccountUpdate(**data.model_dump()), user_id=user_id
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Broker account {account_id} not found",
        )
    return updated


@api_router.delete("/broker-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broker_account(
    account_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    broker_service: Annotated[BrokerService, Depends(get_broker_service)],
) -> None:
    """Delete a broker account.

    Raises:
        HTTPException: 404 when no account with this id exists or it is
            owned by another user; 409 when the account still has
            transactions (mapped by :func:`register_exception_handlers`).
    """
    deleted = await broker_service.delete_account(account_id, user_id=user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Broker account {account_id} not found",
        )


# --------------------------------------------------------------------------- #
# transaction endpoints (bearer auth)
# --------------------------------------------------------------------------- #


@api_router.get("/transactions", response_model=list[TransactionDTO])
async def list_transactions(
    user_id: Annotated[int, Depends(get_current_user_id)],
    portfolio_service: Annotated[PortfolioService, Depends(get_portfolio_service)],
    bond_id: int | None = None,
    broker_account_id: int | None = None,
) -> list[TransactionDTO]:
    """List the caller's transactions, optionally filtered by bond and broker account."""
    return await portfolio_service.list_transactions(
        user_id, bond_id=bond_id, broker_account_id=broker_account_id
    )


@api_router.post(
    "/transactions", status_code=status.HTTP_201_CREATED, response_model=TransactionDTO
)
async def create_transaction(
    data: TransactionCreate,
    user_id: Annotated[int, Depends(get_current_user_id)],
    portfolio_service: Annotated[PortfolioService, Depends(get_portfolio_service)],
) -> TransactionDTO:
    """Record a transaction for the caller.

    Raises:
        HTTPException: 422 when a SELL/MATURE would drive the position
            below zero, 400 when the transaction is rejected (e.g. unknown
            bond) — both mapped by :func:`register_exception_handlers`.
    """
    return await portfolio_service.add_transaction(user_id, data)


# --------------------------------------------------------------------------- #
# portfolio / analytics endpoints (bearer auth)
# --------------------------------------------------------------------------- #


@api_router.get("/portfolio", response_model=PortfolioSummary)
async def get_portfolio(
    user_id: Annotated[int, Depends(get_current_user_id)],
    analytics_service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    params: Annotated[PortfolioQueryParams, Query()],
) -> PortfolioSummary:
    """Full portfolio snapshot for the caller.

    Query parameters tune the ``next_coupons`` and ``upcoming_cashflows``
    sections: ``next_coupons_horizon_days`` (1..3650, default 730 — ~24
    months) and ``next_coupons_limit`` (1..1000, default 100), plus
    ``cashflows_horizon_days`` (1..7300, default 3650 — effectively "to
    maturity" for typical bonds) and ``cashflows_limit`` (1..5000,
    default 500). They only affect the horizon/size of those sections;
    every other field of the summary is unchanged. Out-of-range values
    are rejected with 422. Passing ``broker_account_id`` restricts the
    summary to transactions on that broker account.
    """
    return await analytics_service.get_portfolio_summary(
        user_id,
        horizon_days=params.next_coupons_horizon_days,
        limit=params.next_coupons_limit,
        cashflows_horizon_days=params.cashflows_horizon_days,
        cashflows_limit=params.cashflows_limit,
        broker_account_id=params.broker_account_id,
    )


@api_router.get("/portfolio/positions", response_model=list[PositionDTO])
async def get_portfolio_positions(
    user_id: Annotated[int, Depends(get_current_user_id)],
    portfolio_service: Annotated[PortfolioService, Depends(get_portfolio_service)],
    broker_account_id: int | None = None,
) -> list[PositionDTO]:
    """All open positions for the caller, optionally restricted to one broker account."""
    return await portfolio_service.get_all_positions(user_id, broker_account_id=broker_account_id)


@api_router.get("/bonds/{bond_id}/yield", response_model=PositionAnalytics)
async def get_bond_yield(
    bond_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    analytics_service: Annotated[AnalyticsService, Depends(get_analytics_service)],
) -> PositionAnalytics:
    """Yield analytics for the caller's position in one bond.

    Raises:
        HTTPException: 404 when there is no open position in this bond.
    """
    analytics = await analytics_service.get_position_analytics(user_id, bond_id)
    if analytics is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No open position in bond {bond_id}",
        )
    return analytics


# --------------------------------------------------------------------------- #
# domain exception -> HTTP status mapping
# --------------------------------------------------------------------------- #


def _detail_response(
    status_code: int,
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    """Build an exception handler rendering ``{"detail": str(exc)}``."""

    async def handler(request: Request, exc: Exception) -> JSONResponse:
        logger.info("Mapped %s to HTTP %d: %s", type(exc).__name__, status_code, exc)
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    return handler


def register_exception_handlers(app: FastAPI) -> None:
    """Register domain-error to HTTP-status mappings on the application.

    Must be called by the application entry point that mounts
    :data:`api_router`.

    Args:
        app: FastAPI application assembling the service.
    """
    app.add_exception_handler(BondNotFoundError, _detail_response(status.HTTP_404_NOT_FOUND))
    app.add_exception_handler(BondIsinDuplicateError, _detail_response(status.HTTP_409_CONFLICT))
    app.add_exception_handler(BondDeletionBlockedError, _detail_response(status.HTTP_409_CONFLICT))
    app.add_exception_handler(BondNotOwnedError, _detail_response(status.HTTP_403_FORBIDDEN))
    app.add_exception_handler(BrokerNotFoundError, _detail_response(status.HTTP_404_NOT_FOUND))
    app.add_exception_handler(
        BrokerAccountNotFoundError, _detail_response(status.HTTP_404_NOT_FOUND)
    )
    app.add_exception_handler(BrokerHasAccountsError, _detail_response(status.HTTP_409_CONFLICT))
    app.add_exception_handler(BrokerNameDuplicateError, _detail_response(status.HTTP_409_CONFLICT))
    app.add_exception_handler(
        BrokerAccountHasTransactionsError, _detail_response(status.HTTP_409_CONFLICT)
    )
    app.add_exception_handler(
        InsufficientPositionError, _detail_response(status.HTTP_422_UNPROCESSABLE_CONTENT)
    )
    app.add_exception_handler(
        InvalidTransactionError, _detail_response(status.HTTP_400_BAD_REQUEST)
    )
    app.add_exception_handler(UsernameTakenError, _detail_response(status.HTTP_409_CONFLICT))
    app.add_exception_handler(
        InvalidCredentialsError, _detail_response(status.HTTP_401_UNAUTHORIZED)
    )
    app.add_exception_handler(AuthError, _detail_response(status.HTTP_400_BAD_REQUEST))
