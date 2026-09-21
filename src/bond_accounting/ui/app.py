"""Точка сборки UI: регистрирует все страницы NiceGUI.

Единственная функция :func:`create_ui_app` вызывается из ``main.py`` до
``ui.run()``; сами страницы разнесены по модулям ``pages_*``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bond_accounting.ui.pages_analytics import register_analytics_pages
from bond_accounting.ui.pages_auth import register_auth_pages
from bond_accounting.ui.pages_bonds import register_bond_pages
from bond_accounting.ui.pages_home import register_home_page
from bond_accounting.ui.pages_transactions import register_transaction_pages

if TYPE_CHECKING:
    from bond_accounting.analytics.service import AnalyticsService
    from bond_accounting.auth.jwt_service import JwtService
    from bond_accounting.auth.service import AuthService
    from bond_accounting.bonds.service import BondService
    from bond_accounting.portfolio.service import PortfolioService

logger = logging.getLogger(__name__)


def create_ui_app(
    auth_service: AuthService,
    jwt_service: JwtService,
    bond_service: BondService,
    portfolio_service: PortfolioService,
    analytics_service: AnalyticsService,
) -> None:
    """Зарегистрировать все страницы UI (вызывается из main.py ДО ui.run()).

    Args:
        auth_service: Сервис аутентификации (вход, регистрация).
        jwt_service: Сервис проверки JWT из cookie ``token``.
        bond_service: Сервис CRUD облигаций.
        portfolio_service: Сервис сделок и позиций.
        analytics_service: Сервис аналитики портфеля.
    """
    register_auth_pages(auth_service)
    register_home_page(jwt_service, portfolio_service, bond_service)
    register_bond_pages(jwt_service, bond_service)
    register_transaction_pages(jwt_service, portfolio_service, bond_service)
    register_analytics_pages(jwt_service, analytics_service)
    logger.info(
        "UI: страницы зарегистрированы (/login, /register, /, /bonds, /transactions, /analytics)"
    )
