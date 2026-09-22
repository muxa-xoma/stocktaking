"""Service-level tests for ``AccountOperationService`` (CRUD + ownership).

Fixture stack: the shared ``session_factory`` / ``event_bus`` fixtures from
``tests/conftest.py`` (migrated temp SQLite) plus the real
``AccountOperationService`` and ``BrokerService``. Users and broker accounts
are seeded the same way as in ``tests/integration/test_brokers.py``.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

import pytest

from bond_accounting.account_operations.dto import (
    AccountOperationCreate,
    AccountOperationUpdate,
)
from bond_accounting.account_operations.exceptions import AccountOperationForbiddenError
from bond_accounting.account_operations.service import AccountOperationService
from bond_accounting.brokers.exceptions import (
    BrokerAccountHasOperationsError,
    BrokerAccountNotFoundError,
)
from bond_accounting.brokers.service import (
    BrokerAccountCreate,
    BrokerCreate,
    BrokerService,
)
from bond_accounting.db.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.brokers.dto import BrokerAccountDTO
    from bond_accounting.event_bus import EventBus


def _create_dto(account_id: int, **overrides: Any) -> AccountOperationCreate:
    defaults: dict[str, Any] = {
        "broker_account_id": account_id,
        "type": "DEPOSIT",
        "amount": 100.0,
        "date": datetime.date(2026, 1, 15),
        "note": None,
    }
    return AccountOperationCreate(**{**defaults, **overrides})


async def _seed_user(session_factory: async_sessionmaker[AsyncSession], username: str) -> int:
    """Insert a user row and return its id."""
    async with session_factory() as session:
        user = User(username=username, password_hash="not-a-real-hash")
        session.add(user)
        await session.commit()
        return user.id


@pytest.fixture
def account_operation_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> AccountOperationService:
    """The service under test (real DB, no event bus by design)."""
    return AccountOperationService(session_factory)


@pytest.fixture
def broker_service(
    session_factory: async_sessionmaker[AsyncSession], event_bus: EventBus
) -> BrokerService:
    """The real broker service (for seeding accounts and the delete guard)."""
    return BrokerService(session_factory, event_bus)


@pytest.fixture
async def user_a(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """First test user (owner of the fixtures under test)."""
    return await _seed_user(session_factory, "alice")


@pytest.fixture
async def user_b(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Second test user (must not see or modify user A's operations)."""
    return await _seed_user(session_factory, "bob")


@pytest.fixture
async def account_a(broker_service: BrokerService, user_a: int) -> BrokerAccountDTO:
    """A broker account owned by user A."""
    broker = await broker_service.create_broker(
        BrokerCreate(name="Тестовый брокер", commission=0.3, min_commission=None)
    )
    return await broker_service.create_account(
        BrokerAccountCreate(broker_id=broker.id, name="Основной", account_type="STANDARD"),
        user_id=user_a,
    )


@pytest.fixture
async def account_b(broker_service: BrokerService, user_b: int) -> BrokerAccountDTO:
    """A broker account owned by user B."""
    broker = await broker_service.create_broker(
        BrokerCreate(name="Брокер Боба", commission=0.1, min_commission=None)
    )
    return await broker_service.create_account(
        BrokerAccountCreate(broker_id=broker.id, name="Счёт Боба", account_type="STANDARD"),
        user_id=user_b,
    )


# --------------------------------------------------------------------- #
# create


async def test_create_returns_dto_with_id(
    account_operation_service: AccountOperationService, account_a: BrokerAccountDTO, user_a: int
) -> None:
    """A created operation comes back as a DTO with the generated id."""
    dto = await account_operation_service.create(_create_dto(account_a.id), user_a)

    assert dto.id > 0
    assert dto.user_id == user_a
    assert dto.broker_account_id == account_a.id
    assert dto.type == "DEPOSIT"
    assert dto.amount == 100.0
    assert dto.date == datetime.date(2026, 1, 15)
    assert dto.note is None
    assert dto.created_at is not None


async def test_create_for_foreign_account_rejected(
    account_operation_service: AccountOperationService, account_b: BrokerAccountDTO, user_a: int
) -> None:
    """Creating an operation on someone else's account raises NotFound."""
    with pytest.raises(BrokerAccountNotFoundError):
        await account_operation_service.create(_create_dto(account_b.id), user_a)


