"""Tests for the in-process event bus: protocol, AsyncQueueEventBus, request/response."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import pytest

from bond_accounting.config.settings import EventBusConfig
from bond_accounting.event_bus import (
    ALL_TOPICS,
    AsyncQueueEventBus,
    Message,
    RequestTimeoutError,
    Topic,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    BusFactory = Callable[..., Awaitable[AsyncQueueEventBus]]


async def _wait_until(
    predicate: Callable[[], bool], timeout: float = 2.0, what: str = "condition"
) -> None:
    """Poll ``predicate`` until it is true or fail the test after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for {what}")
        await asyncio.sleep(0.005)


@pytest.fixture
async def make_bus() -> AsyncIterator[BusFactory]:
    """Factory creating tracked buses; every created bus is stopped after the test."""
    buses: list[AsyncQueueEventBus] = []

    async def _make(
        config: EventBusConfig | None = None, *, start: bool = True
    ) -> AsyncQueueEventBus:
        bus = AsyncQueueEventBus(config or EventBusConfig())
        buses.append(bus)
        if start:
            await bus.start()
        return bus

    yield _make
    for bus in buses:
        if bus.running:
            await bus.stop()


# --------------------------------------------------------------------- #
# Message and topic registry


def test_message_requires_topic_and_sender() -> None:
    with pytest.raises(ValueError, match="topic"):
        Message(topic="", payload={"a": 1}, sender="bonds")
    with pytest.raises(ValueError, match="sender"):
        Message(topic=Topic.BOND_CREATED, payload={"a": 1}, sender="")
    message = Message(topic=Topic.BOND_CREATED, payload={"a": 1}, sender="bonds")
    assert message.message_id
    assert message.reply_to is None
    assert message.timestamp.tzinfo is not None


def test_all_topics_registry() -> None:
    assert len(ALL_TOPICS) == 12
    for name in (
        Topic.BOND_CREATED,
        Topic.BOND_UPDATED,
        Topic.BOND_DELETED,
        Topic.BROKER_CREATED,
        Topic.BROKER_UPDATED,
        Topic.BROKER_DELETED,
        Topic.BROKER_ACCOUNT_CREATED,
        Topic.BROKER_ACCOUNT_UPDATED,
        Topic.BROKER_ACCOUNT_DELETED,
        Topic.TRANSACTION_CREATED,
        Topic.POSITION_UPDATED,
        Topic.PORTFOLIO_RECALCULATED,
    ):
        assert name in ALL_TOPICS


# --------------------------------------------------------------------- #
# Validation


async def test_unknown_topic_rejected(make_bus: BusFactory) -> None:
    bus = await make_bus()

    async def event_handler(message: Message) -> None:
        pass

    async def reply_handler(message: Message) -> dict[str, int]:
        return {"ok": 1}

    with pytest.raises(ValueError, match="Unknown topic"):
        await bus.publish("nope.topic", {}, "tests")
    with pytest.raises(ValueError, match="Unknown topic"):
        bus.subscribe("nope.topic", event_handler)
    with pytest.raises(ValueError, match="Unknown topic"):
        bus.request_handler("nope.topic", reply_handler)
    with pytest.raises(ValueError, match="Unknown topic"):
        await bus.request("nope.topic", {}, "tests", timeout=0.05)


# --------------------------------------------------------------------- #
# Pub/sub


async def test_publish_delivers_to_subscriber(make_bus: BusFactory) -> None:
    bus = await make_bus(start=False)
    received: list[Message] = []

    async def handler(message: Message) -> None:
        received.append(message)

    unsubscribe = bus.subscribe(Topic.TRANSACTION_CREATED, handler)
    await bus.start()  # subscriptions made before start get dispatchers at start
    message_id = await bus.publish(Topic.TRANSACTION_CREATED, {"isin": "X"}, "tests")

    await _wait_until(lambda: len(received) == 1, what="message delivery")
    assert received[0].topic == Topic.TRANSACTION_CREATED
    assert received[0].payload == {"isin": "X"}
    assert received[0].sender == "tests"
    assert received[0].message_id == message_id
    unsubscribe()


async def test_multiple_subscribers_and_fifo_order(make_bus: BusFactory) -> None:
    bus = await make_bus()
    first: list[int] = []
    second: list[int] = []

    async def handler_first(message: Message) -> None:
        await asyncio.sleep(0)
        first.append(message.payload["n"])

    async def handler_second(message: Message) -> None:
        second.append(message.payload["n"])

    bus.subscribe(Topic.BOND_UPDATED, handler_first)
    bus.subscribe(Topic.BOND_UPDATED, handler_second)
    for n in range(5):
        await bus.publish(Topic.BOND_UPDATED, {"n": n}, "tests")

    await _wait_until(lambda: len(first) == 5 and len(second) == 5, what="both subscribers")
    assert first == [0, 1, 2, 3, 4]
    assert second == [0, 1, 2, 3, 4]


