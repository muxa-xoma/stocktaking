"""Страница сделок (``/transactions``): ввод BUY/SELL/MATURE и история."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from nicegui import ui
from pydantic import ValidationError

from bond_accounting.portfolio.dto import TransactionCreate
from bond_accounting.portfolio.service import PortfolioError
from bond_accounting.ui.base_page import BasePage
from bond_accounting.ui.common import (
    build_account_selector,
    fmt_date,
    fmt_money,
    notify_error,
    page_header,
    parse_date,
    resolve_account_id,
)

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.bonds.service import BondService
    from bond_accounting.brokers.service import BrokerService
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
    {"name": "account", "label": "Счёт", "field": "account", "align": "left"},
    {"name": "type", "label": "Тип", "field": "type", "align": "left"},
    {"name": "quantity", "label": "Количество", "field": "quantity", "align": "right"},
    {"name": "price", "label": "Цена", "field": "price", "align": "right"},
    {"name": "commission", "label": "Комиссия", "field": "commission", "align": "right"},
]


@dataclass
class _TransactionsState:
    """Мутируемое состояние страницы: идентификатор пользователя, форма и таблица."""

    user_id: int
    account_select: ui.select
    bond_select: ui.select
    type_select: ui.select
    quantity_input: ui.number
    price_input: ui.number
    trade_date_input: ui.date_input
    commission_input: ui.number
    table: ui.table


class TransactionsPage(BasePage):
    """Страница сделок (``/transactions``)."""

    path = "/transactions"

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

    async def _load_rows(self, state: _TransactionsState) -> list[dict[str, Any]]:
        """Собрать строки таблицы сделок с ISIN облигаций и именами счетов."""
        transactions = await self.portfolio_service.list_transactions(state.user_id)
        bonds = {bond.id: bond for bond in await self.bond_service.list_all()}
        accounts = {
            acc.id: f"{acc.broker_name} / {acc.name}"
            for acc in await self.broker_service.list_accounts_for_user(state.user_id)
        }
        return [
            {
                "date": fmt_date(txn.date),
                "isin": bonds[txn.bond_id].isin if txn.bond_id in bonds else f"#{txn.bond_id}",
                "account": accounts.get(txn.broker_account_id, f"#{txn.broker_account_id}"),
                "type": _TYPE_OPTIONS.get(txn.type, txn.type),
                "quantity": txn.quantity,
                "price": fmt_money(txn.price),
                "commission": fmt_money(txn.commission),
            }
            for txn in transactions
        ]

    async def _reload(self, state: _TransactionsState) -> None:
        """Перезагрузить таблицу сделок."""
        try:
            state.table.rows = await self._load_rows(state)
        except Exception as exc:
            notify_error(exc)

    async def _handle_add(self, state: _TransactionsState) -> None:
        """Записать сделку из формы и обновить историю."""
        account_id = resolve_account_id(state.account_select.value)
        if account_id is None:
            ui.notify("Выберите брокерский счёт", type="negative")
            return
        if state.bond_select.value is None:
            ui.notify("Выберите облигацию", type="negative")
            return
        try:
            txn = await self.portfolio_service.add_transaction(
                state.user_id,
                TransactionCreate(
                    bond_id=state.bond_select.value,
                    broker_account_id=account_id,
                    type=state.type_select.value,
                    quantity=int(state.quantity_input.value or 0),
                    price=float(state.price_input.value or 0),
                    date=parse_date(state.trade_date_input.value, "Дата сделки"),
                    commission=float(state.commission_input.value or 0),
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
            state.user_id,
            txn.bond_id,
            txn.type,
        )
        ui.notify("Сделка записана", type="positive")
        await self._reload(state)

    async def render(self, user: TokenPayload) -> None:
        """Форма ввода сделки и таблица сделок пользователя."""

        try:
            bonds = await self.bond_service.list_all()
        except Exception as exc:
            notify_error(exc)
            bonds = []
        bond_options: dict[int, str] = {bond.id: f"{bond.isin} — {bond.name}" for bond in bonds}

        page_header("/transactions")
        with ui.column().classes("w-full max-w-5xl mx-auto gap-6"):
            ui.label("Сделки").classes("text-h5")
            with ui.card().classes("w-full"):
                ui.label("Новая сделка").classes("text-subtitle1")
                account_select = await build_account_selector(
                    self.broker_service, user.user_id, mandatory=True
                )
                with ui.row().classes("w-full items-start gap-2"):
                    bond_select = ui.select(bond_options, label="Облигация")
                    type_select = ui.select(_TYPE_OPTIONS, value="BUY", label="Тип")
                    quantity_input = ui.number("Количество", min=1)
                    price_input = ui.number("Цена")
                    trade_date_input = ui.date_input("Дата")
                    commission_input = ui.number("Комиссия", value=0, min=0)
                add_button = ui.button("Добавить сделку", icon="add")

            table = ui.table(columns=_COLUMNS, rows=[]).classes("w-full")
            refresh_button = ui.button("Обновить", icon="refresh")

        state = _TransactionsState(
            user_id=user.user_id,
            account_select=account_select,
            bond_select=bond_select,
            type_select=type_select,
            quantity_input=quantity_input,
            price_input=price_input,
            trade_date_input=trade_date_input,
            commission_input=commission_input,
            table=table,
        )
        add_button.on_click(partial(self._handle_add, state))
        refresh_button.on_click(partial(self._reload, state))
        await self._reload(state)
