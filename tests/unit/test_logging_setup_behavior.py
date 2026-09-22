"""Tests for the new ``setup_logging`` / ``reset_third_party_loggers`` behavior.

Complements ``tests/unit/test_logging_setup.py`` (which covers JSON/text
output, level filtering and basic root-handler idempotency) with the T02
additions:

* forced reset of every third-party logger (:data:`_THIRD_PARTY_LOGGERS`):
  handlers removed, level reset to ``NOTSET``, ``propagate`` re-enabled —
  both via the public :func:`reset_third_party_loggers` and as a side
  effect of :func:`setup_logging`;
* idempotency of :func:`setup_logging` and of the one-time PyJWT-warning
  capture: no duplicated root handler, no duplicated ``warnings`` filter,
  no re-wrapped ``warnings.showwarning``;
* PyJWT's ``InsecureKeyLengthWarning`` routed to the
  ``bond_accounting.pyjwt_warnings`` logger at WARNING level, while any
  other warning is delegated to the previous ``showwarning`` untouched;
* foreign (non-managed) handlers on unrelated loggers — and on the root
  logger — are never removed.
"""

from __future__ import annotations

import json
import logging
import re
import warnings
from typing import TYPE_CHECKING

import pytest
from jwt.warnings import InsecureKeyLengthWarning
from pythonjsonlogger.msgspec import MsgspecFormatter

from bond_accounting.config import logging_setup
from bond_accounting.config.logging_setup import (
    _THIRD_PARTY_LOGGERS,
    reset_third_party_loggers,
    setup_logging,
)
from bond_accounting.config.settings import LoggingConfig

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Generator:
    """Snapshot/restore root logger handlers and level around each test."""
    root = logging.getLogger()
    before_handlers = root.handlers[:]
    before_level = root.level
    yield
    root.handlers[:] = before_handlers
    root.setLevel(before_level)


@pytest.fixture(autouse=True)
def _restore_third_party_loggers() -> Generator:
    """Snapshot/restore third-party logger state around each test."""
    saved: dict[str, tuple[list[logging.Handler], int, bool]] = {}
    for name in _THIRD_PARTY_LOGGERS:
        third_party_logger = logging.getLogger(name)
        saved[name] = (
            third_party_logger.handlers[:],
            third_party_logger.level,
            third_party_logger.propagate,
        )
    yield
    for name, (handlers, level, propagate) in saved.items():
        third_party_logger = logging.getLogger(name)
        third_party_logger.handlers[:] = handlers
        third_party_logger.setLevel(level)
        third_party_logger.propagate = propagate


@pytest.fixture
def fresh_pyjwt_capture(monkeypatch: pytest.MonkeyPatch) -> Generator:
    """Force a fresh one-time install of the PyJWT-warning capture.

    ``_install_pyjwt_warning_capture`` installs the ``warnings`` filter and
    the ``showwarning`` wrapper only once per process; resetting the module
    flag (and restoring the ``warnings`` module state afterwards) makes the
    idempotency assertions deterministic regardless of test order.
    """
    monkeypatch.setattr(logging_setup, "_pyjwt_capture_installed", False)
    monkeypatch.setattr(logging_setup, "_prev_showwarning", None)
    saved_showwarning = warnings.showwarning
    saved_filters = warnings.filters[:]
    yield
    warnings.showwarning = saved_showwarning
    warnings.filters = saved_filters


def _managed_handlers() -> list[logging.Handler]:
    """Return root handlers installed by ``setup_logging`` (marked ones)."""
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, logging_setup._MANAGED_ATTR, False)
    ]


def _insecure_key_length_filters() -> int:
    """Count ``warnings`` filters targeting ``InsecureKeyLengthWarning``."""
    return sum(1 for item in warnings.filters if item[2] is InsecureKeyLengthWarning)


