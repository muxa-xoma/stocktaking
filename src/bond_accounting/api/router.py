"""REST API router for the bond accounting service.

Exposes 30 endpoints under the ``/api`` prefix. All services arrive
through FastAPI dependencies (see :mod:`bond_accounting.api.deps`); the
router never constructs services or the application itself — the app is
assembled in ``main.py`` which mounts :data:`api_router`.
"""

from __future__ import annotations

# Runtime import on purpose: pydantic resolves the postponed `datetime.date`
# annotations of the request models against the module namespace.
import datetime  # noqa: TC003
import logging
import uuid
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (  # noqa: TC002 (FastAPI resolves get_type_hints() at route-registration time)
    AsyncSession,
    async_sessionmaker,
)

from bond_accounting.account_operations.dto import (
    AccountOperationCreate,
    AccountOperationDTO,
    AccountOperationUpdate,
)
from bond_accounting.account_operations.exceptions import (
    AccountOperationForbiddenError,
)
from bond_accounting.account_operations.service import (  # noqa: TC001
    AccountOperationService,
)
from bond_accounting.analytics.dto import (
    PortfolioQueryParams,
    PortfolioSummary,
    PositionAnalytics,
)

# AnalyticsService stays a runtime import: FastAPI resolves endpoint/dependency
# annotations with get_type_hints() at route-registration time.
from bond_accounting.analytics.service import AnalyticsService  # noqa: TC001
from bond_accounting.api.deps import (
    get_account_operation_service,
    get_analytics_service,
    get_auth_service,
    get_bond_reference_service,
    get_bond_service,
    get_broker_service,
    get_current_user_id,
    get_portfolio_service,
    get_session_factory,
)
from bond_accounting.auth import (
    AuthError,
    AuthService,
    InvalidCredentialsError,
    UsernameTakenError,
)
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
from bond_accounting.brokers.exceptions import BrokerAccountHasOperationsError
from bond_accounting.brokers.service import (
    BrokerAccountHasTransactionsError,
    BrokerAccountNotFoundError,
    BrokerHasAccountsError,
    BrokerNameDuplicateError,
    BrokerNotFoundError,
    BrokerService,
)
from bond_accounting.db.models import BondCoupon
from bond_accounting.market_data import (
    BondReferenceService,
    NoBondsFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from bond_accounting.portfolio.dto import (
    PositionDTO,
    TransactionCreate,
    TransactionDTO,
    TransactionUpdate,
)
from bond_accounting.portfolio.service import (
    InsufficientPositionError,
    InvalidTransactionError,
    PortfolioService,
    TransactionForbiddenError,
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
        default=None,
        ge=0,
        description=(
            "Optional minimum commission (percent or fixed rubles depending on "
            "min_commission_type)."
        ),
    )
    min_commission_type: Literal["PERCENT", "RUBLES"] = Field(
        default="PERCENT",
        description="Interpretation of min_commission: percent or fixed rubles.",
    )
    description: str | None = None


class BrokerUpdateRequest(BaseModel):
    """Partial-update request body for ``PUT /api/brokers/{broker_id}``."""

    name: str | None = None
    commission: float | None = Field(
        default=None, ge=0, description="Commission percent (e.g. 5.0 = 5%)."
    )
    min_commission: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Minimum commission (percent or fixed rubles depending on min_commission_type)."
        ),
    )
    min_commission_type: Literal["PERCENT", "RUBLES"] | None = Field(
        default=None,
        description="Interpretation of min_commission: percent or fixed rubles.",
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


class BondReferenceResponse(BaseModel):
    """MOEX bond reference as returned by the reference endpoints.

    Serializable response shape mirroring the frozen ``BondReference``
    dataclass from :mod:`bond_accounting.market_data` (SC-3/SC-4); built
    from it with ``model_validate`` thanks to ``from_attributes``.
    """

    model_config = ConfigDict(from_attributes=True)

    isin: str = Field(description="ISIN of the bond.")
    name: str = Field(description="Short name of the bond.")
    nominal: int = Field(description="Face value in currency units.")
    coupon_rate: float = Field(description="Annual coupon rate in percent; 0 for zero-coupon.")
    coupon_period_days: int = Field(description="Days between coupons; 0 = zero-coupon bond.")
    maturity_date: datetime.date = Field(description="Maturity date.")
    issuer: str | None = Field(default=None, description="Optional issuer name.")


class BondReferenceSearchResponse(BaseModel):
    """Search-result envelope for ``GET /api/bonds/reference/search``."""

    results: list[BondReferenceResponse] = Field(description="Matching bond references.")


class BondCouponResponse(BaseModel):
    """One saved ``bond_coupons`` schedule row returned by the coupons endpoint.

    Read from the DB only (no MOEX round-trip); an empty saved schedule is a
    valid 200 with an empty list (DC-4b).
    """

    coupon_date: datetime.date = Field(description="Coupon payment date.")
    coupon_amount: float = Field(description="Coupon cash amount per payment.")


class SyncCouponsResponse(BaseModel):
    """Payload for ``POST /api/bonds/{isin}/sync-coupons``.

    Only the task-creation result is returned; no task status is persisted (user
    decision 2026-09-23, DC-4a).
    """

    task_id: str = Field(description="Fresh task id (uuid4().hex) of the spawned background job.")


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
    bond_reference_service: Annotated[BondReferenceService, Depends(get_bond_reference_service)],
) -> BondDTO:
    """Create a bond owned by the calling user.

    Any authenticated user may create a bond; only its owner may later
    update or delete it.

    After a successful create the bond's coupon schedule is auto-populated
    from MOEX fire-and-forget (DC-5). The trigger is wrapped in ``try/except``
    and the background job swallows all errors itself (DC-3), so a populate
    failure can never alter the 201 response.

    Raises:
        HTTPException: 409 when the ISIN already exists (mapped by
            :func:`register_exception_handlers`).
    """
    bond = await bond_service.create(data, user_id=user_id)
    try:
        bond_reference_service.schedule_populate_coupons(data.isin)
    except Exception:
        logger.warning("Coupon auto-populate failed for isin=%s", data.isin, exc_info=True)
    return bond


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
# bond reference (MOEX) endpoints (bearer auth)
# --------------------------------------------------------------------------- #