async def test_unsubscribe_stops_delivery(make_bus: BusFactory) -> None:
    bus = await make_bus()
    received: list[Message] = []

    async def handler(message: Message) -> None:
        received.append(message)

    unsubscribe = bus.subscribe(Topic.BOND_DELETED, handler)
    await bus.publish(Topic.BOND_DELETED, {"n": 1}, "tests")
    await _wait_until(lambda: len(received) == 1, what="first delivery")

    unsubscribe()
    unsubscribe()  # idempotent
    await bus.publish(Topic.BOND_DELETED, {"n": 2}, "tests")
    await asyncio.sleep(0.05)
    assert len(received) == 1


# --------------------------------------------------------------------- #
# Failure isolation


async def test_handler_exception_is_contained(
    make_bus: BusFactory, caplog: pytest.LogCaptureFixture
) -> None:
    bus = await make_bus()
    good: list[Message] = []

    async def bad_handler(message: Message) -> None:
        raise RuntimeError("boom")

    async def good_handler(message: Message) -> None:
        good.append(message)

    bus.subscribe(Topic.POSITION_UPDATED, bad_handler)
    bus.subscribe(Topic.POSITION_UPDATED, good_handler)

    with caplog.at_level(logging.ERROR, logger="bond_accounting.event_bus.async_queue_bus"):
        await bus.publish(Topic.POSITION_UPDATED, {"n": 1}, "tests")
        await _wait_until(lambda: len(good) == 1, what="delivery to healthy subscriber")

    # The other subscriber received the message despite the failing handler.
    assert len(good) == 1
    # The failure was logged at error level with topic and message context.
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert errors
    assert Topic.POSITION_UPDATED in caplog.text

    # The dispatcher survived: subsequent messages still get through.
    await bus.publish(Topic.POSITION_UPDATED, {"n": 2}, "tests")
    await _wait_until(lambda: len(good) == 2, what="delivery after handler failure")
    assert bus.running


# --------------------------------------------------------------------- #
# Request/response


async def test_request_response(make_bus: BusFactory) -> None:
    bus = await make_bus()
    seen: list[Message] = []

    async def echo(message: Message) -> dict[str, int]:
        seen.append(message)
        return {"echo": message.payload["value"]}

    unsubscribe = bus.request_handler(Topic.BOND_CREATED, echo)
    reply = await bus.request(Topic.BOND_CREATED, {"value": 42}, "tests", timeout=1.0)

    assert reply == {"echo": 42}
    assert len(seen) == 1
    assert seen[0].reply_to is None  # the request itself is not a reply
    unsubscribe()


async def test_request_without_handler_times_out(make_bus: BusFactory) -> None:
    bus = await make_bus()

    with pytest.raises(RequestTimeoutError) as exc_info:
        await bus.request(Topic.PORTFOLIO_RECALCULATED, {}, "tests", timeout=0.05)
    assert exc_info.value.message_id

    # timeout=None with no handler registered: fail fast instead of hanging.
    with pytest.raises(RequestTimeoutError, match="no request handler registered"):
        await bus.request(Topic.PORTFOLIO_RECALCULATED, {}, "tests")


# --------------------------------------------------------------------- #
# Lifecycle


async def test_start_stop_idempotency() -> None:
    bus = AsyncQueueEventBus(EventBusConfig())

    await bus.stop()  # no-op without start
    assert not bus.running

    await bus.start()
    assert bus.running
    await bus.start()  # idempotent
    assert bus.running

    await bus.stop()
    assert not bus.running

    with pytest.raises(RuntimeError, match="not running"):
        await bus.publish(Topic.BOND_CREATED, {}, "tests")
    await bus.stop()  # still a no-op


# --------------------------------------------------------------------- #
# Backpressure


async def test_backpressure_slow_subscriber(make_bus: BusFactory) -> None:
    bus = await make_bus(EventBusConfig(max_queue_size=1))
    received: list[int] = []

    async def slow_handler(message: Message) -> None:
        await asyncio.sleep(0.01)
        received.append(message.payload["n"])

    bus.subscribe(Topic.BOND_DELETED, slow_handler)

    async def publish_five() -> None:
        for n in range(5):
            await bus.publish(Topic.BOND_DELETED, {"n": n}, "tests")

    publisher = asyncio.create_task(publish_five())
    await _wait_until(lambda: len(received) == 5, timeout=5.0, what="all backpressured messages")
    await publisher
    assert received == [0, 1, 2, 3, 4]
    assert bus.running


async def test_backpressure_warning_logged_once(
    make_bus: BusFactory, caplog: pytest.LogCaptureFixture
) -> None:
    bus = await make_bus(EventBusConfig(max_queue_size=5))
    received: list[int] = []

    async def slow_handler(message: Message) -> None:
        await asyncio.sleep(0.05)
        received.append(message.payload["n"])

    bus.subscribe(Topic.BOND_DELETED, slow_handler)

    with caplog.at_level(logging.WARNING, logger="bond_accounting.event_bus.async_queue_bus"):
        for n in range(5):
            await bus.publish(Topic.BOND_DELETED, {"n": n}, "tests")

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    await _wait_until(lambda: len(received) == 5, timeout=5.0, what="drainage of full queue")
