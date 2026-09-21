"""Immutable message envelope exchanged over the internal event bus."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


@dataclass(frozen=True)
class Message:
    """A single event flowing through the event bus.

    Messages are immutable value objects: producers build them (directly or
    via :meth:`~bond_accounting.event_bus.event_bus.EventBus.publish`) and
    consumers must not mutate ``payload``.

    Attributes:
        topic: Target topic; must be one of
            :data:`~bond_accounting.event_bus.topics.ALL_TOPICS`.
        payload: Arbitrary mapping carried by the event. The bus does not
            validate its contents; producers and consumers agree on the
            schema per topic.
        sender: Name of the producing module, e.g. ``"bonds"``.
        message_id: Unique identifier generated per message. Replies
            reference the request's id via ``reply_to``.
        timestamp: UTC creation time.
        reply_to: ``message_id`` of the request this message replies to, or
            ``None`` for fire-and-forget events.

    Raises:
        ValueError: If ``topic`` or ``sender`` is empty.
    """

    topic: str
    payload: Mapping[str, Any]
    sender: str
    message_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    reply_to: str | None = None

    def __post_init__(self) -> None:
        """Validate that ``topic`` and ``sender`` are non-empty."""
        if not self.topic:
            raise ValueError("Message.topic must be a non-empty string")
        if not self.sender:
            raise ValueError("Message.sender must be a non-empty string")
