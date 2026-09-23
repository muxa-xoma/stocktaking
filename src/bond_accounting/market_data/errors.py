"""Error hierarchy for the MOEX ISS bond reference integration.

All errors raised by :mod:`bond_accounting.market_data` derive from
:class:`MarketDataError`. The REST layer maps them per the error contract
(spec §5): provider failures — including a disabled integration — surface
as HTTP 503, a missing ISIN as HTTP 404.
"""

from __future__ import annotations

__all__ = [
    "MarketDataError",
    "NoBondsFoundError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]


class MarketDataError(Exception):
    """Base error for the MOEX bond reference integration."""


class ProviderUnavailableError(MarketDataError):
    """The MOEX ISS provider is unusable.

    Raised on transport errors, non-200 responses and malformed responses.
    By contract it is also raised by the reference service when the
    integration is disabled via configuration (``market_data.enabled =
    false``): callers treat both cases identically (REST 503, static UI
    hint).
    """


class ProviderTimeoutError(MarketDataError):
    """The MOEX ISS provider did not answer within the configured timeout."""


class NoBondsFoundError(MarketDataError):
    """No MOEX bond matched the requested ISIN."""
