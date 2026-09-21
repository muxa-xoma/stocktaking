"""Logging setup driven by :class:`~bond_accounting.config.settings.LoggingConfig`.

Two output formats are supported on the root logger, both writing to stdout:

* ``json`` — machine-readable JSON via ``python-json-logger``'s msgspec backend
  (:class:`pythonjsonlogger.msgspec.MsgspecFormatter`). Includes the standard
  fields (timestamp, level, name, message, module, funcName, lineno, process,
  thread, ...) plus any ``extra=`` kwargs passed to log calls.
* ``text`` — human-readable lines such as
  ``2026-09-20 12:00:00 | INFO | bond_accounting.bonds | message | key=value``.

:func:`setup_logging` is idempotent: repeated calls replace previously
installed (managed) handlers instead of duplicating them.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.msgspec import MsgspecFormatter

if TYPE_CHECKING:
    from bond_accounting.config.settings import LoggingConfig

#: Attribute names emitted by the JSON formatter (comma-style ``fmt``).
#: Log-record extras (``extra={...}``) are added on top automatically.
_JSON_FIELDS = (
    "message",
    "levelname",
    "name",
    "module",
    "funcName",
    "lineno",
    "process",
    "processName",
    "thread",
    "threadName",
)

#: Rename ``levelname`` to the more common JSON key ``level``.
_JSON_RENAME_FIELDS = {"levelname": "level"}

_JSON_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"
_TEXT_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Marker attribute on handlers installed by :func:`setup_logging`; used to
#: remove them (and only them) on subsequent calls.
_MANAGED_ATTR = "_bond_accounting_managed"


class _TextFormatter(logging.Formatter):
    """Human-readable formatter that appends ``key=value`` extras.

    Produces lines like::

        2026-09-20 12:00:00 | INFO | bond_accounting.bonds | message | key=value
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render the record, appending non-standard extras as ``key=value``."""
        base = super().format(record)
        extras = {key: value for key, value in record.__dict__.items() if key not in RESERVED_ATTRS}
        if not extras:
            return base
        suffix = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        return f"{base} | {suffix}"


def _make_formatter(config: LoggingConfig) -> logging.Formatter:
    """Build the formatter matching ``config.format``."""
    if config.format == "json":
        return MsgspecFormatter(
            fmt=",".join(_JSON_FIELDS),
            style=",",
            datefmt=_JSON_DATEFMT,
            rename_fields=_JSON_RENAME_FIELDS,
            timestamp=True,
        )
    return _TextFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt=_TEXT_DATEFMT,
    )


def setup_logging(config: LoggingConfig) -> None:
    """Configure the root logger per the config (json or text).

    Installs a single ``logging.StreamHandler`` on stdout with the configured
    format and level, removing any handlers previously installed by this
    function (so repeated calls never duplicate handlers). The ``logging``
    module's ``lastResort`` fallback handler is aligned with the configured
    level as well.

    Args:
        config: Logging settings (level, format).
    """
    root = logging.getLogger()

    for handler in root.handlers[:]:
        if getattr(handler, _MANAGED_ATTR, False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(config.level)

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _MANAGED_ATTR, True)
    handler.setFormatter(_make_formatter(config))
    root.addHandler(handler)

    # lastResort only fires when no handlers exist at all; keep its threshold
    # consistent with the configured level.
    if logging.lastResort is not None:
        logging.lastResort.setLevel(config.level)

    logging.getLogger(__name__).info(
        "Logging configured: level=%s format=%s", config.level, config.format
    )
