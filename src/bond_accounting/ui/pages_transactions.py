"""Страница сделок (``/transactions``): ввод BUY/SELL/MATURE и история."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nicegui import ui
from pydantic import ValidationError

from bond_accounting.portfolio.dto import TransactionCreate
from bond_accounting.portfolio.service import PortfolioError
from bond_accounting.ui.common import (
    fmt_date,
    fmt_money,
    get_current_user,
    notify_error,
    page_header,
    parse_date,
)

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService
    from bond_accounting.bonds.service import BondService
    from bond_accounting.portfolio.service import PortfolioService

logger = logging.getLogger(__name__)

#: Варианты типа сделки: значение DTO -> подпись в UI.
_TYPE_OPTIONS: dict[str, str] = {
    "BUY": "Покупка",
    "SELL": "Продажа",
    "MATURE": "Погашение",
}

_COLUMNS: list[dict[str, Any]] = [
    {"name": "date", "label": "Дата", "field": "date", "align": "right", "sortable": True},
    {"name": "isin", "label": "ISIN", "field": "isin", "align": "left"},
    {"name": "type", "label": "Тип", "field": "type", "align": "left"},
    {"name": "quantity", "label": "Количество", "field": "quantity", "align": "right"},
    {"name": "price", "label": "Цена", "field": "price", "align": "right"},
    {"name": "commission", "label": "Комиссия", "field": "commission", "align": "right"},
]


def register_transaction_pages(
    jwt_service: JwtService, portfolio_service: PortfolioService, bond_service: BondService
) -> None:
    """Зарегистрировать страницу ``/transactions``.

    Args:
        jwt_service: Сервис проверки JWT из cookie.
        portfolio_service: Сервис записи сделок и истории.
        bond_service: Справочник облигаций для выбора в форме сделки.
    """

    @ui.page("/transactions")
    async def transactions_page() -> None:
        """Форма ввода сделки и таблица сделок пользователя."""
        user = get_current_user(jwt_service)
        if user is None:
            return

        try:
            bonds = await bond_service.list_all()
        except Exception as exc:
            notify_error(exc)
            bonds = []
        bond_options: dict[int, str] = {bond.id: f"{bond.isin} — {bond.name}" for bond in bonds}

        async def load_rows() -> list[dict[str, Any]]:
            """Собрать строки таблицы сделок с ISIN облигаций."""
            transactions = await portfolio_service.list_transactions(user.user_id)
            bonds = {bond.id: bond for bond in await bond_service.list_all()}
            return [
                {
                    "date": fmt_date(txn.date),
                    "isin": bonds[txn.bond_id].isin if txn.bond_id in bonds else f"#{txn.bond_id}",
                    "type": _TYPE_OPTIONS.get(txn.type, txn.type),
                    "quantity": txn.quantity,
                    "price": fmt_money(txn.price),
                    "commission": fmt_money(txn.commission),
                }
                for txn in transactions
            ]

        async def reload() -> None:
            """Перезагрузить таблицу сделок."""
            try:
                table.rows = await load_rows()
            except Exception as exc:
                notify_error(exc)

        async def handle_add() -> None:
            """Записать сделку из формы и обновить историю."""
            if bond_select.value is None:
                ui.notify("Выберите облигацию", type="negative")
                return
            try:
                txn = await portfolio_service.add_transaction(
                    user.user_id,
                    TransactionCreate(
                        bond_id=bond_select.value,
                        type=type_select.value,
                        quantity=int(quantity_input.value or 0),
                        price=float(price_input.value or 0),
                        date=parse_date(trade_date_input.value, "Дата сделки"),
                        commission=float(commission_input.value or 0),
                    ),
                )
            except (ValidationError, ValueError) as exc:
                ui.notify(f"Некорректные данные сделки: {exc}", type="negative")
                return
            except PortfolioError as exc:
                ui.notify(str(exc), type="negative")
                return
            except Exception as exc:
                notify_error(exc)
                return
            logger.info(
                "UI: записана сделка user_id=%s bond_id=%s type=%s",
                user.user_id,
                txn.bond_id,
                txn.type,
            )
            ui.notify("Сделка записана", type="positive")
            await reload()

        page_header("/transactions")
        with ui.column().classes("w-full max-w-5xl mx-auto gap-6"):
            ui.label("Сделки").classes("text-h5")
            with ui.card().classes("w-full"):
                ui.label("Новая сделка").classes("text-subtitle1")
                with ui.row().classes("w-full items-start gap-2"):
                    bond_select = ui.select(bond_options, label="Облигация")
                    type_select = ui.select(_TYPE_OPTIONS, value="BUY", label="Тип")
                    quantity_input = ui.number("Количество", min=1)
                    price_input = ui.number("Цена")
                    trade_date_input = ui.date_input("Дата")
                    commission_input = ui.number("Комиссия", value=0, min=0)
                ui.button("Добавить сделку", icon="add", on_click=handle_add)

            table = ui.table(columns=_COLUMNS, rows=[]).classes("w-full")
            ui.button("Обновить", icon="refresh", on_click=reload)

        await reload()
