"""``asyncio.Queue``-backed implementation of the event bus protocol.

Architecture
------------
* Every subscription (from :meth:`subscribe` or
  :meth:`request_handler`) gets its own bounded ``asyncio.Queue`` and one
  dedicated dispatcher task that dequeues messages and awaits the handler.
* Multiple subscribers per topic are supported; there is no
  single-handler-per-topic restriction.

Guarantees and documented behavior
----------------------------------
* **Ordering**: delivery order within a single subscription equals
  publish order (FIFO). Across different subscriptions there is no
  ordering guarantee.
* **Backpressure**: when a subscriber queue is full, ``publish``/``request``
  block on ``queue.put`` (the publisher is slowed down by its slowest
  subscriber for that topic). The first time a queue crosses 80% of its
  capacity, a single ``warning`` is logged (not per message); the flag is
  reset once the queue drains below the threshold again.
* **Failure isolation**: handler exceptions are caught and logged at
  ``error`` level (with topic, sender, message_id, and the exception); a
  failing handler neither crashes its dispatcher nor affects other
  subscribers.
* **Request/response**: ``request()`` registers a future keyed by the
  request's ``message_id``; a request handler's returned mapping is
  wrapped into a reply ``Message`` (same topic, ``reply_to`` set to the
  request id) and resolves that future. Replies are delivered to the
  originator only — they are not re-dispatched to topic subscribers.
  Timeout semantics: with ``timeout`` set, ``RequestTimeoutError`` is
  raised when no reply arrives in time; with ``timeout=None`` the call
  fails immediately if no request handler is registered on the topic, and
  otherwise waits indefinitely.
  Exception policy: if a request handler raises while serving a
  ``request()``, the exception is logged at ``error`` level and the
  pending reply future is resolved with :class:`RequestHandlerError`
  (chaining the original exception) — the requester never hangs, not
  even with ``timeout=None``. Because a request fans out to all
  subscriptions on the topic and several request handlers may exist,
  resolution follows "first wins": the first handler to either reply or
  fail resolves the future; later replies are discarded and later
  failures only log. A failing request handler does not affect delivery
  to other subscribers, and its dispatcher keeps serving subsequent
  messages.
* **Lifecycle**: ``start()`` is idempotent; ``stop()`` without ``start()``
  is a no-op. ``stop()`` cancels dispatchers, drains queues, and fails
  pending reply futures with ``RequestTimeoutError``. Publishing while
  stopped raises ``RuntimeError``.
* **Thread-safety**: the bus is designed for a single event loop; the
  async API (``publish``/``request``/dispatcher loop) is not safe to call
  from other threads, and cross-thread publishes must be marshalled onto
  the bus's loop via ``loop.run_in_executor``/
  ``loop.call_soon_threadsafe`` (out of scope for this implementation).
  The subscription *registry*, however, is guarded by a ``threading.Lock``
  (see :attr:`AsyncQueueEventBus._subscriptions_lock`) so that a
  ``subscribe()``/``unsubscribe()`` interleaved with a routing pass can
  never corrupt iteration — even a mid-loop unsubscribe (possible,
  because routing awaits per-subscriber queue puts) operates on a
  snapshot and cannot raise ``RuntimeError: list changed size during
  iteration``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bond_accounting.event_bus.event_bus import (
    EventBus,
    RequestHandlerError,
    RequestTimeoutError,
)
from bond_accounting.event_bus.message import Message
from bond_accounting.event_bus.topics import ALL_TOPICS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from typing import Any

    from bond_accounting.config.settings import EventBusConfig
    from bond_accounting.event_bus.event_bus import EventHandler, RequestHandler

    _SubscriptionHandler = Callable[[Message], Awaitable[Mapping[str, Any] | None]]

logger = logging.getLogger(__name__)

#: Share of ``max_queue_size`` at which the one-shot high-watermark warning fires.
_HIGH_WATERMARK_RATIO = 0.8


@dataclass
class _Subscription:
    """Internal bookkeeping for one registered subscriber."""

    topic: str
    handler: _SubscriptionHandler
    queue: asyncio.Queue[Message]
    #: Whether the handler serves requests (its return value becomes a reply).
    is_request_handler: bool = False
    #: ``sender`` stamped on reply messages produced by this subscription.
    reply_sender: str = ""
    #: Dispatcher task; ``None`` while the bus is stopped.
    dispatcher: asyncio.Task[None] | None = None
    #: Whether the 80%-full warning has already been logged for the current fill episode.
    warned_high_watermark: bool = False


class AsyncQueueEventBus(EventBus):
    """In-process event bus backed by per-subscriber bounded ``asyncio.Queue``s.

    See the module docstring for the full list of guarantees (ordering,
    backpressure, failure isolation, request/response, lifecycle,
    thread-safety/single-loop usage). Subscription management is guarded by
    :attr:`_subscriptions_lock` using the snapshot-then-process pattern:
    the registry is only ever touched inside short, non-awaiting critical
    sections, and handlers/queue operations run outside the lock.
    """

    def __init__(self, config: EventBusConfig) -> None:
        """Create a stopped bus.

        Args:
            config: Bus configuration; ``max_queue_size`` bounds every
                subscriber queue (``<= 0`` means unbounded, in which case
                no high-watermark warnings are logged).
        """
        self._config = config
        self._subscriptions: dict[str, list[_Subscription]] = {}
        # Guards ``_subscriptions`` (and nothing else). A ``threading.Lock``
        # rather than ``asyncio.Lock``: ``subscribe()``/``unsubscribe()`` are
        # synchronous and must be callable without a running loop (e.g. at
        # wiring time). It is only ever held for short, non-awaiting critical
        # sections — snapshot/mutate the registry — so it cannot be held
        # across an ``await`` and can never deadlock the event loop.
        self._subscriptions_lock = threading.Lock()
        self._reply_futures: dict[str, asyncio.Future[Message]] = {}
        self._running = False
        if config.max_queue_size > 0:
            self._warn_threshold: int | None = max(
                1, int(config.max_queue_size * _HIGH_WATERMARK_RATIO)
            )
        else:
            self._warn_threshold = None

    @property
    def running(self) -> bool:
        """Whether the bus is currently started."""
        return self._running

    async def start(self) -> None:
        """Start the bus: create dispatcher tasks for all subscriptions.

        Idempotent: calling ``start()`` on a running bus is a no-op.
        """
        if self._running:
            logger.debug("Event bus start() ignored: already running")
            return
        self._running = True
        with self._subscriptions_lock:
            subscriptions = [
                subscription
                for subscribers in self._subscriptions.values()
                for subscription in subscribers
            ]
        started = 0
        for subscription in subscriptions:
            if self._ensure_dispatcher(subscription):
                started += 1
        logger.info("Event bus started: %d dispatcher task(s) spawned", started)

    async def stop(self) -> None:
        """Stop the bus: cancel dispatchers, drain queues, fail pending replies.

        No-op when the bus is not running. After ``stop()`` the bus can be
        ``start()``-ed again; existing subscriptions survive the restart.
        """
        if not self._running:
            logger.debug("Event bus stop() ignored: not running")
            return
        self._running = False
        with self._subscriptions_lock:
            dispatchers = [
                subscription.dispatcher
                for subscribers in self._subscriptions.values()
                for subscription in subscribers
                if subscription.dispatcher is not None
            ]
            subscriptions = [
                subscription
                for subscribers in self._subscriptions.values()
                for subscription in subscribers
            ]
        for task in dispatchers:
            task.cancel()
        if dispatchers:
            await asyncio.gather(*dispatchers, return_exceptions=True)
        dropped = 0
        for subscription in subscriptions:
            subscription.dispatcher = None
            dropped += self._drain(subscription.queue)
        for message_id, future in self._reply_futures.items():
            if not future.done():
                future.set_exception(
                    RequestTimeoutError(
                        message_id, reason="event bus stopped before a reply arrived"
                    )
                )
        self._reply_futures.clear()
        logger.info(
            "Event bus stopped: %d dispatcher task(s) cancelled, %d queued message(s) dropped",
            len(dispatchers),
            dropped,
        )

    async def publish(self, topic: str, payload: Mapping[str, Any], sender: str) -> str:
        """Publish a fire-and-forget event; returns the ``message_id``.

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
            RuntimeError: If the bus is not running.
        """
        self._validate_topic(topic)
        self._ensure_running("publish")
        message = Message(topic=topic, payload=payload, sender=sender)
        await self._route(message)
        logger.debug("Published message %s on topic %r from %r", message.message_id, topic, sender)
        return message.message_id

    async def request(
        self,
        topic: str,
        payload: Mapping[str, Any],
        sender: str,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Publish a message and await a reply; see the module docstring.

        Exception policy for failing request handlers: if a handler raises
        while serving this request, the pending reply future is resolved
        with :class:`RequestHandlerError` (the original exception chained as
        ``__cause__``) — the caller never hangs, including with
        ``timeout=None``. Resolution is "first wins": with several request
        handlers on the topic, the first reply or the first failure
        resolves the request; later outcomes are logged and discarded.

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
            RuntimeError: If the bus is not running.
            RequestTimeoutError: If no reply arrives within ``timeout``, or
                immediately when ``timeout is None`` and no request handler
                is registered on the topic.
            RequestHandlerError: If a request handler serving this request
                raises while processing it.
        """
        self._validate_topic(topic)
        self._ensure_running("request")
        message = Message(topic=topic, payload=payload, sender=sender)
        if timeout is None and not self._has_request_handler(topic):
            raise RequestTimeoutError(
                message.message_id,
                reason=f"no request handler registered for topic {topic!r} and no timeout given",
            )
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._reply_futures[message.message_id] = future
        try:
            await self._route(message)
            if timeout is None:
                reply = await future
            else:
                try:
                    reply = await asyncio.wait_for(future, timeout)
                except TimeoutError as exc:
                    raise RequestTimeoutError(
                        message.message_id,
                        reason=f"no reply for topic {topic!r} within {timeout}s",
                    ) from exc
            logger.debug("Request %s on topic %r got a reply", message.message_id, topic)
            return reply.payload
        finally:
            self._reply_futures.pop(message.message_id, None)

    def subscribe(self, topic: str, handler: EventHandler) -> Callable[[], None]:
        """Register a fire-and-forget handler; returns an unsubscribe function.

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
        """
        self._validate_topic(topic)
        return self._add_subscription(topic, handler, is_request_handler=False)

    def request_handler(self, topic: str, handler: RequestHandler) -> Callable[[], None]:
        """Register a request handler; returns an unsubscribe function.

        Reply messages are stamped with ``sender`` derived from the
        handler's ``__module__`` (falling back to ``"<topic>.request_handler"``).

        Raises:
            ValueError: If ``topic`` is not registered in ``ALL_TOPICS``.
        """
        self._validate_topic(topic)
        return self._add_subscription(topic, handler, is_request_handler=True)

    # ------------------------------------------------------------------ #
    # internals

    def _add_subscription(
        self,
        topic: str,
        handler: _SubscriptionHandler,
        *,
        is_request_handler: bool,
    ) -> Callable[[], None]:
        """Create a subscription (queue + optional dispatcher) and an unsubscribe closure."""
        subscription = _Subscription(
            topic=topic,
            handler=handler,
            queue=asyncio.Queue(self._config.max_queue_size),
            is_request_handler=is_request_handler,
            reply_sender=getattr(handler, "__module__", None) or f"{topic}.request_handler",
        )
        with self._subscriptions_lock:
            self._subscriptions.setdefault(topic, []).append(subscription)
            total = len(self._subscriptions[topic])
        if self._running:
            self._ensure_dispatcher(subscription)
        kind = "request handler" if is_request_handler else "subscriber"
        logger.debug("Registered %s on topic %r (%d total)", kind, topic, total)

        def unsubscribe() -> None:
            """Remove the subscription; idempotent, safe to call more than once."""
            self._remove_subscription(subscription)

        return unsubscribe

    def _remove_subscription(self, subscription: _Subscription) -> None:
        """Detach a subscription: cancel its dispatcher and drop queued messages.

        The registry update runs under the subscriptions lock; cancelling
        the dispatcher and draining the queue happen outside it (they do
        not touch the registry and may not be instantaneous).
        """
        with self._subscriptions_lock:
            subscribers = self._subscriptions.get(subscription.topic)
            if subscribers is None or subscription not in subscribers:
                return
            subscribers.remove(subscription)
            if not subscribers:
                del self._subscriptions[subscription.topic]
        if subscription.dispatcher is not None:
            subscription.dispatcher.cancel()
            subscription.dispatcher = None
        dropped = self._drain(subscription.queue)
        logger.debug(
            "Unsubscribed handler from topic %r (dropped %d queued message(s))",
            subscription.topic,
            dropped,
        )

    def _ensure_dispatcher(self, subscription: _Subscription) -> bool:
        """Spawn the dispatcher task for a subscription if it has none; returns whether it did."""
        if subscription.dispatcher is not None and not subscription.dispatcher.done():
            return False
        subscription.dispatcher = asyncio.get_running_loop().create_task(
            self._dispatch(subscription),
            name=f"event-bus-dispatcher:{subscription.topic}",
        )
        return True

    async def _dispatch(self, subscription: _Subscription) -> None:
        """Dequeue messages and await the handler, forever (until cancelled).

        Handler failures are logged and swallowed so that the dispatcher
        keeps serving subsequent messages and other subscribers are not
        affected. For request handlers, a failure additionally resolves
        the pending reply future with :class:`RequestHandlerError` (see
        :meth:`_fail_pending_reply`) so the requester cannot hang.
        """
        while True:
            message = await subscription.queue.get()
            if self._warn_threshold is None or subscription.queue.qsize() < self._warn_threshold:
                subscription.warned_high_watermark = False
            try:
                result = await subscription.handler(message)
            except Exception as exc:
                logger.error(
                    "Event handler failed: topic=%s sender=%s message_id=%s",
                    message.topic,
                    message.sender,
                    message.message_id,
                    exc_info=True,
                )
                if subscription.is_request_handler:
                    self._fail_pending_reply(message, exc)
                continue
            if subscription.is_request_handler:
                self._maybe_reply(subscription, message, result)

    def _fail_pending_reply(self, request: Message, exc: Exception) -> None:
        """Resolve the pending reply future of a request whose handler raised.

        Follows the "first resolution wins" policy of :meth:`_maybe_reply`:
        if the future is already resolved (another handler replied, the
        requester timed out, or the bus was stopped), the failure is only
        logged (already done by the caller) and the future is left alone.
        """
        future = self._reply_futures.get(request.message_id)
        if future is None or future.done():
            return
        error = RequestHandlerError(request.topic, request.message_id, exc)
        error.__cause__ = exc
        future.set_exception(error)

    def _maybe_reply(
        self,
        subscription: _Subscription,
        request: Message,
        result: Mapping[str, Any] | None,
    ) -> None:
        """Wrap a request handler's return value into a reply message, if applicable.

        A handler's result becomes a reply only when the message originates
        from ``request()`` — i.e. there is a pending future keyed by the
        message's ``message_id``. Fire-and-forget deliveries to request
        handlers simply discard the returned payload.
        """
        future = self._reply_futures.get(request.message_id)
        if future is None or future.done():
            if result is not None:
                logger.debug(
                    "Request handler on topic %r returned a payload for message %s which has no "
                    "pending requester; discarding",
                    request.topic,
                    request.message_id,
                )
            return
        if result is None:
            logger.debug(
                "Request handler on topic %r returned no reply for request %s",
                request.topic,
                request.message_id,
            )
            return
        reply = Message(
            topic=request.topic,
            payload=result,
            sender=subscription.reply_sender,
            reply_to=request.message_id,
        )
        future.set_result(reply)
        logger.debug(
            "Delivered reply %s for request %s on topic %r",
            reply.message_id,
            request.message_id,
            request.topic,
        )

    async def _route(self, message: Message) -> None:
        """Enqueue the message into every subscription on its topic, in registration order.

        Snapshot-then-process: the subscription list is copied under the
        subscriptions lock and the copy is iterated outside it, so a
        concurrent ``unsubscribe()`` (possible between the per-subscriber
        ``queue.put`` awaits) cannot raise "list changed size during
        iteration" or silently skip subscribers that were present when
        routing began. Enqueued messages of a subscription unsubscribed
        mid-route are dropped by its dispatcher's cancellation. Handler
        execution never happens under the lock — it happens in the
        dispatcher tasks, not here.

        Blocks (backpressure) while any subscriber queue is full.
        """
        with self._subscriptions_lock:
            snapshot = list(self._subscriptions.get(message.topic, ()))
        for subscription in snapshot:
            await subscription.queue.put(message)
            self._maybe_warn_high_watermark(subscription)

    def _maybe_warn_high_watermark(self, subscription: _Subscription) -> None:
        """Log a one-shot warning when a queue crosses 80% of its capacity."""
        if self._warn_threshold is None or subscription.warned_high_watermark:
            return
        queue_size = subscription.queue.qsize()
        if queue_size >= self._warn_threshold:
            subscription.warned_high_watermark = True
            logger.warning(
                "Subscriber queue for topic %r is at %d/%d entries (>=80%% full); "
                "publishers may block until the subscriber catches up",
                subscription.topic,
                queue_size,
                self._config.max_queue_size,
            )

    def _has_request_handler(self, topic: str) -> bool:
        """Whether at least one request handler is registered on the topic."""
        with self._subscriptions_lock:
            subscribers = self._subscriptions.get(topic, ())
            return any(subscription.is_request_handler for subscription in subscribers)

    def _ensure_running(self, operation: str) -> None:
        """Raise ``RuntimeError`` if the bus is not running."""
        if not self._running:
            raise RuntimeError(
                f"Cannot {operation}: event bus is not running (call start() first or stop() was called)"
            )

    @staticmethod
    def _validate_topic(topic: str) -> None:
        """Reject topics not registered in :data:`ALL_TOPICS`."""
        if topic not in ALL_TOPICS:
            raise ValueError(
                f"Unknown topic {topic!r}; known topics: {', '.join(sorted(ALL_TOPICS))}. "
                "Register new topics in bond_accounting.event_bus.topics.Topic."
            )

    @staticmethod
    def _drain(queue: asyncio.Queue[Message]) -> int:
        """Empty a queue, returning the number of dropped messages."""
        dropped = 0
        while not queue.empty():
            queue.get_nowait()
            dropped += 1
        return dropped
