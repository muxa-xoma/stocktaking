"""CRUD service for bonds with EventBus publishing.

Ordering guarantee: every mutation (create/update/delete) flushes its DB
writes, publishes the corresponding ``bond.*`` event(s), and only then
commits. If publishing fails (e.g. the event bus is stopped and raises
``RuntimeError``), the operation aborts and the transaction is rolled
back — a committed DB change without notification is considered worse
than a failed operation. The reverse (a delivered event without a commit)
cannot happen unless the commit itself fails *after* a successful publish.

``bond.updated`` / ``bond.deleted`` events are fanned out per *holder* —
every user with transactions on the bond receives a separate event whose
payload additionally carries that holder's ``user_id`` (stamped on top of
the flat bond fields, mirroring how ``transaction.created`` and
``position.updated`` carry their user context). Bonds without holders
publish nothing: there is nobody whose portfolio needs recalculating.

Subscriber note: handlers run asynchronously on the bus's dispatcher
tasks (separate from the publisher), so a handler may start before the
publisher's transaction commits. Subscribers such as analytics read the
DB in their own sessions; with WAL (the default SQLite journal mode here)
they observe a stable snapshot — either the pre- or the post-commit state,
never a partial one — and the eventual consistency converges on the next
read/event. No subscriber assumes the publisher's commit precedes
handler execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bond_accounting.bonds.dto import BondCreate, BondDTO, BondUpdate
from bond_accounting.db.models import Bond, Transaction
from bond_accounting.event_bus import EventBus, Topic

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

#: Sender name stamped on every message published by this service.
_SENDER = "bonds"


class BondError(Exception):
    """Base error for bond operations."""


class BondNotFoundError(BondError):
    """Raised when a referenced bond does not exist."""


class BondIsinDuplicateError(BondError):
    """Raised when creating a bond whose ISIN already exists.

    Attributes:
        isin: The duplicated ISIN.
    """

    def __init__(self, isin: str) -> None:
        super().__init__(f"Bond with ISIN {isin!r} already exists")
        self.isin = isin


class BondDeletionBlockedError(BondError):
    """Raised when deleting a bond that has transactions."""


class BondNotOwnedError(BondError):
    """Raised when a user mutates a bond owned by somebody else.

    Any authenticated user can create a bond, but only the owner may
    update or delete it.

    Attributes:
        bond_id: Primary key of the bond.
        user_id: Id of the user who attempted the mutation.
    """

    def __init__(self, bond_id: int, user_id: int) -> None:
        super().__init__(f"Bond {bond_id} is not owned by user {user_id}")
        self.bond_id = bond_id
        self.user_id = user_id


class BondService:
    """CRUD service for bonds with EventBus publishing."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
    ) -> None:
        """Initialize the service.

        Args:
            session_factory: Factory of ``AsyncSession`` objects; each service
                method opens its own short-lived session from it.
            event_bus: Running event bus used to publish ``bond.*`` events.
        """
        self._session_factory = session_factory
        self._event_bus = event_bus

    async def create(self, data: BondCreate, *, user_id: int) -> BondDTO:
        """Insert a new bond owned by ``user_id`` and publish ``bond.created``.

        Args:
            data: Validated creation input (see :class:`~bond_accounting.bonds.dto.BondCreate`).
            user_id: Id of the creating user; stored as the bond's owner.

        Returns:
            The DTO of the created bond (with the generated ``id``).

        Raises:
            BondIsinDuplicateError: If a bond with the same ISIN already exists.
            RuntimeError: If the event bus is not running; the insert is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            existing = await session.scalar(select(Bond).where(Bond.isin == data.isin))
            if existing is not None:
                raise BondIsinDuplicateError(data.isin)

            bond = Bond(**data.model_dump(), owner_id=user_id)
            session.add(bond)
            try:
                await session.flush()
                await session.refresh(bond)
                dto = BondDTO.from_orm(bond)
                await self._publish(Topic.BOND_CREATED, dto.model_dump())
                await session.commit()
            except IntegrityError as exc:  # race: unique constraint still guards us
                await session.rollback()
                raise BondIsinDuplicateError(data.isin) from exc
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Bond created: id=%s isin=%s", dto.id, dto.isin)
        return dto

    async def get(self, bond_id: int) -> BondDTO | None:
        """Fetch by primary key.

        Returns:
            The bond DTO, or ``None`` if no bond with this id exists.
        """
        async with self._session_factory() as session:
            bond = await session.get(Bond, bond_id)
            return BondDTO.from_orm(bond) if bond is not None else None

    async def get_by_isin(self, isin: str) -> BondDTO | None:
        """Fetch by ISIN.

        Returns:
            The bond DTO, or ``None`` if no bond with this ISIN exists.
        """
        async with self._session_factory() as session:
            bond = await session.scalar(select(Bond).where(Bond.isin == isin))
            return BondDTO.from_orm(bond) if bond is not None else None

    async def list_all(self) -> list[BondDTO]:
        """Return all bonds, ordered by id (ascending)."""
        async with self._session_factory() as session:
            bonds = (await session.scalars(select(Bond).order_by(Bond.id))).all()
            return [BondDTO.from_orm(bond) for bond in bonds]

    async def update(self, bond_id: int, data: BondUpdate, *, user_id: int) -> BondDTO | None:
        """Update explicitly provided fields and publish ``bond.updated``.

        Fields are taken from ``data.model_dump(exclude_unset=True)``, so
        fields omitted from the request leave the columns untouched. The only
        nullable column is ``issuer`` (see
        :attr:`~bond_accounting.bonds.dto.BondUpdate.NULLABLE_FIELDS`): an
        explicitly provided ``null`` for it clears the column, while
        ``null`` for any other field is ignored (treated as "not provided").

        One ``bond.updated`` event is published per holder (user with
        transactions on the bond); the payload is the updated ``BondDTO``
        dump plus the holder's ``user_id``. If the bond has no holders,
        nothing is published.

        Args:
            bond_id: Primary key of the bond to update.
            data: Partial update input.
            user_id: Id of the requesting user; only the owner may update.

        Returns:
            The updated bond DTO, or ``None`` if the bond does not exist.

        Raises:
            BondNotOwnedError: If the bond is owned by another user.
            RuntimeError: If the event bus is not running; the update is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            bond = await session.get(Bond, bond_id)
            if bond is None:
                logger.warning("Update of nonexistent bond skipped: id=%s", bond_id)
                return None
            if bond.owner_id != user_id:
                raise BondNotOwnedError(bond_id, user_id)
            changes = {
                field: value
                for field, value in data.model_dump(exclude_unset=True).items()
                # An explicit ``null`` only reaches the DB for nullable
                # columns (``issuer``); for the rest it means "not provided".
                if value is not None or field in BondUpdate.NULLABLE_FIELDS
            }
            for field, value in changes.items():
                setattr(bond, field, value)
            try:
                await session.flush()
                await session.refresh(bond)
                dto = BondDTO.from_orm(bond)
                holder_ids = await self._holder_ids(session, bond_id)
                await self._publish_for_holders(Topic.BOND_UPDATED, holder_ids, dto.model_dump())
                await session.commit()
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Bond updated: id=%s isin=%s changes=%s", dto.id, dto.isin, sorted(changes))
        return dto

    async def delete(self, bond_id: int, *, user_id: int) -> bool:
        """Delete the bond and publish ``bond.deleted``.

        One ``bond.deleted`` event is published per holder (user with
        transactions on the bond); the payload is ``{"bond_id": ...}``
        plus the holder's ``user_id``. If the bond has no holders, nothing
        is published.

        Args:
            bond_id: Primary key of the bond to delete.
            user_id: Id of the requesting user; only the owner may delete.

        Returns:
            ``True`` if the bond was deleted, ``False`` if it did not exist.

        Raises:
            BondNotOwnedError: If the bond is owned by another user.
            BondDeletionBlockedError: If the bond has at least one transaction.
            RuntimeError: If the event bus is not running; the deletion is
                rolled back and no DB change occurs.
        """
        async with self._session_factory() as session:
            bond = await session.get(Bond, bond_id)
            if bond is None:
                logger.warning("Delete of nonexistent bond skipped: id=%s", bond_id)
                return False
            if bond.owner_id != user_id:
                raise BondNotOwnedError(bond_id, user_id)
            has_txns = await session.scalar(
                select(func.count()).select_from(Transaction).where(Transaction.bond_id == bond_id)
            )
            if has_txns:
                raise BondDeletionBlockedError(
                    f"Bond {bond_id} has {has_txns} transaction(s) and cannot be deleted"
                )
            await session.delete(bond)
            try:
                await session.flush()
                # Deletion is blocked while transactions exist, so in practice
                # this is empty; queried anyway to keep the fan-out correct if
                # the blocking rule is ever relaxed.
                holder_ids = await self._holder_ids(session, bond_id)
                await self._publish_for_holders(
                    Topic.BOND_DELETED, holder_ids, {"bond_id": bond_id}
                )
                await session.commit()
            except Exception:
                # Publish failure (e.g. stopped bus) aborts the operation.
                await session.rollback()
                raise

        logger.info("Bond deleted: id=%s", bond_id)
        return True

    async def _publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        """Publish a payload on the given topic with the module's sender name."""
        message_id = await self._event_bus.publish(topic, payload, sender=_SENDER)
        logger.debug("Published %s: message_id=%s", topic, message_id)

    @staticmethod
    async def _holder_ids(session: AsyncSession, bond_id: int) -> list[int]:
        """Distinct ids of the users holding (having transactions on) a bond."""
        result = await session.scalars(
            select(Transaction.user_id)
            .where(Transaction.bond_id == bond_id)
            .distinct()
            .order_by(Transaction.user_id)
        )
        return list(result.all())

    async def _publish_for_holders(
        self, topic: str, holder_ids: Sequence[int], payload: Mapping[str, Any]
    ) -> None:
        """Publish one event per holder, payload enriched with the holder's ``user_id``.

        The base payload stays flat (same shape as the ``bond.created``
        payload) and ``user_id`` is stamped on top, mirroring how
        ``transaction.created`` / ``position.updated`` carry user context.
        With no holders nothing is published (no-op).
        """
        if not holder_ids:
            logger.debug("No holders for bond event: topic=%s not published", topic)
            return
        for holder_id in holder_ids:
            message_id = await self._event_bus.publish(
                topic, {**payload, "user_id": holder_id}, sender=_SENDER
            )
            logger.debug("Published %s for user_id=%s: message_id=%s", topic, holder_id, message_id)
