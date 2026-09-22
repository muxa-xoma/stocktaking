"""Logging setup driven by :class:`~bond_accounting.config.settings.LoggingConfig`.

Two output formats are supported on the root logger, both writing to stdout:

* ``json`` — machine-readable JSON via ``python-json-logger``'s msgspec backend
  (:class:`pythonjsonlogger.msgspec.MsgspecFormatter`). Includes the standard
  fields (timestamp, level, name, message, module, funcName, lineno, process,
  thread, ...) plus any ``extra=`` kwargs passed to log calls.
* ``text`` — human-readable lines such as
  ``2026-09-20 12:00:00 | INFO | bond_accounting.bonds | message | key=value``.

Third-party loggers (:data:`_THIRD_PARTY_LOGGERS` — uvicorn server/access,
nicegui, sqlalchemy, alembic, fastapi) are forcibly reset on every call so
their records flow through the single root handler in the configured format
and level. ``reset_third_party_loggers`` is also public for re-invocation at
runtime (e.g. after some library installs its own handlers).

Interaction with uvicorn's logging setup: uvicorn's ``Config`` normally runs
``dictConfig(LOGGING_CONFIG)`` in its ``__init__``, attaching its own handlers
to ``uvicorn`` / ``uvicorn.access`` with ``propagate=False``. The application
counters this by passing ``log_config=None`` to ``ui.run`` (a stock
``uvicorn.Config`` parameter forwarded via ``**kwargs``): uvicorn then skips
its dictConfig entirely and only applies ``log_level`` to the ``uvicorn.*``
loggers, so their records keep propagating to the root handler installed
here.

PyJWT's ``InsecureKeyLengthWarning`` is emitted via ``warnings.warn`` (on
both the sign and the verify path) and would otherwise be lost in a JSON
log stream. :func:`setup_logging` therefore installs a narrow, one-time
capture: ``warnings.showwarning`` is wrapped so that this specific category
is logged to the ``bond_accounting.pyjwt_warnings`` logger, while every other
warning keeps its default behaviour via delegation to the previous
``showwarning``. The wrapper stays installed for the lifetime of the
process.

:func:`setup_logging` is idempotent: repeated calls replace previously
installed (managed) handlers instead of duplicating them, and the
PyJWT-warning capture is installed only once.
"""

from __future__ import annotations

import logging
import sys
import warnings
from typing import TYPE_CHECKING, TextIO

from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.msgspec import MsgspecFormatter

if TYPE_CHECKING:
    from collections.abc import Callable

    from bond_accounting.config.settings import LoggingConfig

__all__ = ["reset_third_party_loggers", "setup_logging"]

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

#: Third-party loggers forcibly reset by :func:`setup_logging` /
#: :func:`reset_third_party_loggers` so their records propagate to the root
#: handler instead of going through library-installed handlers.
_THIRD_PARTY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "nicegui",
    "sqlalchemy",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "alembic",
    "fastapi",
)

#: Dedicated logger for PyJWT's ``InsecureKeyLengthWarning``; the fixed name
#: shows up in the JSON ``name`` field of the emitted record.
_PYJWT_WARNING_LOGGER = "bond_accounting.pyjwt_warnings"

#: ``warnings.showwarning`` captured before installing the PyJWT wrapper; used
#: to delegate every non-PyJWT warning to the default behaviour.
_prev_showwarning: (
    Callable[[Warning | str, type[Warning], str, int, TextIO | None, str | None], None] | None
) = None

#: Guards against installing the PyJWT-warning capture (filter + showwarning
#: wrapper) more than once across repeated :func:`setup_logging` calls.
_pyjwt_capture_installed = False


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


def reset_third_party_loggers() -> None:
    """Reset known third-party loggers so their records reach the root logger.

    For every logger in :data:`_THIRD_PARTY_LOGGERS` all handlers are removed
    (and closed), the level is reset to ``NOTSET`` (inheriting from the root)
    and ``propagate`` is re-enabled. Idempotent; safe to call at runtime if a
    library re-installs its own handlers after :func:`setup_logging`.
    """
    for name in _THIRD_PARTY_LOGGERS:
        third_party_logger = logging.getLogger(name)
        for handler in third_party_logger.handlers[:]:
            third_party_logger.removeHandler(handler)
            handler.close()
        third_party_logger.setLevel(logging.NOTSET)
        third_party_logger.propagate = True


def _install_pyjwt_warning_capture() -> None:
    """Convert PyJWT's ``InsecureKeyLengthWarning`` into a log record.

    Wraps ``warnings.showwarning`` so that only this category is routed to the
    :data:`_PYJWT_WARNING_LOGGER` logger (and from there through the root
    handler); every other warning is delegated to the previously installed
    ``showwarning`` unchanged. Installs the capture only once per process.
    """
    global _prev_showwarning, _pyjwt_capture_installed
    if _pyjwt_capture_installed:
        return

    try:
        from jwt.warnings import InsecureKeyLengthWarning
    except ImportError:
        logging.getLogger(__name__).warning(
            "PyJWT is not importable; InsecureKeyLengthWarning will not be captured"
        )
        return

    warnings.filterwarnings("once", category=InsecureKeyLengthWarning)
    _prev_showwarning = warnings.showwarning

    def _show_warning_pyjwt(
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = None,
        line: str | None = None,
    ) -> None:
        if category is InsecureKeyLengthWarning:
            logging.getLogger(_PYJWT_WARNING_LOGGER).warning("%s: %s", category.__name__, message)
            return
        if _prev_showwarning is not None:
            _prev_showwarning(message, category, filename, lineno, file, line)

    warnings.showwarning = _show_warning_pyjwt  # ty: ignore[invalid-assignment]
    _pyjwt_capture_installed = True


def setup_logging(config: LoggingConfig) -> None:
    """Configure the root logger per the config (json or text).

    Installs a single ``logging.StreamHandler`` on stdout with the configured
    format and level, removing any handlers previously installed by this
    function (so repeated calls never duplicate handlers). The ``logging``
    module's ``lastResort`` fallback handler is aligned with the configured
    level as well. Known third-party loggers are forcibly reset so their
    records flow through the root handler, and PyJWT's
    ``InsecureKeyLengthWarning`` is captured into a log record (once per
    process).

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

    reset_third_party_loggers()
    _install_pyjwt_warning_capture()

    logging.getLogger(__name__).info(
        "Logging configured: level=%s format=%s", config.level, config.format
    )
