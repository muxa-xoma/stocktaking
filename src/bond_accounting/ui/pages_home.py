"""Главная страница портфеля (``/``): текущие позиции пользователя."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from nicegui import ui

from bond_accounting.ui.base_page import BasePage
from bond_accounting.ui.common import (
    build_account_selector,
    fmt_money,
    notify_error,
    page_header,
    resolve_account_id,
)

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.bonds.service import BondService
    from bond_accounting.brokers.service import BrokerService
    from bond_accounting.portfolio.service import PortfolioService

logger = logging.getLogger(__name__)

_COLUMNS: list[dict[str, Any]] = [
    {"name": "isin", "label": "ISIN", "field": "isin", "align": "left", "sortable": True},
    {"name": "name", "label": "Название", "field": "name", "align": "left"},
    {
        "name": "quantity",
        "label": "Количество",
        "field": "quantity",
        "align": "right",
        "sortable": True,
    },
    {
        "name": "avg_buy_price",
        "label": "Средняя цена покупки",
        "field": "avg_buy_price",
        "align": "right",
    },
    {"name": "total_invested", "label": "Вложено", "field": "total_invested", "align": "right"},
]


@dataclass
class _HomeState:
    """Мутируемое состояние страницы: идентификатор пользователя и виджеты."""

    user_id: int
    account_select: ui.select
    table: ui.table


class HomePage(BasePage):
    """Главная страница портфеля (``/``)."""

    path = "/"

    def __init__(
        self,
        jwt_service: JwtService,
        portfolio_service: PortfolioService,
        bond_service: BondService,
        broker_service: BrokerService,
    ) -> None:
        super().__init__(jwt_service)
        self.portfolio_service = portfolio_service
        self.bond_service = bond_service
        self.broker_service = broker_service

    async def _load_rows(self, state: _HomeState) -> list[dict[str, Any]]:
        """Собрать строки таблицы: позиции, обогащённые данными облигаций."""
        account_id = resolve_account_id(state.account_select.value)
        positions = await self.portfolio_service.get_all_positions(
            state.user_id, broker_account_id=account_id
        )
        bonds = {bond.id: bond for bond in await self.bond_service.list_all()}
        rows: list[dict[str, Any]] = []
        for position in positions:
            bond = bonds.get(position.bond_id)
            if bond is None:
                logger.warning(
                    "UI: позиция ссылается на несуществующую облигацию id=%s", position.bond_id
                )
                continue
            rows.append(
                {
                    "isin": bond.isin,
                    "name": bond.name,
                    "quantity": position.quantity,
                    "avg_buy_price": fmt_money(position.avg_buy_price),
                    "total_invested": fmt_money(position.total_invested),
                }
            )
        return rows

    async def _reload(self, state: _HomeState) -> None:
        """Перезагрузить данные позиций в таблицу."""
        try:
            state.table.rows = await self._load_rows(state)
        except Exception as exc:
            notify_error(exc)

    async def render(self, user: TokenPayload) -> None:
        """Таблица позиций портфеля с фильтром по счёту и кнопкой обновления."""
        page_header("/")
        with ui.column().classes("w-full max-w-4xl mx-auto gap-4"):
            ui.label("Портфель").classes("text-h5")
            account_select = await build_account_selector(self.broker_service, user.user_id)
            table = ui.table(columns=_COLUMNS, rows=[]).classes("w-full")
            refresh_button = ui.button("Обновить", icon="refresh")

        state = _HomeState(
            user_id=user.user_id,
            account_select=account_select,
            table=table,
        )
        account_select.on_value_change(partial(self._reload, state))
        refresh_button.on_click(partial(self._reload, state))
        await self._reload(state)
