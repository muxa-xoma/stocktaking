"""Portfolio service: transaction recording, position calculation, events.

The service owns three responsibilities:

1. Recording BUY/SELL/MATURE transactions with business-rule validation
   (a SELL/MATURE may not drive the position below zero).
2. Deriving positions from the transaction history (no ``position`` column
   is stored; see :mod:`bond_accounting.db.models`).
3. Publishing ``transaction.created`` and ``position.updated`` events to
   the :class:`~bond_accounting.event_bus.EventBus` after every successful
   insert.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bond_accounting.db.models import (
    TRANSACTION_TYPES,
    BrokerAccount,
    Transaction,
    User,
)
from bond_accounting.event_bus import Topic
from bond_accounting.portfolio.dto import PositionDTO, TransactionCreate, TransactionDTO
from bond_accounting.portfolio.netting import net_position

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.event_bus import EventBus

logger = logging.getLogger(__name__)

#: ``sender`` value stamped on every message published by this module.
_SENDER = "portfolio"


class PortfolioError(Exception):
    """Base class for portfolio domain errors."""


class InsufficientPositionError(PortfolioError):
    """A SELL/MATURE operation would drive the position below zero."""


class InvalidTransactionError(PortfolioError):
    """The transaction itself is invalid (unknown type, broken references, ...)."""


class PortfolioService:
    """Transaction recording and position calculation."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], event_bus: EventBus
    ) -> None:
        """Create the service.

        Args:
            session_factory: Factory producing ``AsyncSession`` objects
                (``expire_on_commit=False`` recommended so committed rows
                stay usable for DTO mapping).
            event_bus: Bus used to publish ``transaction.created`` and
                ``position.updated`` events; must be started by the caller.
        """
        self._session_factory = session_factory
        self._event_bus = event_bus

    # ------------------------------------------------------------------ #
    # commands

    async def add_transaction(self, user_id: int, data: TransactionCreate) -> TransactionDTO:
        """Record a transaction and publish the corresponding events.

        Locking strategy (TOCTOU protection):

            The position is an aggregate over the ``transactions`` table, not
            a stored row, so there is no single position row to lock with
            ``SELECT ... FOR UPDATE``. Instead, every ``add_transaction``
            call first locks the acting user's row in ``users``
            (``SELECT ... WHERE id = :uid FOR UPDATE``) inside the same
            transaction that computes the position and performs the insert.
            This serializes concurrent SELL/MATURE checks for the same
            user, eliminating the read-check-insert race window. The bond
            row is deliberately *not* locked: positions are per-user and
            there is no cross-user invariant, so a bond lock would only add
            contention without improving correctness. BUY transactions take
            the same lock for uniform reasoning (every position mutation
            for a user is serialized).

            Database behaviour:

            * PostgreSQL — ``FOR UPDATE`` takes a real row lock; a second
              SELL/MATURE for the same user blocks until the first
              transaction commits or rolls back.
            * SQLite — ``FOR UPDATE`` is not supported by the dialect and
              is silently ignored (SQLAlchemy compiles the statement without
              it); correctness is preserved because SQLite serializes
              writers at the database level anyway (WAL / write lock), so no
              concurrent transaction can interleave between the check and
              the insert.

        For SELL/MATURE the current position is checked under that lock: if
        the operation would leave a negative quantity, the insert is rolled
        back and :class:`InsufficientPositionError` is raised. Rows rejected
        by the database (e.g. a nonexistent user or bond) surface as
        :class:`InvalidTransactionError`.

        Args:
            user_id: User performing the operation.
            data: Validated transaction payload.

        Returns:
            The recorded transaction as a :class:`TransactionDTO`.

        Raises:
            InvalidTransactionError: If the type is unknown or the row is
                rejected by database constraints.
            InsufficientPositionError: If a SELL/MATURE exceeds the current
                position.
        """
        if data.type not in TRANSACTION_TYPES:
            # Defensive: TransactionCreate's Literal already guarantees this.
            raise InvalidTransactionError(
                f"Unknown transaction type {data.type!r}; expected one of {TRANSACTION_TYPES}"
            )

        async with self._session_factory() as session:
            try:
                async with session.begin():
                    account = await session.scalar(
                        select(BrokerAccount).where(
                            BrokerAccount.id == data.broker_account_id,
                            BrokerAccount.user_id == user_id,
                        )
                    )
                    if account is None:
                        raise InvalidTransactionError(
                            f"Broker account {data.broker_account_id} not found or not "
                            f"owned by user {user_id}"
                        )
                    # Lock the user row so concurrent SELL/MATURE position
                    # checks for the same user serialize against each other
                    # (see the docstring for the full strategy). If the user
                    # does not exist, no row is locked and the insert below
                    # fails with an FK violation -> InvalidTransactionError.
                    await session.execute(
                        select(User.id).where(User.id == user_id).with_for_update()
                    )
                    if data.type in ("SELL", "MATURE"):
                        current = self._quantity_of(
                            await self._load_transactions(
                                session,
                                user_id,
                                data.bond_id,
                                broker_account_id=data.broker_account_id,
                            )
                        )
                        if current < data.quantity:
                            raise InsufficientPositionError(
                                f"Cannot {data.type} {data.quantity} unit(s) of bond "
                                f"{data.bond_id} for user {user_id}: "
                                f"current position is {current}"
                            )
                    txn = Transaction(
                        user_id=user_id,
                        bond_id=data.bond_id,
                        type=data.type,
                        quantity=data.quantity,
                        price=data.price,
                        date=data.date,
                        commission=data.commission,
                        broker_account_id=data.broker_account_id,
                    )
                    session.add(txn)
            except IntegrityError as exc:
                raise InvalidTransactionError(
                    f"Transaction rejected by the database for user {user_id}, "
                    f"bond {data.bond_id}: {exc.orig}"
                ) from exc

        dto = TransactionDTO.from_orm(txn)
        position = await self.get_position(user_id, data.bond_id)
        logger.info(
            (
                "Recorded transaction id=%s type=%s user_id=%s bond_id=%s quantity=%s price=%s; "
                "position is now %s unit(s)"
            ),
            dto.id,
            dto.type,
            dto.user_id,
            dto.bond_id,
            dto.quantity,
            dto.price,
            position.quantity,
        )

        await self._event_bus.publish(Topic.TRANSACTION_CREATED, dto.model_dump(), sender=_SENDER)
        await self._event_bus.publish(Topic.POSITION_UPDATED, position.model_dump(), sender=_SENDER)
        return dto

    # ------------------------------------------------------------------ #
    # queries

    async def list_transactions(
        self, user_id: int, bond_id: int | None = None, broker_account_id: int | None = None
    ) -> list[TransactionDTO]:
        """List transactions for a user, optionally filtered by bond.

        Args:
            user_id: User whose transactions are listed.
            bond_id: When given, only transactions on this bond are returned.
            broker_account_id: When given, only transactions on this broker
                account are returned.

        Returns:
            Transactions ordered by ``date``, then ``id``.
        """
        stmt = select(Transaction).where(Transaction.user_id == user_id)
        if bond_id is not None:
            stmt = stmt.where(Transaction.bond_id == bond_id)
        if broker_account_id is not None:
            stmt = stmt.where(Transaction.broker_account_id == broker_account_id)
        stmt = stmt.order_by(Transaction.date, Transaction.id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [TransactionDTO.from_orm(txn) for txn in result.scalars().all()]

    async def get_position(
        self, user_id: int, bond_id: int, broker_account_id: int | None = None
    ) -> PositionDTO:
        """Current position for one user-bond pair.

        Args:
            user_id: User whose position is computed.
            bond_id: Bond the position is held in.
            broker_account_id: When given, restrict the computation to
                transactions on this broker account.

        Returns:
            The derived position; a pair with no history yields a closed
            (zero) position, not an error.
        """
        async with self._session_factory() as session:
            txns = await self._load_transactions(
                session, user_id, bond_id, broker_account_id=broker_account_id
            )
        return self._to_position(user_id, bond_id, txns)

    async def get_all_positions(
        self, user_id: int, broker_account_id: int | None = None
    ) -> list[PositionDTO]:
        """All non-zero positions for a user.

        Args:
            user_id: User whose positions are computed.
            broker_account_id: When given, restrict the computation to
                transactions on this broker account.

        Returns:
            Positions with ``quantity != 0``, keyed by bond.
        """
        async with self._session_factory() as session:
            stmt = select(Transaction).where(Transaction.user_id == user_id)
            if broker_account_id is not None:
                stmt = stmt.where(Transaction.broker_account_id == broker_account_id)
            result = await session.execute(stmt.order_by(Transaction.date, Transaction.id))
            txns = list(result.scalars().all())

        grouped: dict[int, list[Transaction]] = {}
        for txn in txns:
            grouped.setdefault(txn.bond_id, []).append(txn)

        return [
            position
            for bond_id, rows in grouped.items()
            if (position := self._to_position(user_id, bond_id, rows)).quantity != 0
        ]

    async def get_position_value(
        self, user_id: int, bond_id: int, broker_account_id: int | None = None
    ) -> float:
        """Value the position at the average-cost price of the open position.

        Args:
            user_id: User whose position is valued.
            bond_id: Bond the position is held in.
            broker_account_id: When given, restrict the computation to
                transactions on this broker account.

        Returns:
            ``quantity * avg_buy_price`` using the same average-cost basis
            as :class:`PositionDTO`, or ``0.0`` when there is no open
            position (nothing held or no history at all).
        """
        async with self._session_factory() as session:
            txns = await self._load_transactions(
                session, user_id, bond_id, broker_account_id=broker_account_id
            )
        quantity, avg_buy_price = net_position(txns)
        if quantity > 0 and avg_buy_price is not None:
            return quantity * avg_buy_price
        return 0.0

    # ------------------------------------------------------------------ #
    # internals

    @staticmethod
    async def _load_transactions(
        session: AsyncSession,
        user_id: int,
        bond_id: int,
        broker_account_id: int | None = None,
    ) -> list[Transaction]:
        """Fetch all transactions for one user-bond pair, ordered by date, id."""
        stmt = select(Transaction).where(
            Transaction.user_id == user_id, Transaction.bond_id == bond_id
        )
        if broker_account_id is not None:
            stmt = stmt.where(Transaction.broker_account_id == broker_account_id)
        result = await session.execute(stmt.order_by(Transaction.date, Transaction.id))
        return list(result.scalars().all())

    @staticmethod
    def _quantity_of(txns: Sequence[Transaction]) -> int:
        """Derive the held quantity: BUY minus SELL minus MATURE."""
        quantity = 0
        for txn in txns:
            if txn.type == "BUY":
                quantity += txn.quantity
            else:  # SELL / MATURE (CHECK-constrained in the DB)
                quantity -= txn.quantity
        return quantity

    @staticmethod
    def _to_position(user_id: int, bond_id: int, txns: Sequence[Transaction]) -> PositionDTO:
        """Build a :class:`PositionDTO` from a bond's transaction history.

        ``avg_buy_price`` is the average-cost price of the currently open
        position (see :func:`bond_accounting.portfolio.netting.net_position`);
        it is ``None`` when the position is flat.
        """
        quantity, avg_buy_price = net_position(txns)

        if quantity > 0 and avg_buy_price is not None:
            return PositionDTO(
                user_id=user_id,
                bond_id=bond_id,
                quantity=quantity,
                avg_buy_price=avg_buy_price,
                total_invested=avg_buy_price * quantity,
            )
        return PositionDTO(
            user_id=user_id,
            bond_id=bond_id,
            quantity=quantity,
            avg_buy_price=None,
            total_invested=0.0,
        )
