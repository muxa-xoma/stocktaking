"""Точка сборки UI: регистрирует все страницы NiceGUI.

Единственная функция :func:`create_ui_app` вызывается из ``main.py`` до
``ui.run()``; сами страницы разнесены по модулям ``pages_*``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bond_accounting.ui.pages_accounts import AccountsPage
from bond_accounting.ui.pages_analytics import AnalyticsPage
from bond_accounting.ui.pages_auth import LoginPage, RegisterPage
from bond_accounting.ui.pages_bonds import BondsPage
from bond_accounting.ui.pages_brokers import BrokersPage
from bond_accounting.ui.pages_home import HomePage
from bond_accounting.ui.pages_operations import OperationsPage
from bond_accounting.ui.pages_transactions import TransactionsPage

if TYPE_CHECKING:
    from bond_accounting.account_operations.service import AccountOperationService
    from bond_accounting.analytics.service import AnalyticsService
    from bond_accounting.auth.jwt_service import JwtService
    from bond_accounting.auth.service import AuthService
    from bond_accounting.bonds.service import BondService
    from bond_accounting.brokers.service import BrokerService
    from bond_accounting.portfolio.service import PortfolioService

logger = logging.getLogger(__name__)


def create_ui_app(
    auth_service: AuthService,
    jwt_service: JwtService,
    bond_service: BondService,
    broker_service: BrokerService,
    portfolio_service: PortfolioService,
    analytics_service: AnalyticsService,
    account_operation_service: AccountOperationService,
) -> None:
    """Зарегистрировать все страницы UI (вызывается из main.py ДО ui.run()).

    Args:
        auth_service: Сервис аутентификации (вход, регистрация).
        jwt_service: Сервис проверки JWT из cookie ``token``.
        bond_service: Сервис CRUD облигаций.
        broker_service: Сервис CRUD брокеров и брокерских счетов.
        portfolio_service: Сервис сделок и позиций.
        analytics_service: Сервис аналитики портфеля.
        account_operation_service: Сервис CRUD операций по счёту
            (пополнения, выводы, налоги).
    """
    LoginPage(auth_service, jwt_service).register()
    RegisterPage(auth_service, jwt_service).register()
    HomePage(jwt_service, portfolio_service, bond_service, broker_service).register()
    BondsPage(jwt_service, bond_service).register()
    BrokersPage(jwt_service, broker_service).register()
    AccountsPage(jwt_service, broker_service).register()
    TransactionsPage(jwt_service, portfolio_service, bond_service, broker_service).register()
    OperationsPage(jwt_service, account_operation_service, broker_service).register()
    AnalyticsPage(jwt_service, analytics_service, broker_service).register()
    logger.info(
        "UI: страницы зарегистрированы (/login, /register, /, /bonds, /brokers, "
        "/accounts, /transactions, /operations, /analytics)"
    )
