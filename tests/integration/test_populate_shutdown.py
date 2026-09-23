"""Regression tests for the shutdown race between fire-and-forget populate
tasks and ``MoexClient.aclose`` (fix card fix-teardown-race).

Covers the two delivered remedies:

1. ``BondReferenceService`` tracks every task spawned by
   ``schedule_populate_coupons`` (discarded via ``add_done_callback``) and
   ``shutdown()`` cancels the outstanding ones and gathers with
   ``return_exceptions=True``, so the composition root can settle them
   deterministically before closing the shared HTTP client.
2. ``MoexClient.aclose`` swallows only the teardown ``RuntimeError`` from an
   already-closed transport (warning logged); any other error propagates.

The hanging-request test reproduces the race condition itself: a populate
task in flight over a transport that never answers, followed by shutdown —
previously the connection was torn down concurrently with ``aclose`` and
uvloop's ``write_eof`` raised on the dead transport.
"""

from __future__ import annotations

import asyncio
import logging
import typing

import httpx
import pytest

from bond_accounting.market_data import BondReferenceService, MoexClient
from tests.integration.test_populate_coupons import _ISIN, _coupon_rows, _make_client, _seed_bond

if typing.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE_URL = "https://iss.moex.com"


class _HangingTransport(httpx.AsyncBaseTransport):
    """Transport whose requests never complete until they are cancelled.

    Records the teardown order into ``events`` so the test can assert that
    the populate task was cancelled (and settled) before the client closed.
    """

    def __init__(self) -> None:
        self.request_started = asyncio.Event()
        self.events: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.events.append("request-started")
        self.request_started.set()
        try:
            await asyncio.Event().wait()  # never completes
        except asyncio.CancelledError:
            self.events.append("populate-cancelled")
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        self.events.append("client-aclosed")


class _RaisingTransport(httpx.AsyncBaseTransport):
    """Transport whose close raises the given exception (never serves requests)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")  # pragma: no cover

    async def aclose(self) -> None:
        raise self._error


def _client_with_transport(transport: httpx.AsyncBaseTransport) -> MoexClient:
    """Build a ``MoexClient`` over an injected transport (same pattern as _make_client)."""
    client = MoexClient(base_url=BASE_URL, timeout_s=5.0)
    client._http = httpx.AsyncClient(base_url=BASE_URL, timeout=5.0, transport=transport)
    return client


# --------------------------------------------------------------------------- #
# Task tracking (registry fills on spawn, empties on completion)
# --------------------------------------------------------------------------- #


async def test_schedule_populate_coupons_tracks_task_and_discards_on_completion(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The spawned task is tracked and leaves the registry once it completes."""
    await _seed_bond(session_factory)

    client, injected = _make_client(coupon_data=_coupon_rows(2))
    service = BondReferenceService(client, session_factory=session_factory)
    try:
        assert service._populate_tasks == set()
        task = service.schedule_populate_coupons(_ISIN)
        assert service._populate_tasks == {task}
        await task
        # The discard callback runs on the next loop iteration after completion.
        await asyncio.sleep(0)
        assert service._populate_tasks == set()
    finally:
        await injected.aclose()


# --------------------------------------------------------------------------- #
# shutdown(): cancel outstanding tasks, settle before aclose, no escapes
# --------------------------------------------------------------------------- #


async def test_shutdown_cancels_outstanding_task_and_settles_before_aclose(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An in-flight populate task is cancelled and awaited before aclose runs.

    The task's request hangs on a transport that never answers (the race
    precondition); shutdown() must cancel it, swallow the cancellation via
    gather(return_exceptions=True) and leave the registry empty so the
    subsequent MoexClient.aclose() cannot race a live connection.
    """
    await _seed_bond(session_factory)

    transport = _HangingTransport()
    client = _client_with_transport(transport)
    service = BondReferenceService(client, session_factory=session_factory)
    task = service.schedule_populate_coupons(_ISIN)

    await asyncio.wait_for(transport.request_started.wait(), timeout=5.0)
    assert not task.done()

    await service.shutdown()  # must not raise

    assert task.done()
    assert task.cancelled()
    await asyncio.sleep(0)
    assert service._populate_tasks == set()

    # aclose() is only safe now, after the task settled.
    await client.aclose()
    assert transport.events == ["request-started", "populate-cancelled", "client-aclosed"]


async def test_shutdown_with_completed_tasks_awaits_them(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """shutdown() gathers even when the tracked tasks already finished normally."""
    await _seed_bond(session_factory)

    client, injected = _make_client(coupon_data=_coupon_rows(2))
    service = BondReferenceService(client, session_factory=session_factory)
    try:
        task = service.schedule_populate_coupons(_ISIN)
        await task
        await service.shutdown()  # no-op settle; must not raise or cancel anything
    finally:
        await injected.aclose()

    assert task.done()
    assert not task.cancelled()


async def test_shutdown_without_tasks_is_a_noop() -> None:
    """shutdown() on a fresh service (no tasks ever spawned) does nothing."""
    client, injected = _make_client()
    service = BondReferenceService(client)
    try:
        await service.shutdown()  # must not raise
    finally:
        await injected.aclose()
    assert service._populate_tasks == set()


# --------------------------------------------------------------------------- #
# MoexClient.aclose defensive guard
# --------------------------------------------------------------------------- #


async def test_aclose_swallows_runtime_error_from_closed_transport(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A teardown RuntimeError (uvloop write_eof on a dead transport) is swallowed
    with a warning instead of propagating out of shutdown."""
    client = _client_with_transport(
        _RaisingTransport(
            RuntimeError(
                "unable to perform operation on <TCPTransport closed=True>; the handler is closed"
            )
        )
    )
    with caplog.at_level(logging.WARNING, logger="bond_accounting.market_data.client"):
        await client.aclose()  # must not raise

    assert any("already-closed transport" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_aclose_propagates_other_errors() -> None:
    """Only RuntimeError is guarded; any other close error still propagates."""
    client = _client_with_transport(_RaisingTransport(ValueError("boom")))
    with pytest.raises(ValueError, match="boom"):
        await client.aclose()