async def test_create_for_nonexistent_account_rejected(
    account_operation_service: AccountOperationService, user_a: int
) -> None:
    """Creating an operation on a nonexistent account raises NotFound."""
    with pytest.raises(BrokerAccountNotFoundError):
        await account_operation_service.create(_create_dto(999999), user_a)


# --------------------------------------------------------------------- #
# list


async def test_list_for_user_returns_only_owned_operations(
    account_operation_service: AccountOperationService, account_a: BrokerAccountDTO, user_a: int
) -> None:
    """Only the caller's operations are listed, ordered by date then id."""
    await account_operation_service.create(
        _create_dto(account_a.id, date=datetime.date(2026, 2, 1)), user_a
    )
    await account_operation_service.create(
        _create_dto(account_a.id, date=datetime.date(2026, 1, 1)), user_a
    )

    listed = await account_operation_service.list_for_user(user_a)

    assert [op.date for op in listed] == [
        datetime.date(2026, 1, 1),
        datetime.date(2026, 2, 1),
    ]
    assert all(op.user_id == user_a for op in listed)


async def test_list_for_user_excludes_other_users(
    account_operation_service: AccountOperationService,
    account_a: BrokerAccountDTO,
    user_a: int,
    user_b: int,
) -> None:
    """User B's listing is empty when only user A has operations."""
    await account_operation_service.create(_create_dto(account_a.id), user_a)

    assert await account_operation_service.list_for_user(user_b) == []


async def test_list_for_account_filters_by_account(
    account_operation_service: AccountOperationService,
    account_a: BrokerAccountDTO,
    account_b: BrokerAccountDTO,
    user_a: int,
    user_b: int,
) -> None:
    """The per-account listing restricts to one user-broker-account pair."""
    await account_operation_service.create(_create_dto(account_a.id), user_a)
    await account_operation_service.create(
        _create_dto(account_a.id, type="TAX", amount=13.0), user_a
    )
    await account_operation_service.create(_create_dto(account_b.id), user_b)

    assert len(await account_operation_service.list_for_account(account_a.id, user_a)) == 2
    assert len(await account_operation_service.list_for_account(account_b.id, user_b)) == 1
    # User A sees nothing on user B's account and vice versa.
    assert await account_operation_service.list_for_account(account_b.id, user_a) == []
    assert await account_operation_service.list_for_account(account_a.id, user_b) == []


# --------------------------------------------------------------------- #
# get


async def test_get_returns_operation(
    account_operation_service: AccountOperationService, account_a: BrokerAccountDTO, user_a: int
) -> None:
    """An owned operation is fetched by id."""
    created = await account_operation_service.create(_create_dto(account_a.id), user_a)

    fetched = await account_operation_service.get(created.id, user_a)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.amount == 100.0


async def test_get_missing_returns_none(
    account_operation_service: AccountOperationService, user_a: int
) -> None:
    """A nonexistent operation yields None (the router maps it to 404)."""
    assert await account_operation_service.get(999999, user_a) is None


async def test_get_other_users_operation_forbidden(
    account_operation_service: AccountOperationService,
    account_a: BrokerAccountDTO,
    user_a: int,
    user_b: int,
) -> None:
    """User B cannot read user A's operation."""
    created = await account_operation_service.create(_create_dto(account_a.id), user_a)

    with pytest.raises(AccountOperationForbiddenError):
        await account_operation_service.get(created.id, user_b)


# --------------------------------------------------------------------- #
# update


async def test_update_changes_provided_fields_only(
    account_operation_service: AccountOperationService, account_a: BrokerAccountDTO, user_a: int
) -> None:
    """Omitted fields are untouched; provided ones change."""
    created = await account_operation_service.create(
        _create_dto(account_a.id, note="старая заметка"), user_a
    )

    updated = await account_operation_service.update(
        created.id, AccountOperationUpdate(amount=250.0, type="WITHDRAWAL"), user_a
    )

    assert updated is not None
    assert updated.amount == 250.0
    assert updated.type == "WITHDRAWAL"
    assert updated.date == created.date
    assert updated.note == "старая заметка"


