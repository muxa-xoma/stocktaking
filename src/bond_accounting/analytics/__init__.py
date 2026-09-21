"""Analytics: portfolio metrics, reporting, and event publishing."""

from bond_accounting.analytics.dto import (
    Cashflow,
    CouponDue,
    PortfolioSummary,
    PositionAnalytics,
    RealizedPnl,
)
from bond_accounting.analytics.service import AnalyticsService, attach_to_event_bus

__all__ = [
    "AnalyticsService",
    "Cashflow",
    "CouponDue",
    "PortfolioSummary",
    "PositionAnalytics",
    "RealizedPnl",
    "attach_to_event_bus",
]