@api_router.get("/bonds/reference/search", response_model=BondReferenceSearchResponse)
async def search_bond_references(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_reference_service: Annotated[BondReferenceService, Depends(get_bond_reference_service)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> BondReferenceSearchResponse:
    """Search the MOEX bond reference registry.

    An empty result set is a regular 200. Provider outages and a disabled
    integration surface as 503 (mapped by :func:`register_exception_handlers`).
    """
    results = await bond_reference_service.search(q, limit=limit)
    return BondReferenceSearchResponse(
        results=[BondReferenceResponse.model_validate(ref) for ref in results]
    )


@api_router.get("/bonds/reference/{isin}", response_model=BondReferenceResponse)
async def get_bond_reference(
    isin: Annotated[str, Path(min_length=1, max_length=64)],
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_reference_service: Annotated[BondReferenceService, Depends(get_bond_reference_service)],
) -> BondReferenceResponse:
    """Fetch one MOEX bond reference by exact (case-insensitive) ISIN.

    Raises:
        HTTPException: 404 when the ISIN is unknown (mapped by
            :func:`register_exception_handlers`), 503 when the provider is
            unavailable or the integration is disabled.
    """
    reference = await bond_reference_service.get_by_isin(isin)
    return BondReferenceResponse.model_validate(reference)


@api_router.get("/bonds/{isin}/coupons", response_model=list[BondCouponResponse])
async def get_bond_coupons(
    isin: Annotated[str, Path(min_length=1, max_length=64)],
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_service: Annotated[BondService, Depends(get_bond_service)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> list[BondCouponResponse]:
    """Read the saved coupon schedule (``bond_coupons``) for a bond.

    Reads the DB only — no provider round-trip. An empty saved schedule is a
    valid 200 (``[]``), never an error.

    Raises:
        HTTPException: 404 when the ISIN is unknown (raises
            :class:`~bond_accounting.bonds.service.BondNotFoundError`, mapped by
            :func:`register_exception_handlers`).
    """
    bond = await bond_service.get_by_isin(isin)
    if bond is None:
        raise BondNotFoundError(f"Bond with ISIN {isin!r} not found")
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(BondCoupon)
                .where(BondCoupon.bond_id == bond.id)
                .order_by(BondCoupon.coupon_date, BondCoupon.id)
            )
        ).all()
    return [
        BondCouponResponse(coupon_date=row.coupon_date, coupon_amount=row.coupon_amount)
        for row in rows
    ]


@api_router.post(
    "/bonds/{isin}/sync-coupons",
    response_model=SyncCouponsResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_bond_coupons(
    isin: Annotated[str, Path(min_length=1, max_length=64)],
    user_id: Annotated[int, Depends(get_current_user_id)],
    bond_service: Annotated[BondService, Depends(get_bond_service)],
    bond_reference_service: Annotated[BondReferenceService, Depends(get_bond_reference_service)],
) -> SyncCouponsResponse:
    """Respawn the bond's coupon schedule from the MOEX bondization service.

    202 + ``task_id`` only — no task status is persisted (user decision
    2026-09-23, DC-4a). The bond's existence is checked synchronously first
    (404 unknown bond) and a disabled integration raises 503 before any job is
    spawned. The background job is fire-and-forget (DC-3): it opens its own DB
    session and swallows all errors, so nothing surfaces after the 202.

    Returns:
        The spawned job's fresh ``task_id`` (``uuid4().hex``).
    """
    bond = await bond_service.get_by_isin(isin)
    if bond is None:
        raise BondNotFoundError(f"Bond with ISIN {isin!r} not found")
    if not bond_reference_service.enabled:
        raise ProviderUnavailableError("market_data integration is disabled by config")
    bond_reference_service.schedule_populate_coupons(isin)
    return SyncCouponsResponse(task_id=uuid.uuid4().hex)


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


@api_router.get("/transactions/{transaction_id}", response_model=TransactionDTO)
async def get_transaction(
    transaction_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    portfolio_service: Annotated[PortfolioService, Depends(get_portfolio_service)],
) -> TransactionDTO:
    """Fetch one transaction.

    Raises:
        HTTPException: 404 when no transaction with this id exists; 403 when it
            is owned by another user (mapped by
            :func:`register_exception_handlers`).
    """
    transaction = await portfolio_service.get_transaction(transaction_id, user_id)
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found",
        )
    return transaction


@api_router.put("/transactions/{transaction_id}", response_model=TransactionDTO)
async def update_transaction(
    transaction_id: int,
    data: TransactionUpdate,
    user_id: Annotated[int, Depends(get_current_user_id)],
    portfolio_service: Annotated[PortfolioService, Depends(get_portfolio_service)],
) -> TransactionDTO:
    """Partially update a transaction (``None``/omitted fields unchanged).

    Raises:
        HTTPException: 404 when no transaction with this id exists; 403 when
            it is owned by another user; 422 when the update would drive a
            position below zero; 400 when it is rejected (e.g. unknown bond
            or broker account) — all mapped by
            :func:`register_exception_handlers`.
    """
    updated = await portfolio_service.update_transaction(transaction_id, data, user_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found",
        )
    return updated


@api_router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(
    transaction_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    portfolio_service: Annotated[PortfolioService, Depends(get_portfolio_service)],
) -> None:
    """Delete a transaction.

    Raises:
        HTTPException: 404 when no transaction with this id exists; 403 when
            it is owned by another user; 422 when deleting it would drive a
            position below zero (e.g. the bought bonds were already sold) —
            all mapped by :func:`register_exception_handlers`.
    """
    deleted = await portfolio_service.delete_transaction(transaction_id, user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found",
        )


# --------------------------------------------------------------------------- #
# account-operation endpoints (bearer auth)
# --------------------------------------------------------------------------- #


@api_router.get("/account-operations", response_model=list[AccountOperationDTO])
async def list_account_operations(
    user_id: Annotated[int, Depends(get_current_user_id)],
    account_operation_service: Annotated[
        AccountOperationService, Depends(get_account_operation_service)
    ],
    broker_account_id: int | None = None,
) -> list[AccountOperationDTO]:
    """List the caller's account operations, optionally filtered by broker account."""
    if broker_account_id is not None:
        return await account_operation_service.list_for_account(broker_account_id, user_id)
    return await account_operation_service.list_for_user(user_id)


@api_router.post(
    "/account-operations",
    status_code=status.HTTP_201_CREATED,
    response_model=AccountOperationDTO,
)
async def create_account_operation(
    data: AccountOperationCreate,
    user_id: Annotated[int, Depends(get_current_user_id)],
    account_operation_service: Annotated[
        AccountOperationService, Depends(get_account_operation_service)
    ],
) -> AccountOperationDTO:
    """Record an account operation (deposit/withdrawal/tax) for the caller.

    Raises:
        HTTPException: 404 when the referenced broker account does not exist
            or is owned by another user (mapped by
            :func:`register_exception_handlers`).
    """
    return await account_operation_service.create(data, user_id)


@api_router.get("/account-operations/{operation_id}", response_model=AccountOperationDTO)
async def get_account_operation(
    operation_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    account_operation_service: Annotated[
        AccountOperationService, Depends(get_account_operation_service)
    ],
) -> AccountOperationDTO:
    """Fetch one account operation.

    Raises:
        HTTPException: 404 when no operation with this id exists; 403 when it
            is owned by another user (mapped by
            :func:`register_exception_handlers`).
    """
    operation = await account_operation_service.get(operation_id, user_id)
    if operation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account operation {operation_id} not found",
        )
    return operation


@api_router.put("/account-operations/{operation_id}", response_model=AccountOperationDTO)
async def update_account_operation(
    operation_id: int,
    data: AccountOperationUpdate,
    user_id: Annotated[int, Depends(get_current_user_id)],
    account_operation_service: Annotated[
        AccountOperationService, Depends(get_account_operation_service)
    ],
) -> AccountOperationDTO:
    """Partially update an account operation (``None`` fields unchanged).

    Raises:
        HTTPException: 404 when no operation with this id exists; 403 when it
            is owned by another user (mapped by
            :func:`register_exception_handlers`).
    """
    updated = await account_operation_service.update(operation_id, data, user_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account operation {operation_id} not found",
        )
    return updated


@api_router.delete("/account-operations/{operation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_operation(
    operation_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    account_operation_service: Annotated[
        AccountOperationService, Depends(get_account_operation_service)
    ],
) -> None:
    """Delete an account operation.

    Raises:
        HTTPException: 404 when no operation with this id exists; 403 when it
            is owned by another user (mapped by
            :func:`register_exception_handlers`).
    """
    deleted = await account_operation_service.delete(operation_id, user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account operation {operation_id} not found",
        )


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
    app.add_exception_handler(NoBondsFoundError, _detail_response(status.HTTP_404_NOT_FOUND))
    app.add_exception_handler(
        ProviderUnavailableError, _detail_response(status.HTTP_503_SERVICE_UNAVAILABLE)
    )
    app.add_exception_handler(
        ProviderTimeoutError, _detail_response(status.HTTP_503_SERVICE_UNAVAILABLE)
    )
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
        BrokerAccountHasOperationsError, _detail_response(status.HTTP_409_CONFLICT)
    )
    app.add_exception_handler(
        AccountOperationForbiddenError, _detail_response(status.HTTP_403_FORBIDDEN)
    )
    app.add_exception_handler(
        InsufficientPositionError, _detail_response(status.HTTP_422_UNPROCESSABLE_CONTENT)
    )
    app.add_exception_handler(
        InvalidTransactionError, _detail_response(status.HTTP_400_BAD_REQUEST)
    )
    app.add_exception_handler(
        TransactionForbiddenError, _detail_response(status.HTTP_403_FORBIDDEN)
    )
    app.add_exception_handler(UsernameTakenError, _detail_response(status.HTTP_409_CONFLICT))
    app.add_exception_handler(
        InvalidCredentialsError, _detail_response(status.HTTP_401_UNAUTHORIZED)
    )
    app.add_exception_handler(AuthError, _detail_response(status.HTTP_400_BAD_REQUEST))