async def test_update_missing_returns_none(
    account_operation_service: AccountOperationService, account_a: BrokerAccountDTO, user_a: int
) -> None:
    """Updating a nonexistent operation yields None (the router maps it to 404)."""
    updated = await account_operation_service.update(
        999999, AccountOperationUpdate(amount=1.0), user_a
    )
    assert updated is None


async def test_update_other_users_operation_forbidden(
    account_operation_service: AccountOperationService,
    account_a: BrokerAccountDTO,
    user_a: int,
    user_b: int,
) -> None:
    """User B cannot update user A's operation."""
    created = await account_operation_service.create(_create_dto(account_a.id), user_a)

    with pytest.raises(AccountOperationForbiddenError):
        await account_operation_service.update(
            created.id, AccountOperationUpdate(amount=1.0), user_b
        )


async def test_update_move_to_foreign_account_rejected(
    account_operation_service: AccountOperationService,
    account_a: BrokerAccountDTO,
    account_b: BrokerAccountDTO,
    user_a: int,
) -> None:
    """Moving an operation to another user's account is rejected."""
    created = await account_operation_service.create(_create_dto(account_a.id), user_a)

    with pytest.raises(BrokerAccountNotFoundError):
        await account_operation_service.update(
            created.id, AccountOperationUpdate(broker_account_id=account_b.id), user_a
        )
    # The failed move leaves the row untouched.
    fetched = await account_operation_service.get(created.id, user_a)
    assert fetched is not None
    assert fetched.broker_account_id == account_a.id


async def test_update_explicit_null_note_clears_column(
    account_operation_service: AccountOperationService, account_a: BrokerAccountDTO, user_a: int
) -> None:
    """``note`` is nullable: an explicit null clears it."""
    created = await account_operation_service.create(
        _create_dto(account_a.id, note="заметка"), user_a
    )

    updated = await account_operation_service.update(
        created.id, AccountOperationUpdate(note=None), user_a
    )
    assert updated is not None
    assert updated.note is None


# --------------------------------------------------------------------- #
# delete


async def test_delete_removes_operation(
    account_operation_service: AccountOperationService, account_a: BrokerAccountDTO, user_a: int
) -> None:
    """A deleted operation is gone afterwards."""
    created = await account_operation_service.create(_create_dto(account_a.id), user_a)

    assert await account_operation_service.delete(created.id, user_a) is True
    assert await account_operation_service.get(created.id, user_a) is None


async def test_delete_missing_returns_false(
    account_operation_service: AccountOperationService, user_a: int
) -> None:
    """Deleting a nonexistent operation returns False (the router maps it to 404)."""
    assert await account_operation_service.delete(999999, user_a) is False


async def test_delete_other_users_operation_forbidden(
    account_operation_service: AccountOperationService,
    account_a: BrokerAccountDTO,
    user_a: int,
    user_b: int,
) -> None:
    """User B cannot delete user A's operation."""
    created = await account_operation_service.create(_create_dto(account_a.id), user_a)

    with pytest.raises(AccountOperationForbiddenError):
        await account_operation_service.delete(created.id, user_b)
    # The operation survives the forbidden delete.
    assert await account_operation_service.get(created.id, user_a) is not None


# --------------------------------------------------------------------- #
# broker-account delete guard


async def test_account_with_operations_cannot_be_deleted(
    account_operation_service: AccountOperationService,
    broker_service: BrokerService,
    account_a: BrokerAccountDTO,
    user_a: int,
) -> None:
    """A broker account holding operations is protected from deletion."""
    await account_operation_service.create(_create_dto(account_a.id), user_a)

    with pytest.raises(BrokerAccountHasOperationsError):
        await broker_service.delete_account(account_a.id, user_id=user_a)


async def test_account_without_operations_can_be_deleted(
    account_operation_service: AccountOperationService,
    broker_service: BrokerService,
    account_a: BrokerAccountDTO,
    user_a: int,
) -> None:
    """Deleting all operations unblocks the account deletion."""
    created = await account_operation_service.create(_create_dto(account_a.id), user_a)
    await account_operation_service.delete(created.id, user_a)

    assert await broker_service.delete_account(account_a.id, user_id=user_a) is True
