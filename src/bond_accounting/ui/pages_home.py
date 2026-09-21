"""Главная страница портфеля (``/``): текущие позиции пользователя."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nicegui import ui

from bond_accounting.ui.common import fmt_money, get_current_user, notify_error, page_header

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService
    from bond_accounting.bonds.service import BondService
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


def register_home_page(
    jwt_service: JwtService, portfolio_service: PortfolioService, bond_service: BondService
) -> None:
    """Зарегистрировать страницу ``/`` с таблицей позиций.

    Args:
        jwt_service: Сервис проверки JWT из cookie.
        portfolio_service: Источник позиций пользователя.
        bond_service: Источник справочника облигаций (ISIN, название).
    """

    @ui.page("/")
    async def home_page() -> None:
        """Таблица позиций портфеля с кнопкой обновления."""
        user = get_current_user(jwt_service)
        if user is None:
            return

        async def load_rows() -> list[dict[str, Any]]:
            """Собрать строки таблицы: позиции, обогащённые данными облигаций."""
            positions = await portfolio_service.get_all_positions(user.user_id)
            bonds = {bond.id: bond for bond in await bond_service.list_all()}
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

        async def reload() -> None:
            """Перезагрузить данные позиций в таблицу."""
            try:
                table.rows = await load_rows()
            except Exception as exc:
                notify_error(exc)

        page_header("/")
        with ui.column().classes("w-full max-w-4xl mx-auto gap-4"):
            ui.label("Портфель").classes("text-h5")
            table = ui.table(columns=_COLUMNS, rows=[]).classes("w-full")
            ui.button("Обновить", icon="refresh", on_click=reload)

        await reload()