def _warn_always(message: Warning) -> None:
    """Emit a warning bypassing dedup filters ("once"/"default") of the run.

    Ensures the warning is actually displayed, i.e. routed through the
    currently installed ``warnings.showwarning``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(message, stacklevel=2)


# --------------------------------------------------------------------------- #
# Third-party logger reset
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("logger_name", _THIRD_PARTY_LOGGERS)
def test_reset_third_party_logger(logger_name: str) -> None:
    """reset_third_party_loggers strips handlers/level/propagate per logger."""
    third_party_logger = logging.getLogger(logger_name)
    third_party_logger.addHandler(logging.StreamHandler())
    third_party_logger.setLevel(logging.WARNING)
    third_party_logger.propagate = False

    reset_third_party_loggers()

    assert third_party_logger.handlers == []
    assert third_party_logger.level == logging.NOTSET
    assert third_party_logger.propagate


def test_reset_third_party_loggers_idempotent() -> None:
    """A second call on already-reset loggers keeps them clean."""
    for name in _THIRD_PARTY_LOGGERS:
        third_party_logger = logging.getLogger(name)
        third_party_logger.addHandler(logging.StreamHandler())
        third_party_logger.setLevel(logging.DEBUG)
        third_party_logger.propagate = False

    reset_third_party_loggers()
    reset_third_party_loggers()

    for name in _THIRD_PARTY_LOGGERS:
        third_party_logger = logging.getLogger(name)
        assert third_party_logger.handlers == []
        assert third_party_logger.level == logging.NOTSET
        assert third_party_logger.propagate is True


def test_setup_logging_resets_third_party_loggers() -> None:
    """setup_logging itself resets third-party loggers (uvicorn-style setup)."""
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.addHandler(logging.StreamHandler())
    uvicorn_access.setLevel(logging.INFO)
    uvicorn_access.propagate = False

    setup_logging(LoggingConfig())

    assert uvicorn_access.handlers == []
    assert uvicorn_access.level == logging.NOTSET
    assert uvicorn_access.propagate


# --------------------------------------------------------------------------- #
# Idempotency of setup_logging and of the PyJWT-warning capture
# --------------------------------------------------------------------------- #


def test_setup_logging_no_duplicate_managed_handler(fresh_pyjwt_capture: Generator) -> None:
    """Two calls leave exactly one managed root handler, no re-wrapped capture."""
    setup_logging(LoggingConfig(format="json"))
    showwarning_after_first = warnings.showwarning
    filters_after_first = _insecure_key_length_filters()

    setup_logging(LoggingConfig(format="json"))

    assert len(_managed_handlers()) == 1
    # The capture is installed exactly once: same wrapper function, no
    # additional InsecureKeyLengthWarning filter.
    assert warnings.showwarning is showwarning_after_first
    assert _insecure_key_length_filters() == filters_after_first == 1


# --------------------------------------------------------------------------- #
# Output formats
# --------------------------------------------------------------------------- #


def test_json_formatter_on_root_handler(fresh_pyjwt_capture: Generator) -> None:
    """The managed root handler formats with the msgspec JSON formatter."""
    setup_logging(LoggingConfig(format="json"))

    managed = _managed_handlers()
    assert len(managed) == 1
    assert isinstance(managed[0].formatter, MsgspecFormatter)


def test_json_record_shape(
    fresh_pyjwt_capture: Generator, capsys: pytest.CaptureFixture[str]
) -> None:
    """A JSON record is valid JSON with message/level/name/timestamp keys."""
    setup_logging(LoggingConfig(level="INFO", format="json"))
    capsys.readouterr()  # drain the "Logging configured" line

    logging.getLogger("bond.test.jsonshape").warning("shape check")

    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {"message", "level", "name", "timestamp"} <= set(record)
    assert record["message"] == "shape check"
    assert record["level"] == "WARNING"
    assert record["name"] == "bond.test.jsonshape"


def test_text_line_shape(
    fresh_pyjwt_capture: Generator, capsys: pytest.CaptureFixture[str]
) -> None:
    """A text record matches ``<date> | <LEVEL> | <name> | <message>``."""
    setup_logging(LoggingConfig(level="INFO", format="text"))
    capsys.readouterr()  # drain the "Logging configured" line

    logging.getLogger("bond.test.textshape").warning("shape check")

    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| WARNING \| bond\.test\.textshape \| shape check",
        line,
    )


# --------------------------------------------------------------------------- #
# PyJWT InsecureKeyLengthWarning capture
# --------------------------------------------------------------------------- #


def test_pyjwt_warning_routed_to_logger(
    fresh_pyjwt_capture: Generator, caplog: pytest.LogCaptureFixture
) -> None:
    """InsecureKeyLengthWarning lands in bond_accounting.pyjwt_warnings at WARNING."""
    setup_logging(LoggingConfig(level="INFO", format="text"))

    with caplog.at_level(logging.WARNING, logger="bond_accounting.pyjwt_warnings"):
        _warn_always(InsecureKeyLengthWarning("emulated weak key"))

    captured = [
        record for record in caplog.records if record.name == "bond_accounting.pyjwt_warnings"
    ]
    assert len(captured) == 1
    assert captured[0].levelname == "WARNING"
    assert "InsecureKeyLengthWarning" in captured[0].getMessage()
    assert "emulated weak key" in captured[0].getMessage()


def test_other_warning_not_captured(
    fresh_pyjwt_capture: Generator,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-PyJWT warning is delegated to the previous showwarning, not logged."""
    setup_logging(LoggingConfig(level="INFO", format="text"))

    delegated: list[tuple[object, type[Warning]]] = []

    def spy(
        message: object,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: object = None,
        line: str | None = None,
    ) -> None:
        delegated.append((message, category))

    # The wrapper resolves _prev_showwarning at call time, so a spy installed
    # after setup_logging observes every delegated (non-PyJWT) warning.
    monkeypatch.setattr(logging_setup, "_prev_showwarning", spy)

    with caplog.at_level(logging.WARNING, logger="bond_accounting.pyjwt_warnings"):
        _warn_always(UserWarning("unrelated warning"))

    assert not [
        record for record in caplog.records if record.name == "bond_accounting.pyjwt_warnings"
    ]
    assert len(delegated) == 1
    assert delegated[0][1] is UserWarning


# --------------------------------------------------------------------------- #
# Foreign (non-managed) handlers are preserved
# --------------------------------------------------------------------------- #


def test_foreign_handler_on_unrelated_logger_preserved(fresh_pyjwt_capture: Generator) -> None:
    """setup_logging never touches handlers of unlisted loggers."""
    unrelated = logging.getLogger("some.unrelated.library")
    foreign = logging.StreamHandler()
    unrelated.addHandler(foreign)
    unrelated.setLevel(logging.DEBUG)

    setup_logging(LoggingConfig())

    assert foreign in unrelated.handlers
    assert unrelated.level == logging.DEBUG

    unrelated.removeHandler(foreign)


def test_foreign_handler_on_root_preserved(fresh_pyjwt_capture: Generator) -> None:
    """Only managed root handlers are replaced; foreign ones stay."""
    root = logging.getLogger()
    foreign = logging.StreamHandler()
    root.addHandler(foreign)

    setup_logging(LoggingConfig())

    assert foreign in root.handlers
    assert len(_managed_handlers()) == 1
