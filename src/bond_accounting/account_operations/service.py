"""CRUD service for account operations (non-trading money movements).

Ordering guarantee: every mutation (create/update/delete) flushes its DB
writes and only then commits. Unlike the other services this one does not
publish events on the bus (deliberately out of scope — no analytics
consumes account operations yet), so a mutation is simply flush → commit.

Domain rules:

* An account operation belongs to a single user (``user_id``); only the
  owner may read, update or delete it.
* An operation lives on a broker account owned by the same user; creating
  an operation for — or moving it to — someone else's account is rejected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from bond_accounting.account_operations.dto import (
    AccountOperationCreate,
    AccountOperationDTO,
    AccountOperationUpdate,
)
from bond_accounting.account_operations.exceptions import AccountOperationForbiddenError
from bond_accounting.brokers.exceptions import BrokerAccountNotFoundError
from bond_accounting.db.models import AccountOperation, BrokerAccount

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class AccountOperationService:
    """CRUD service for account operations."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Initialize the service.

        Args:
            session_factory: Factory of ``AsyncSession`` objects; each service
                method opens its own short-lived session from it.
        """
        self._session_factory = session_factory

    async def create(self, data: AccountOperationCreate, user_id: int) -> AccountOperationDTO:
        """Insert a new account operation for the caller.

        Args:
            data: Validated creation input (see
                :class:`~bond_accounting.account_operations.dto.AccountOperationCreate`).
            user_id: Id of the requesting user; the operation is created for
                this user on one of *their* broker accounts.

        Returns:
            The DTO of the created operation (with the generated ``id``).

        Raises:
            BrokerAccountNotFoundError: If the referenced broker account does
                not exist or belongs to another user.
        """
        async with self._session_factory() as session:
            account = await session.scalar(
                select(BrokerAccount).where(
                    BrokerAccount.id == data.broker_account_id,
                    BrokerAccount.user_id == user_id,
                )
            )
            if account is None:
                raise BrokerAccountNotFoundError(
                    f"Broker account {data.broker_account_id} not found or not "
                    f"owned by user {user_id}"
                )
            operation = AccountOperation(user_id=user_id, **data.model_dump())
            session.add(operation)
            try:
                await session.flush()
                await session.refresh(operation)
                dto = AccountOperationDTO.from_orm(operation)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        logger.info(
            "Account operation created: id=%s user_id=%s broker_account_id=%s type=%s amount=%s",
            dto.id,
            dto.user_id,
            dto.broker_account_id,
            dto.type,
            dto.amount,
        )
        return dto

    async def list_for_user(self, user_id: int) -> list[AccountOperationDTO]:
        """List all operations of a user.

        Args:
            user_id: User whose operations are listed.

        Returns:
            Operations ordered by ``date``, then ``id``.
        """
        stmt = (
            select(AccountOperation)
            .where(AccountOperation.user_id == user_id)
            .order_by(AccountOperation.date, AccountOperation.id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [AccountOperationDTO.from_orm(op) for op in result.scalars().all()]

    async def list_for_account(self, account_id: int, user_id: int) -> list[AccountOperationDTO]:
        """List a user's operations on one broker account.

        Args:
            account_id: Broker account to restrict the listing to.
            user_id: User whose operations are listed.

        Returns:
            The user's operations on the account (empty for an account the
            user does not own), ordered by ``date``, then ``id``.
        """
        stmt = (
            select(AccountOperation)
            .where(
                AccountOperation.user_id == user_id,
                AccountOperation.broker_account_id == account_id,
            )
            .order_by(AccountOperation.date, AccountOperation.id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [AccountOperationDTO.from_orm(op) for op in result.scalars().all()]

    async def get(self, operation_id: int, user_id: int) -> AccountOperationDTO | None:
        """Fetch one operation by id.

        Args:
            operation_id: Primary key of the operation.
            user_id: Id of the requesting user; only the owner may read it.

        Returns:
            The operation DTO, or ``None`` if it does not exist.

        Raises:
            AccountOperationForbiddenError: If the operation is owned by
                another user.
        """
        async with self._session_factory() as session:
            operation = await session.get(AccountOperation, operation_id)
            if operation is None:
                return None
            if operation.user_id != user_id:
                raise AccountOperationForbiddenError(
                    f"Account operation {operation_id} is not owned by user {user_id}"
                )
            return AccountOperationDTO.from_orm(operation)

    async def update(
        self, operation_id: int, data: AccountOperationUpdate, user_id: int
    ) -> AccountOperationDTO | None:
        """Update explicitly provided fields.

        Fields are taken from ``data.model_dump(exclude_unset=True)``, so
        fields omitted from the request leave the columns untouched. An
        explicit ``null`` only reaches the DB for nullable columns (see
        :attr:`~bond_accounting.account_operations.dto.AccountOperationUpdate.NULLABLE_FIELDS`);
        for the rest it means "not provided".

        Args:
            operation_id: Primary key of the operation to update.
            data: Partial update input.
            user_id: Id of the requesting user; only the owner may update.

        Returns:
            The updated operation DTO, or ``None`` if it does not exist.

        Raises:
            AccountOperationForbiddenError: If the operation is owned by
                another user.
            BrokerAccountNotFoundError: If a new ``broker_account_id`` is
                given and the account does not exist or belongs to another
                user.
        """
        async with self._session_factory() as session:
            operation = await session.get(AccountOperation, operation_id)
            if operation is None:
                logger.warning(
                    "Update of nonexistent account operation skipped: id=%s", operation_id
                )
                return None
            if operation.user_id != user_id:
                raise AccountOperationForbiddenError(
                    f"Account operation {operation_id} is not owned by user {user_id}"
                )
            changes = {
                field: value
                for field, value in data.model_dump(exclude_unset=True).items()
                # An explicit ``null`` only reaches the DB for nullable
                # columns; for the rest it means "not provided".
                if value is not None or field in AccountOperationUpdate.NULLABLE_FIELDS
            }
            if "broker_account_id" in changes:
                account = await session.scalar(
                    select(BrokerAccount).where(
                        BrokerAccount.id == changes["broker_account_id"],
                        BrokerAccount.user_id == user_id,
                    )
                )
                if account is None:
                    raise BrokerAccountNotFoundError(
                        f"Broker account {changes['broker_account_id']} not found or not "
                        f"owned by user {user_id}"
                    )
            for field, value in changes.items():
                setattr(operation, field, value)
            try:
                await session.flush()
                await session.refresh(operation)
                dto = AccountOperationDTO.from_orm(operation)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        logger.info("Account operation updated: id=%s changes=%s", dto.id, sorted(changes))
        return dto

    async def delete(self, operation_id: int, user_id: int) -> bool:
        """Delete the operation.

        Args:
            operation_id: Primary key of the operation to delete.
            user_id: Id of the requesting user; only the owner may delete.

        Returns:
            ``True`` if the operation was deleted, ``False`` if it did not exist.

        Raises:
            AccountOperationForbiddenError: If the operation is owned by
                another user.
        """
        async with self._session_factory() as session:
            operation = await session.get(AccountOperation, operation_id)
            if operation is None:
                logger.warning(
                    "Delete of nonexistent account operation skipped: id=%s", operation_id
                )
                return False
            if operation.user_id != user_id:
                raise AccountOperationForbiddenError(
                    f"Account operation {operation_id} is not owned by user {user_id}"
                )
            await session.delete(operation)
            try:
                await session.flush()
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        logger.info("Account operation deleted: id=%s user_id=%s", operation_id, user_id)
        return True
