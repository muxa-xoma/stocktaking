"""MOEX ISS bond reference integration: search, DTO mapping, TTL cache, errors.

Public surface (SC-3): :class:`MoexClient` (thin async ISS client),
:class:`BondReferenceService` (search + ``get_by_isin`` with an in-memory
TTL cache), the :class:`BondReference` DTO and the module error hierarchy.
See :mod:`bond_accounting.market_data.client` for the verified MOEX ISS
request flow and :mod:`bond_accounting.market_data.schemas` for the field
mapping.
"""

from __future__ import annotations

from bond_accounting.market_data.client import MoexBondRaw, MoexClient
from bond_accounting.market_data.errors import (
    MarketDataError,
    NoBondsFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from bond_accounting.market_data.schemas import (
    BondReference,
    CouponScheduleEntry,
    bond_reference_from_moex,
)
from bond_accounting.market_data.service import BondReferenceService

__all__ = [
    "BondReference",
    "BondReferenceService",
    "CouponScheduleEntry",
    "MarketDataError",
    "MoexBondRaw",
    "MoexClient",
    "NoBondsFoundError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "bond_reference_from_moex",
]
