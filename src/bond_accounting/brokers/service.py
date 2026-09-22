"""CRUD service for brokers and broker accounts with EventBus publishing.

Ordering guarantee (same as ``bond_accounting.bonds.service``): every
mutation (create/update/delete) flushes its DB writes, publishes the
corresponding ``broker.*`` / ``broker_account.*`` event, and only then
commits. If publishing fails (e.g. the event bus is stopped and raises
``RuntimeError``), the operation aborts and the transaction is rolled
back — a committed DB change without notification is considered worse
than a failed operation. The reverse (a delivered event without a commit)
cannot happen unless the commit itself fails *after* a successful publish.

Domain rules:

* Brokers live in a shared registry — any authenticated user may create,
  update and delete them; there is no per-broker ownership.
* Broker accounts belong to a single user (``user_id``); only the owner
  may update or delete an account.
* A broker with accounts cannot be deleted
  (:class:`~bond_accounting.brokers.exceptions.BrokerHasAccountsError`), and
  an account with transactions cannot be deleted
  (:class:`~bond_accounting.brokers.exceptions.BrokerAccountHasTransactionsError`)
  — likewise an account with account operations
  (:class:`~bond_accounting.brokers.exceptions.BrokerAccountHasOperationsError`).

Subscriber note: handlers run asynchronously on the bus's dispatcher
tasks (separate from the publisher), so a handler may start before the
publisher's transaction commits. No subscriber assumes the publisher's
commit precedes handler execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bond_accounting.brokers.dto import (
    BrokerAccountCreate,
    BrokerAccountDTO,
    BrokerAccountUpdate,
    BrokerCreate,
    BrokerDTO,
    BrokerUpdate,
)
from bond_accounting.brokers.exceptions import (
    BrokerAccountHasOperationsError,
    BrokerAccountHasTransactionsError,
    BrokerAccountNotFoundError,
    BrokerHasAccountsError,
    BrokerNameDuplicateError,
    BrokerNotFoundError,
)
from bond_accounting.db.models import AccountOperation, Broker, BrokerAccount, Transaction
from bond_accounting.event_bus import EventBus, Topic

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

#: Sender name stamped on every message published by this service.
_SENDER = "brokers"


class BrokerService:
    """CRUD service for brokers and broker accounts with EventBus publishing."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
    ) -> None:
        """Initialize the service.

        Args:
            session_factory: Factory of ``AsyncSession`` objects; each service
                method opens its own short-lived session from it.
            event_bus: Running event bus used to publish ``broker.*`` /
                ``broker_account.*`` events.
        """
        self._session_factory = session_factory
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Broker CRUD (shared registry, any authenticated user can call)
    # ------------------------------------------------------------------

    async def create_broker(self, data: BrokerCreate) -> BrokerDTO:
        """Insert a new broker and publish ``broker.created``.

        Args:
            data: Validated creation input (see
                :class:`~bond_accounting.brokers.dto.BrokerCreate`).

        Returns:
            The DTO of the created broker (with the generated ``id``).

        Raises:
            BrokerNameDuplicateError: If a broker with the same name already
                exists (including the unique-constraint race window).
            RuntimeError: If the event bus is not running; the insert is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            existing = await session.scalar(select(Broker).where(Broker.name == data.name))
            if existing is not None:
                raise BrokerNameDuplicateError(f"Broker with name {data.name!r} already exists")

            broker = Broker(**data.model_dump())
            session.add(broker)
            try:
                await session.flush()
                await session.refresh(broker)
                dto = BrokerDTO.from_orm(broker)
                await self._publish(Topic.BROKER_CREATED, dto.model_dump())
                await session.commit()
            except IntegrityError as exc:  # race: unique constraint still guards us
                await session.rollback()
                raise BrokerNameDuplicateError(
                    f"Broker with name {data.name!r} already exists"
                ) from exc
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Broker created: id=%s name=%s", dto.id, dto.name)
        return dto

    async def get_broker(self, broker_id: int) -> BrokerDTO | None:
        """Fetch by primary key.

        Returns:
            The broker DTO, or ``None`` if no broker with this id exists.
        """
        async with self._session_factory() as session:
            broker = await session.get(Broker, broker_id)
            return BrokerDTO.from_orm(broker) if broker is not None else None

    async def list_all_brokers(self) -> list[BrokerDTO]:
        """Return all brokers, ordered by id (ascending)."""
        async with self._session_factory() as session:
            brokers = (await session.scalars(select(Broker).order_by(Broker.id))).all()
            return [BrokerDTO.from_orm(broker) for broker in brokers]

    async def update_broker(self, broker_id: int, data: BrokerUpdate) -> BrokerDTO | None:
        """Update explicitly provided fields and publish ``broker.updated``.

        Fields are taken from ``data.model_dump(exclude_unset=True)``, so
        fields omitted from the request leave the columns untouched. An
        explicit ``null`` only reaches the DB for nullable columns (see
        :attr:`~bond_accounting.brokers.dto.BrokerUpdate.NULLABLE_FIELDS`);
        for the rest it means "not provided".

        Args:
            broker_id: Primary key of the broker to update.
            data: Partial update input.

        Returns:
            The updated broker DTO, or ``None`` if the broker does not exist.

        Raises:
            BrokerNameDuplicateError: If the rename collides with an existing
                broker name.
            RuntimeError: If the event bus is not running; the update is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            broker = await session.get(Broker, broker_id)
            if broker is None:
                logger.warning("Update of nonexistent broker skipped: id=%s", broker_id)
                return None
            changes = {
                field: value
                for field, value in data.model_dump(exclude_unset=True).items()
                # An explicit ``null`` only reaches the DB for nullable
                # columns; for the rest it means "not provided".
                if value is not None or field in BrokerUpdate.NULLABLE_FIELDS
            }
            if "name" in changes:
                existing = await session.scalar(
                    select(Broker).where(Broker.name == changes["name"], Broker.id != broker_id)
                )
                if existing is not None:
                    raise BrokerNameDuplicateError(
                        f"Broker with name {changes['name']!r} already exists"
                    )
            for field, value in changes.items():
                setattr(broker, field, value)
            try:
                await session.flush()
                await session.refresh(broker)
                dto = BrokerDTO.from_orm(broker)
                await self._publish(Topic.BROKER_UPDATED, dto.model_dump())
                await session.commit()
            except IntegrityError as exc:  # race: unique constraint still guards us
                await session.rollback()
                # ``changes["name"]``, not ``broker.name``: after the rollback
                # the ORM instance is detached/expired and touching its
                # attributes triggers sync IO (MissingGreenlet in async).
                raise BrokerNameDuplicateError(
                    f"Broker with name {changes['name']!r} already exists"
                ) from exc
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Broker updated: id=%s changes=%s", dto.id, sorted(changes))
        return dto

    async def delete_broker(self, broker_id: int) -> bool:
        """Delete the broker and publish ``broker.deleted``.

        Args:
            broker_id: Primary key of the broker to delete.

        Returns:
            ``True`` if the broker was deleted, ``False`` if it did not exist.

        Raises:
            BrokerHasAccountsError: If the broker has at least one account.
            RuntimeError: If the event bus is not running; the deletion is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            broker = await session.get(Broker, broker_id)
            if broker is None:
                logger.warning("Delete of nonexistent broker skipped: id=%s", broker_id)
                return False
            has_accounts = await session.scalar(
                select(func.count())
                .select_from(BrokerAccount)
                .where(BrokerAccount.broker_id == broker_id)
            )
            if has_accounts:
                raise BrokerHasAccountsError(
                    f"Broker {broker_id} has {has_accounts} account(s) and cannot be deleted"
                )
            await session.delete(broker)
            try:
                await session.flush()
                await self._publish(Topic.BROKER_DELETED, {"broker_id": broker_id})
                await session.commit()
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Broker deleted: id=%s", broker_id)
        return True

    # ------------------------------------------------------------------
    # BrokerAccount CRUD (per-user)
    # ------------------------------------------------------------------

    async def create_account(self, data: BrokerAccountCreate, *, user_id: int) -> BrokerAccountDTO:
        """Insert a new broker account owned by ``user_id`` and publish
        ``broker_account.created``.

        Args:
            data: Validated creation input (see
                :class:`~bond_accounting.brokers.dto.BrokerAccountCreate`).
            user_id: Id of the creating user; stored as the account's owner.

        Returns:
            The DTO of the created account (with the generated ``id``).

        Raises:
            BrokerNotFoundError: If the referenced broker does not exist.
            RuntimeError: If the event bus is not running; the insert is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            broker = await session.get(Broker, data.broker_id)
            if broker is None:
                raise BrokerNotFoundError(f"Broker {data.broker_id} does not exist")

            account = BrokerAccount(**data.model_dump(), user_id=user_id)
            session.add(account)
            try:
                await session.flush()
                await session.refresh(account)
                dto = BrokerAccountDTO.from_orm(account, broker_name=broker.name)
                await self._publish(Topic.BROKER_ACCOUNT_CREATED, dto.model_dump())
                await session.commit()
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info(
            "Broker account created: id=%s broker_id=%s user_id=%s", dto.id, dto.broker_id, user_id
        )
        return dto

    async def get_account(self, account_id: int, *, user_id: int) -> BrokerAccountDTO | None:
        """Fetch by primary key, joined with the broker for ``broker_name``.

        Args:
            account_id: Id of the account to fetch.
            user_id: Id of the requesting user; the account is only returned
                when this user owns it.

        Returns:
            The account DTO, or ``None`` if no account with this id exists or
            it is owned by another user.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(BrokerAccount, Broker.name)
                    .join(Broker, BrokerAccount.broker_id == Broker.id)
                    .where(
                        BrokerAccount.id == account_id,
                        BrokerAccount.user_id == user_id,
                    )
                )
            ).first()
            return (
                BrokerAccountDTO.from_orm(row[0], broker_name=row[1]) if row is not None else None
            )

    async def list_accounts_for_user(self, user_id: int) -> list[BrokerAccountDTO]:
        """Return all accounts of a user, ordered by id (ascending)."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(BrokerAccount, Broker.name)
                    .join(Broker, BrokerAccount.broker_id == Broker.id)
                    .where(BrokerAccount.user_id == user_id)
                    .order_by(BrokerAccount.id)
                )
            ).all()
            return [
                BrokerAccountDTO.from_orm(account, broker_name=broker_name)
                for account, broker_name in rows
            ]

    async def list_accounts_for_broker(self, broker_id: int) -> list[BrokerAccountDTO]:
        """Return all accounts at a broker, ordered by id (ascending)."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(BrokerAccount, Broker.name)
                    .join(Broker, BrokerAccount.broker_id == Broker.id)
                    .where(BrokerAccount.broker_id == broker_id)
                    .order_by(BrokerAccount.id)
                )
            ).all()
            return [
                BrokerAccountDTO.from_orm(account, broker_name=broker_name)
                for account, broker_name in rows
            ]

    async def update_account(
        self, account_id: int, data: BrokerAccountUpdate, *, user_id: int
    ) -> BrokerAccountDTO | None:
        """Update explicitly provided fields and publish ``broker_account.updated``.

        Fields are taken from ``data.model_dump(exclude_unset=True)``, so
        fields omitted from the request leave the columns untouched. An
        explicit ``null`` only reaches the DB for nullable columns (see
        :attr:`~bond_accounting.brokers.dto.BrokerAccountUpdate.NULLABLE_FIELDS`);
        for the rest it means "not provided".

        Args:
            account_id: Primary key of the account to update.
            data: Partial update input.
            user_id: Id of the requesting user; only the owner may update.

        Returns:
            The updated account DTO, or ``None`` if the account does not exist.

        Raises:
            BrokerAccountNotFoundError: If the account is owned by another user.
            RuntimeError: If the event bus is not running; the update is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            account = await session.get(BrokerAccount, account_id)
            if account is None:
                logger.warning("Update of nonexistent broker account skipped: id=%s", account_id)
                return None
            if account.user_id != user_id:
                raise BrokerAccountNotFoundError(
                    f"Broker account {account_id} is not owned by user {user_id}"
                )
            changes = {
                field: value
                for field, value in data.model_dump(exclude_unset=True).items()
                # An explicit ``null`` only reaches the DB for nullable
                # columns; for the rest it means "not provided".
                if value is not None or field in BrokerAccountUpdate.NULLABLE_FIELDS
            }
            for field, value in changes.items():
                setattr(account, field, value)
            try:
                await session.flush()
                await session.refresh(account)
                broker = await session.get(Broker, account.broker_id)
                dto = BrokerAccountDTO.from_orm(
                    account, broker_name=broker.name if broker is not None else ""
                )
                await self._publish(Topic.BROKER_ACCOUNT_UPDATED, dto.model_dump())
                await session.commit()
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Broker account updated: id=%s changes=%s", dto.id, sorted(changes))
        return dto

    async def delete_account(self, account_id: int, *, user_id: int) -> bool:
        """Delete the account and publish ``broker_account.deleted``.

        Args:
            account_id: Primary key of the account to delete.
            user_id: Id of the requesting user; only the owner may delete.

        Returns:
            ``True`` if the account was deleted, ``False`` if it did not exist.

        Raises:
            BrokerAccountNotFoundError: If the account is owned by another user.
            BrokerAccountHasTransactionsError: If the account has at least
                one transaction.
            BrokerAccountHasOperationsError: If the account has at least one
                account operation.
            RuntimeError: If the event bus is not running; the deletion is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            account = await session.get(BrokerAccount, account_id)
            if account is None:
                logger.warning("Delete of nonexistent broker account skipped: id=%s", account_id)
                return False
            if account.user_id != user_id:
                raise BrokerAccountNotFoundError(
                    f"Broker account {account_id} is not owned by user {user_id}"
                )
            has_txns = await session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.broker_account_id == account_id)
            )
            if has_txns:
                raise BrokerAccountHasTransactionsError(
                    f"Broker account {account_id} has {has_txns} "
                    "transaction(s) and cannot be deleted"
                )
            has_ops = await session.scalar(
                select(func.count())
                .select_from(AccountOperation)
                .where(AccountOperation.broker_account_id == account_id)
            )
            if has_ops:
                raise BrokerAccountHasOperationsError(
                    f"Broker account {account_id} has {has_ops} "
                    "account operation(s) and cannot be deleted"
                )
            await session.delete(account)
            try:
                await session.flush()
                await self._publish(
                    Topic.BROKER_ACCOUNT_DELETED,
                    {"broker_account_id": account_id, "user_id": user_id},
                )
                await session.commit()
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Broker account deleted: id=%s user_id=%s", account_id, user_id)
        return True

    async def _publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        """Publish a payload on the given topic with the module's sender name."""
        message_id = await self._event_bus.publish(topic, payload, sender=_SENDER)
        logger.debug("Published %s: message_id=%s", topic, message_id)
