"""Tests for the logging setup driven by ``LoggingConfig``."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from bond_accounting.config.logging_setup import _TextFormatter, setup_logging
from bond_accounting.config.settings import LoggingConfig, load_settings

if TYPE_CHECKING:
    from collections.abc import Generator


def _managed_handlers() -> list[logging.Handler]:
    """Return root handlers installed by ``setup_logging`` (marked ones)."""
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_bond_accounting_managed", False)
    ]


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Generator:
    """Snapshot/restore root logger handlers and level around each test."""
    root = logging.getLogger()
    before_handlers = root.handlers[:]
    before_level = root.level
    yield
    root.handlers[:] = before_handlers
    root.setLevel(before_level)


def test_json_format_output(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON output is valid JSON with standard fields and extras."""
    setup_logging(LoggingConfig(level="INFO", format="json"))

    logger = logging.getLogger("bond.test.json")
    logger.info("hello world", extra={"request_id": "abc-123"})
    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])

    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert record["name"] == "bond.test.json"
    assert record["timestamp"]
    # Extras from log calls are included.
    assert record["request_id"] == "abc-123"
    # Standard log-record fields beyond the basics.
    assert record["module"]
    assert isinstance(record["lineno"], int)
    assert record["process"]
    assert record["thread"]


def test_text_format_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Text output is human-readable with level, name, message and extras."""
    setup_logging(LoggingConfig(level="INFO", format="text"))

    logger = logging.getLogger("bond.test.text")
    logger.warning("disk almost full", extra={"disk_percent": 91})
    line = capsys.readouterr().out.strip().splitlines()[-1]

    assert "WARNING" in line
    assert "bond.test.text" in line
    assert "disk almost full" in line
    assert "disk_percent=91" in line
    # Timestamp with the documented ``YYYY-MM-DD HH:MM:SS`` shape.
    parts = line.split(" | ")
    assert len(parts[0]) == 19
    assert parts[0][4] == "-"
    assert parts[0][7] == "-"


def test_text_format_no_extras(capsys: pytest.CaptureFixture[str]) -> None:
    """Text output without extras has no trailing separator."""
    setup_logging(LoggingConfig(level="INFO", format="text"))

    logging.getLogger("bond.test.plain").info("plain message")
    line = capsys.readouterr().out.strip().splitlines()[-1]

    assert line.endswith("| plain message")
    assert line.count("|") == 3


def test_setup_logging_idempotent() -> None:
    """Calling setup_logging twice does not duplicate root handlers."""
    root = logging.getLogger()

    setup_logging(LoggingConfig(format="json"))
    handlers_after_first = list(root.handlers)

    setup_logging(LoggingConfig(format="json"))
    handlers_after_second = list(root.handlers)

    # No handler was added or duplicated: only our managed handler was replaced.
    assert len(handlers_after_second) == len(handlers_after_first)
    managed = _managed_handlers()
    assert len(managed) == 1


def test_setup_logging_reconfigures_format(capsys: pytest.CaptureFixture[str]) -> None:
    """A second call with a different format replaces the managed handler."""
    setup_logging(LoggingConfig(format="json"))
    setup_logging(LoggingConfig(format="text"))

    managed = _managed_handlers()
    assert len(managed) == 1
    assert isinstance(managed[0].formatter, _TextFormatter)

    logging.getLogger("bond.test.switch").info("after switch")
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1].count("|") >= 3


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    """DEBUG records are suppressed when the level is INFO."""
    setup_logging(LoggingConfig(level="INFO", format="text"))

    logger = logging.getLogger("bond.test.level")
    capsys.readouterr()  # drain the "Logging configured" line
    logger.debug("hidden debug")
    assert capsys.readouterr().out == ""

    logger.info("visible info")
    out = capsys.readouterr().out
    assert "visible info" in out


def test_level_applied_to_root() -> None:
    """The configured level is applied to the root logger."""
    setup_logging(LoggingConfig(level="WARNING"))
    assert logging.getLogger().level == logging.WARNING


def test_env_override_format(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """BOND_LOGGING__FORMAT overrides the YAML/defaults value."""
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", "env-secret")
    monkeypatch.setenv("BOND_LOGGING__FORMAT", "text")

    settings = load_settings(None)

    assert settings.logging.format == "text"
    assert settings.logging.level == "INFO"


def test_env_override_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOND_LOGGING__LEVEL overrides the default."""
    monkeypatch.setenv("BOND_AUTH__JWT_SECRET", "env-secret")
    monkeypatch.setenv("BOND_LOGGING__LEVEL", "DEBUG")

    settings = load_settings(None)

    assert settings.logging.level == "DEBUG"
