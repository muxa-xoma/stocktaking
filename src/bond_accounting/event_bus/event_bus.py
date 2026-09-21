"""Public interface of the internal event bus.

The event bus is a permanent architectural component: all cross-module
async interactions in the application flow through it. This module defines
the protocol that consumers depend on (plus :class:`RequestTimeoutError`
and :class:`RequestHandlerError`);
the concrete implementation lives in
:mod:`bond_accounting.event_bus.async_queue_bus`.

Topic validation contract: every operation rejects topics that are not
registered in :data:`~bond_accounting.event_bus.topics.ALL_TOPICS` with
``ValueError``. New topics must be added to
:class:`~bond_accounting.event_bus.topics.Topic` before use — there is no
runtime registration path on purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from typing import Any

    from bond_accounting.event_bus.message import Message

    #: Handler for fire-and-forget subscribers: receives the message, returns nothing.
    EventHandler = Callable[[Message], Awaitable[None]]
    #: Handler for request endpoints: receives the request message and returns the reply payload.
    RequestHandler = Callable[[Message], Awaitable[Mapping[str, Any]]]


class RequestTimeoutError(Exception):
    """Raised when a request receives no reply in time.

    The ``message_id`` of the original request message is available both as
    the :attr:`message_id` attribute and in the rendered message.

    Attributes:
        message_id: ``message_id`` of the request that did not get a reply.
    """

    def __init__(self, message_id: str, reason: str = "timed out waiting for a reply") -> None:
        super().__init__(f"{reason} (message_id={message_id!r})")
        self.message_id = message_id


class RequestHandlerError(Exception):
    """Raised by ``request()`` when the handler serving the request fails.

    When a request handler raises while processing a message published via
    ``request()``, the bus resolves the pending reply future with this
    error (instead of leaving it unresolved forever), so the requester
    sees the failure instead of hanging. The original handler exception
    is chained as ``__cause__`` and exposed via :attr:`cause`.

    Attributes:
        topic: Topic the failed request was published on.
        message_id: ``message_id`` of the request whose handler failed.
        cause: The original exception raised by the handler.
    """

    def __init__(self, topic: str, message_id: str, cause: BaseException) -> None:
        super().__init__(
            f"request handler on topic {topic!r} failed for request {message_id!r}: {cause!r}"
        )
        self.topic = topic
        self.message_id = message_id
        self.cause = cause


class EventBus(Protocol):
    """Public async interface other modules depend on.

    Implementations are expected to be single-event-loop components (see
    :class:`~bond_accounting.event_bus.async_queue_bus.AsyncQueueEventBus`
    for the concrete semantics).
    """

    async def start(self) -> None:
        """Start the bus (idempotent): create dispatcher tasks for subscribers."""

    async def stop(self) -> None:
        """Stop the bus (no-op if not running): cancel dispatchers, drop queued messages."""

    @property
    def running(self) -> bool:
        """Whether the bus is currently started."""

    async def publish(self, topic: str, payload: Mapping[str, Any], sender: str) -> str:
        """Publish a fire-and-forget event.

        Args:
            topic: Target topic; must be in ``ALL_TOPICS``.
            payload: Event payload mapping.
            sender: Producing module name.

        Returns:
            The ``message_id`` of the published message.

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
            RuntimeError: If the bus is not running.
        """

    async def request(
        self,
        topic: str,
        payload: Mapping[str, Any],
        sender: str,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Publish a message and await a reply (request/response).

        Args:
            topic: Target topic; must be in ``ALL_TOPICS``.
            payload: Request payload mapping.
            sender: Producing module name.
            timeout: Seconds to wait for the reply. ``None`` means: fail
                immediately if no request handler is registered on the
                topic, otherwise wait indefinitely.

        Returns:
            The reply payload mapping.

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
            RuntimeError: If the bus is not running.
            RequestTimeoutError: If no reply arrives within ``timeout``.
            RequestHandlerError: If a request handler serving this request
                raises while processing it (the original exception is
                chained as ``__cause__``). Delivery to other subscribers on
                the topic is unaffected.
        """

    def subscribe(self, topic: str, handler: EventHandler) -> Callable[[], None]:
        """Register a fire-and-forget handler for a topic.

        Args:
            topic: Topic to subscribe to; must be in ``ALL_TOPICS``.
            handler: Async callable invoked with each published message.

        Returns:
            An unsubscribe function (idempotent).

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
        """

    def request_handler(self, topic: str, handler: RequestHandler) -> Callable[[], None]:
        """Register a handler that returns a reply payload for requests.

        When a message published via ``request()`` is routed to this
        handler, its returned mapping is wrapped into a reply ``Message``
        (with ``reply_to`` set to the request's ``message_id``) and
        delivered back to the originator.

        Args:
            topic: Topic to serve requests on; must be in ``ALL_TOPICS``.
            handler: Async callable returning the reply payload.

        Returns:
            An unsubscribe function (idempotent).

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
        """
