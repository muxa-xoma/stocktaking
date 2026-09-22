"""Страница сделок (``/transactions``): ввод BUY/SELL/MATURE и история.

Формы вынесены в модальные окна (единый визуальный паттерн с другими
страницами, см. ``crud_mixin.py``). Особенности страницы, из-за которых она
не использует ``CrudPageMixin``:

* тип сделки меняет состав полей (для MATURE нет комиссии);
* цена и комиссия вводятся в выбираемом режиме — «за бумагу» или «за
  сделку» — и при сохранении приводятся к цене/комиссии за бумагу
  (так значения хранятся в БД);
* под комиссией показывается подсказка с расчётом по тарифам брокера
  (``commission`` / ``min_commission`` / ``min_commission_type``),
  обновляющаяся на каждое изменение количества, цены, режима или счёта.

Окна создания и правки строятся из одного набора виджетов
(:class:`_TransactionForm`), чтобы поля, переключатели режимов и подсказка
комиссии вели себя одинаково. Клик по строке таблицы открывает окно правки,
из него — подтверждение удаления (как на других страницах).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from nicegui import ui
from pydantic import ValidationError

from bond_accounting.portfolio.dto import TransactionCreate, TransactionUpdate
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
    row_from_event,
    style_table,
)

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.bonds.service import BondService
    from bond_accounting.brokers.service import BrokerService
    from bond_accounting.portfolio.dto import TransactionDTO
    from bond_accounting.portfolio.service import PortfolioService

logger = logging.getLogger(__name__)

#: Варианты типа сделки: значение DTO -> подпись в UI.
_TYPE_OPTIONS: dict[str, str] = {
    "BUY": "Покупка",
    "SELL": "Продажа",
    "MATURE": "Погашение",
}

#: Значения переключателя режима ввода цены/комиссии.
_MODE_OPTIONS: dict[str, str] = {
    "paper": "За бумагу",
    "trade": "За сделку",
}


def _ceil_to_2(value: float) -> float:
    """Округление вверх до 2 знаков (2.351 → 2.36).

    ``round(value * 100, 9)`` убирает шум двоичной арифметики
    (например 2.36 * 100 = 236.00000000000003), чтобы ``ceil`` не
    поднимал уже «ровное» значение на копейку.
    """
    return math.ceil(round(value * 100, 9)) / 100


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
class _TransactionForm:
    """Виджеты одной формы сделки (создания или правки).

    Одна и та же структура используется обоими модальными окнами, поэтому
    реакции на изменение полей (видимость комиссии, подсказка тарифа) и
    сбор значений при сохранении не дублируются.
    """

    account_select: Any = None
    bond_select: Any = None
    type_select: Any = None
    quantity_input: Any = None
    price_input: Any = None
    price_mode_toggle: Any = None
    commission_section: Any = None
    commission_input: Any = None
    commission_mode_toggle: Any = None
    commission_hint: Any = None
    trade_date_input: Any = None


@dataclass
class _TransactionsState:
    """Локальное состояние страницы сделок: виджеты модальных форм и таблица.

    Создаётся в :meth:`TransactionsPage.render` до построения виджетов и
    заполняется по мере построения. ``cache`` — id сделки -> DTO для формы
    правки; ``broker_cache`` — счёт -> тариф брокера этого счёта (кэш на
    время жизни страницы, чтобы подсказка комиссии не перечитывала брокера
    на каждый ввод); ``bond_labels`` — id облигации -> подпись для текста
    подтверждения удаления.
    """

    user_id: int
    table: Any = None
    create_dialog: Any = None
    edit_dialog: Any = None
    confirm_delete_dialog: Any = None
    confirm_message: Any = None
    selected_id: int | None = None
    cache: dict[int, TransactionDTO] = field(default_factory=dict)
    bond_labels: dict[int, str] = field(default_factory=dict)
    broker_cache: dict[int, Any] = field(default_factory=dict)
    create: _TransactionForm = field(default_factory=_TransactionForm)
    edit: _TransactionForm = field(default_factory=_TransactionForm)


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
        state.cache = {txn.id: txn for txn in transactions}
        return [
            {
                "id": txn.id,
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
        """Перезагрузить таблицу сделок и сбросить выбор записи."""
        try:
            state.table.rows = await self._load_rows(state)
        except Exception as exc:
            notify_error(exc)
            return
        state.selected_id = None
        state.table.selected = []

    async def _broker_for_account(self, state: _TransactionsState, account_id: int) -> Any:
        """Тариф брокера счёта (с кэшем); ``None``, если счёт/брокер не найдены."""
        if account_id not in state.broker_cache:
            broker = None
            account = await self.broker_service.get_account(account_id, user_id=state.user_id)
            if account is not None:
                broker = await self.broker_service.get_broker(account.broker_id)
            state.broker_cache[account_id] = broker
        return state.broker_cache[account_id]

    async def _update_form(self, state: _TransactionsState, form: _TransactionForm) -> None:
        """Реакция на изменение полей формы: видимость комиссии и подсказка."""
        form.commission_section.set_visibility(form.type_select.value != "MATURE")
        await self._update_commission_hint(state, form)

    async def _update_commission_hint(
        self, state: _TransactionsState, form: _TransactionForm
    ) -> None:
        """Пересчитать подсказку примерной комиссии по тарифу брокера счёта.

        Оценивается комиссия всей сделки: для режима «за бумагу» базой
        служит ``количество × цена``, для «за сделку» — введённая цена как
        есть. Тариф берётся у брокера выбранного счёта:

        * ``PERCENT``: ``min_commission`` трактуется как минимальная ставка
          (проценты), берётся ``max(commission, min_commission)``;
        * ``RUBLES``: ``min_commission`` — минимальная сумма в рублях,
          берётся ``max(процент от суммы, min_commission)``.
        """
        if form.type_select.value == "MATURE":
            form.commission_hint.set_text("Примерная комиссия: —")
            return
        account_id = resolve_account_id(form.account_select.value)
        quantity = form.quantity_input.value or 0
        price = form.price_input.value or 0
        if account_id is None or quantity <= 0 or price <= 0:
            form.commission_hint.set_text("Примерная комиссия: —")
            return
        try:
            broker = await self._broker_for_account(state, account_id)
        except Exception as exc:
            logger.info("UI: не удалось загрузить тариф брокера для подсказки (%s)", exc)
            broker = None
        if broker is None:
            form.commission_hint.set_text("Примерная комиссия: —")
            return
        if form.price_mode_toggle.value == "paper":
            total_price = float(quantity) * float(price)
        else:
            total_price = float(price)
        min_commission = broker.min_commission or 0.0
        if broker.min_commission_type == "RUBLES":
            calc = total_price * broker.commission / 100
            commission_total = max(calc, min_commission)
        else:  # PERCENT
            effective_rate = max(broker.commission, min_commission)
            commission_total = total_price * effective_rate / 100
        form.commission_hint.set_text(f"Примерная комиссия: {commission_total:.2f}")

    def _gather_form(
        self, state: _TransactionsState, form: _TransactionForm
    ) -> dict[str, Any] | None:
        """Собрать и проверить значения формы; ``None`` — данные некорректны.

        Значения цены и комиссии приводятся к «за бумагу» (формат
        хранения): режим «за сделку» делится на количество и результат
        округляется вверх до 2 знаков (ceil). Для MATURE комиссия не
        вводится и записывается нулевой.
        """
        account_id = resolve_account_id(form.account_select.value)
        if account_id is None:
            ui.notify("Выберите брокерский счёт", type="negative")
            return None
        if form.bond_select.value is None:
            ui.notify("Выберите облигацию", type="negative")
            return None
        # Дата разбирается до проверок остальных полей: пустое или некорректное
        # значение должно показывать единый тост валидации «Некорректные данные
        # сделки» (исключение ловится вызывающими обработчиками).
        trade_date = parse_date(form.trade_date_input.value, "Дата сделки")
        quantity = int(form.quantity_input.value or 0)
        if quantity <= 0:
            ui.notify("Количество должно быть положительным", type="negative")
            return None
        price_raw = float(form.price_input.value or 0)
        price = (
            _ceil_to_2(price_raw / quantity)
            if form.price_mode_toggle.value == "trade"
            else price_raw
        )
        if form.type_select.value == "MATURE":
            commission = 0.0
        else:
            commission_raw = float(form.commission_input.value or 0)
            commission = (
                _ceil_to_2(commission_raw / quantity)
                if form.commission_mode_toggle.value == "trade"
                else commission_raw
            )
        return {
            "bond_id": form.bond_select.value,
            "broker_account_id": account_id,
            "type": form.type_select.value,
            "quantity": quantity,
            "price": price,
            "date": trade_date,
            "commission": commission,
        }

    async def _handle_create(self, state: _TransactionsState) -> None:
        """Записать сделку из формы создания и обновить историю."""
        try:
            values = self._gather_form(state, state.create)
            if values is None:
                return
            txn = await self.portfolio_service.add_transaction(
                state.user_id, TransactionCreate(**values)
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
        state.create_dialog.close()
        self._reset_transaction_create_form(state)
        await self._reload(state)

    def _reset_transaction_create_form(self, state: _TransactionsState) -> None:
        form = state.create
        form.bond_select.value = None
        form.type_select.value = "BUY"
        form.quantity_input.value = None
        form.price_input.value = None
        form.price_mode_toggle.value = "paper"
        form.commission_input.value = 0
        form.commission_mode_toggle.value = "paper"
        form.commission_hint.set_text("Примерная комиссия: —")
        form.trade_date_input.value = None

    def _open_create_dialog(self, state: _TransactionsState) -> None:
        self._reset_transaction_create_form(state)
        state.create_dialog.open()

    async def _handle_update(self, state: _TransactionsState) -> None:
        """Сохранить изменения выбранной сделки и обновить историю."""
        if state.selected_id is None:
            ui.notify("Сначала выберите сделку в таблице", type="warning")
            return
        try:
            values = self._gather_form(state, state.edit)
            if values is None:
                return
            txn = await self.portfolio_service.update_transaction(
                state.selected_id, TransactionUpdate(**values), state.user_id
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
        if txn is None:
            ui.notify("Сделка не найдена", type="warning")
            await self._reload(state)
            return
        logger.info(
            "UI: обновлена сделка id=%s user_id=%s type=%s", txn.id, state.user_id, txn.type
        )
        ui.notify("Сделка обновлена", type="positive")
        state.edit_dialog.close()
        await self._reload(state)

    def _handle_delete(self, state: _TransactionsState) -> None:
        """Закрыть окно правки и открыть подтверждение удаления сделки."""
        if state.selected_id is None:
            ui.notify("Сначала выберите сделку в таблице", type="warning")
            return
        state.edit_dialog.close()
        state.confirm_delete_dialog.open()

    async def _confirm_delete(self, state: _TransactionsState) -> None:
        """Удалить выбранную сделку после подтверждения."""
        state.confirm_delete_dialog.close()
        if state.selected_id is None:
            return
        txn_id = state.selected_id
        try:
            deleted = await self.portfolio_service.delete_transaction(txn_id, state.user_id)
        except PortfolioError as exc:
            ui.notify(str(exc), type="negative")
            return
        except Exception as exc:
            notify_error(exc)
            return
        if deleted:
            logger.info("UI: удалена сделка id=%s user_id=%s", txn_id, state.user_id)
            ui.notify("Сделка удалена", type="positive")
        else:
            ui.notify("Сделка не найдена", type="warning")
        await self._reload(state)

    async def _on_row_click(self, state: _TransactionsState, e: Any) -> None:
        """Заполнить форму правки и открыть модальное окно при клике по строке."""
        row = row_from_event(e, "id")
        if row is None:
            return
        txn = state.cache.get(row["id"])
        if txn is None:
            return
        state.selected_id = txn.id
        self._fill_edit_form(state, txn)
        state.edit_dialog.open()
        await self._update_form(state, state.edit)

    def _fill_edit_form(self, state: _TransactionsState, txn: TransactionDTO) -> None:
        """Заполнить форму правки значениями выбранной сделки.

        Цена и комиссия подставляются в режиме «за бумагу» (формат
        хранения), текст подтверждения удаления — «тип ISIN — название
        количество шт.».
        """
        form = state.edit
        form.account_select.value = txn.broker_account_id
        form.bond_select.value = txn.bond_id
        form.type_select.value = txn.type
        form.quantity_input.value = txn.quantity
        form.price_mode_toggle.value = "paper"
        form.price_input.value = txn.price
        form.commission_mode_toggle.value = "paper"
        form.commission_input.value = txn.commission
        form.trade_date_input.value = txn.date.isoformat()
        bond_label = state.bond_labels.get(txn.bond_id, f"#{txn.bond_id}")
        state.confirm_message.set_text(
            f"Вы уверены, что хотите удалить сделку "
            f"'{_TYPE_OPTIONS.get(txn.type, txn.type)} {bond_label} {txn.quantity} шт.'?"
        )

    async def render(self, user: TokenPayload) -> None:
        """Таблица сделок и модальные окна создания, правки и удаления."""
        state = _TransactionsState(user_id=user.user_id)

        try:
            bonds = await self.bond_service.list_all()
        except Exception as exc:
            notify_error(exc)
            bonds = []
        bond_options: dict[int, str] = {bond.id: f"{bond.isin} — {bond.name}" for bond in bonds}
        state.bond_labels = bond_options

        page_header("/transactions")
        with ui.column().classes("w-full gap-6"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Сделки").classes("text-h5")
                with ui.row().classes("gap-2"):
                    ui.button(
                        "Добавить", icon="add", on_click=partial(self._open_create_dialog, state)
                    )
                    ui.button("Обновить", icon="refresh", on_click=partial(self._reload, state))

            state.table = ui.table(columns=_COLUMNS, rows=[], row_key="id")
            style_table(state.table)

        with ui.dialog() as create_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.create_dialog = create_dialog
            form = state.create
            ui.label("Новая сделка").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                form.account_select = await build_account_selector(
                    self.broker_service, user.user_id, mandatory=True
                )
                form.bond_select = ui.select(bond_options, label="Облигация")
                form.type_select = ui.select(_TYPE_OPTIONS, value="BUY", label="Тип")
                form.quantity_input = ui.number("Количество", min=1)
                form.price_input = ui.number("Цена", min=0, step=0.01)
                form.price_mode_toggle = ui.toggle(_MODE_OPTIONS, value="paper")
                with ui.column().classes("w-full gap-2") as commission_section:
                    form.commission_input = ui.number("Комиссия", value=0, min=0, step=0.01)
                    form.commission_mode_toggle = ui.toggle(_MODE_OPTIONS, value="paper")
                    form.commission_hint = ui.label("Примерная комиссия: —").classes(
                        "text-sm text-gray-400"
                    )
                form.commission_section = commission_section
                form.trade_date_input = ui.date_input("Дата")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Добавить", icon="check", on_click=partial(self._handle_create, state))
                ui.button("Отмена", on_click=create_dialog.close).props("flat")

        with ui.dialog() as edit_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.edit_dialog = edit_dialog
            form = state.edit
            ui.label("Редактирование сделки").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                form.account_select = await build_account_selector(
                    self.broker_service, user.user_id, mandatory=True
                )
                form.bond_select = ui.select(bond_options, label="Облигация")
                form.type_select = ui.select(_TYPE_OPTIONS, label="Тип")
                form.quantity_input = ui.number("Количество", min=1)
                form.price_input = ui.number("Цена", min=0, step=0.01)
                form.price_mode_toggle = ui.toggle(_MODE_OPTIONS, value="paper")
                with ui.column().classes("w-full gap-2") as commission_section:
                    form.commission_input = ui.number("Комиссия", value=0, min=0, step=0.01)
                    form.commission_mode_toggle = ui.toggle(_MODE_OPTIONS, value="paper")
                    form.commission_hint = ui.label("Примерная комиссия: —").classes(
                        "text-sm text-gray-400"
                    )
                form.commission_section = commission_section
                form.trade_date_input = ui.date_input("Дата")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Сохранить", icon="save", on_click=partial(self._handle_update, state))
                ui.button(
                    "Удалить", icon="delete", on_click=partial(self._handle_delete, state)
                ).props("color=negative")
                ui.button("Отмена", on_click=edit_dialog.close).props("flat")

        with ui.dialog() as confirm_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.confirm_delete_dialog = confirm_dialog
            state.confirm_message = ui.label("")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Да", on_click=partial(self._confirm_delete, state)).props(
                    "color=negative"
                )
                ui.button("Нет", on_click=confirm_dialog.close).props("flat")

        for form in (state.create, state.edit):
            on_form_change = partial(self._update_form, state, form)
            form.account_select.on_value_change(on_form_change)
            form.type_select.on_value_change(on_form_change)
            form.quantity_input.on_value_change(on_form_change)
            form.price_input.on_value_change(on_form_change)
            form.price_mode_toggle.on_value_change(on_form_change)

        state.table.on("rowClick", partial(self._on_row_click, state))

        await self._reload(state)
