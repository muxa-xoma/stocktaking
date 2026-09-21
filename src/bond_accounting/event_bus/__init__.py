"""In-process event bus: topic registry, pub/sub, and request/response.

Permanent architectural component: all cross-module async interactions
flow through :class:`EventBus`. See
:mod:`bond_accounting.event_bus.async_queue_bus` for the concrete
implementation and its guarantees (ordering, backpressure, failure
isolation, lifecycle, single-event-loop usage).
"""

from bond_accounting.event_bus.async_queue_bus import AsyncQueueEventBus
from bond_accounting.event_bus.event_bus import (
    EventBus,
    RequestHandlerError,
    RequestTimeoutError,
)
from bond_accounting.event_bus.message import Message
from bond_accounting.event_bus.topics import ALL_TOPICS, Topic

__all__ = [
    "ALL_TOPICS",
    "AsyncQueueEventBus",
    "EventBus",
    "Message",
    "RequestHandlerError",
    "RequestTimeoutError",
    "Topic",
]
