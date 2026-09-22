"""Страница операций по счёту (``/operations``): пополнения, выводы, налоги."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from nicegui import ui

from bond_accounting.account_operations.dto import (
    AccountOperationCreate,
    AccountOperationUpdate,
)
from bond_accounting.ui.base_page import BasePage
from bond_accounting.ui.common import (
    fmt_date,
    fmt_money,
    notify_error,
    page_header,
    parse_date,
    style_table,
)
from bond_accounting.ui.crud_mixin import CrudPageMixin

if TYPE_CHECKING:
    from bond_accounting.account_operations.service import AccountOperationService
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.brokers.service import BrokerService

logger = logging.getLogger(__name__)

#: Варианты типа операции: значение DTO -> подпись в UI.
_TYPE_OPTIONS: dict[str, str] = {
    "DEPOSIT": "Пополнение",
    "WITHDRAWAL": "Вывод средств",
    "TAX": "Налог",
}

_COLUMNS: list[dict[str, Any]] = [
    {"name": "date", "label": "Дата", "field": "date", "align": "left", "sortable": True},
    {"name": "account", "label": "Счёт", "field": "account", "align": "left"},
    {"name": "type", "label": "Тип", "field": "type", "align": "left", "sortable": True},
    {"name": "amount", "label": "Сумма", "field": "amount", "align": "right", "sortable": True},
    {"name": "note", "label": "Примечание", "field": "note", "align": "left"},
]


@dataclass
class _OperationsState:
    """Локальное состояние страницы операций: виджеты, кэш и текущий выбор.

    Создаётся в :meth:`OperationsPage.render` в локальной области видимости и
    передаётся первым аргументом в общие методы :class:`CrudPageMixin`
    (поля ``cache``, ``selected_id``, ``table``, ``create_dialog``,
    ``edit_dialog``, ``confirm_delete_dialog`` — часть интерфейса миксина).
    Виджетные поля заполняются по мере построения страницы в
    :meth:`render`.
    """

    owner_id: int
    cache: dict[int, Any]
    selected_id: int | None
    #: Подписи выпадающих списков счетов: id счёта -> «Брокер / Счёт».
    account_options: dict[int, str] = field(default_factory=dict)
    table: Any = None
    create_dialog: Any = None
    edit_dialog: Any = None
    confirm_delete_dialog: Any = None
    confirm_message: Any = None
    create_account_select: Any = None
    create_type_select: Any = None
    create_amount_input: Any = None
    create_date_input: Any = None
    create_note_input: Any = None
    edit_account_select: Any = None
    edit_type_select: Any = None
    edit_amount_input: Any = None
    edit_date_input: Any = None
    edit_note_input: Any = None


class OperationsPage(BasePage, CrudPageMixin):
    """Страница операций по счёту (``/operations``).

    Именование сущностей выбрано в женском роде ед. числа; `BasePage` указан
    первым в списке базовых классов, поэтому его ``render``/``register`` не
    перекрываются методами миксина.
    """

    path = "/operations"

    #: Морфология сообщений CRUD (см. :class:`CrudPageMixin`).
    entity_title: ClassVar[str] = "Операция"
    entity_accusative: ClassVar[str] = "операцию"
    entity_genitive: ClassVar[str] = "операции"
    selected_verb: ClassVar[str] = "Выбрана операция:"
    no_selection_text: ClassVar[str] = "Операция не выбрана — кликните по строке в таблице"
    name_required_message: ClassVar[str] = "Сумма должна быть больше нуля"
    created_suffix: ClassVar[str] = "добавлена"
    updated_suffix: ClassVar[str] = "обновлена"
    deleted_message: ClassVar[str] = "Операция удалена"
    not_found_message: ClassVar[str] = "Операция не найдена"
    delete_guard_error: ClassVar[type[Exception] | tuple[type[Exception], ...]] = ()

    def __init__(
        self,
        jwt_service: JwtService,
        account_operation_service: AccountOperationService,
        broker_service: BrokerService,
    ) -> None:
        super().__init__(jwt_service)
        self.account_operation_service = account_operation_service
        self.broker_service = broker_service

    # -- Хуки CRUD ----------------------------------------------------------

    async def _load_rows(self, state: _OperationsState) -> list[dict[str, Any]]:
        """Загрузить операции пользователя и обновить кэш для формы правки."""
        operations = await self.account_operation_service.list_for_user(state.owner_id)
        accounts = {
            acc.id: f"{acc.broker_name} / {acc.name}"
            for acc in await self.broker_service.list_accounts_for_user(state.owner_id)
        }
        state.cache.clear()
        state.cache.update({op.id: op for op in operations})
        return [
            {
                "id": op.id,
                "date": fmt_date(op.date),
                "account": accounts.get(op.broker_account_id, f"#{op.broker_account_id}"),
                "type": _TYPE_OPTIONS.get(op.type, op.type),
                "amount": fmt_money(op.amount),
                "note": op.note or "—",
            }
            for op in operations
        ]

    def _fill_edit_form(self, state: _OperationsState, entity: Any) -> None:
        """Заполнить форму правки значениями выбранной операции."""
        state.edit_account_select.value = entity.broker_account_id
        state.edit_type_select.value = entity.type
        state.edit_amount_input.value = float(entity.amount)
        state.edit_date_input.value = entity.date.isoformat()
        state.edit_note_input.value = entity.note or ""
        state.confirm_message.set_text(
            f"Вы уверены, что хотите удалить {self.entity_accusative} "
            f"'{_TYPE_OPTIONS.get(entity.type, entity.type)} {entity.amount:.2f}'?"
        )

    def _entity_name(self, entity: Any) -> str:
        """Краткое имя операции — «тип сумма»."""
        return f"{_TYPE_OPTIONS.get(entity.type, entity.type)} {entity.amount:.2f}"

    def _entity_caption(self, entity: Any) -> str:
        """Полная подпись операции в label после выбора в таблице."""
        return f"{fmt_date(entity.date)} — {self._entity_name(entity)}"

    def _build_create_dto(self, state: _OperationsState) -> AccountOperationCreate:
        """Собрать ``AccountOperationCreate`` из полей формы создания."""
        return AccountOperationCreate(
            broker_account_id=int(state.create_account_select.value),
            type=state.create_type_select.value,
            amount=float(state.create_amount_input.value or 0),
            date=parse_date(state.create_date_input.value, "Дата операции"),
            note=state.create_note_input.value or None,
        )

    def _build_update_dto(self, state: _OperationsState) -> AccountOperationUpdate:
        """Собрать ``AccountOperationUpdate`` из полей формы правки."""
        return AccountOperationUpdate(
            broker_account_id=int(state.edit_account_select.value),
            type=state.edit_type_select.value,
            amount=float(state.edit_amount_input.value or 0),
            date=parse_date(state.edit_date_input.value, "Дата операции"),
            note=state.edit_note_input.value or None,
        )

    def _prevalidate_create(self, state: _OperationsState) -> str | None:
        """Счёт обязателен, сумма должна быть положительной."""
        if state.create_account_select.value is None:
            return "Выберите брокерский счёт"
        if not state.create_amount_input.value or state.create_amount_input.value <= 0:
            return self.name_required_message
        return None

    def _prevalidate_update(self, state: _OperationsState) -> str | None:
        """Счёт обязателен, сумма должна быть положительной."""
        if state.edit_account_select.value is None:
            return "Выберите брокерский счёт"
        if not state.edit_amount_input.value or state.edit_amount_input.value <= 0:
            return self.name_required_message
        return None

    async def _service_create(self, state: _OperationsState, dto: AccountOperationCreate) -> Any:
        """Создать операцию через сервис и залогировать факт создания."""
        operation = await self.account_operation_service.create(dto, user_id=state.owner_id)
        logger.info(
            "UI: создана операция id=%s type=%s amount=%s",
            operation.id,
            operation.type,
            operation.amount,
        )
        return operation

    async def _service_update(
        self, state: _OperationsState, entity_id: int, dto: AccountOperationUpdate
    ) -> Any:
        """Обновить операцию через сервис."""
        return await self.account_operation_service.update(entity_id, dto, user_id=state.owner_id)

    async def _service_delete(self, state: _OperationsState, entity_id: int) -> bool:
        """Удалить операцию через сервис и залогировать факт удаления."""
        deleted = await self.account_operation_service.delete(entity_id, user_id=state.owner_id)
        if deleted:
            logger.info("UI: удалена операция id=%s", entity_id)
        return deleted

    def _reset_create_form(self, state: _OperationsState) -> None:
        state.create_account_select.value = None
        state.create_type_select.value = "DEPOSIT"
        state.create_amount_input.value = None
        state.create_date_input.value = None
        state.create_note_input.value = None

    # -- Страница ------------------------------------------------------------

    async def render(self, user: TokenPayload) -> None:
        """Таблица операций и модальные окна создания/правки/удаления."""
        #: State создаётся до построения виджетов, чтобы обработчики (связанные
        #: через ``functools.partial``) ссылались на уже существующий объект
        #: ещё в момент построения кнопок.
        state = _OperationsState(owner_id=user.user_id, cache={}, selected_id=None)

        try:
            accounts = await self.broker_service.list_accounts_for_user(user.user_id)
        except Exception as exc:
            notify_error(exc)
            accounts = []
        state.account_options = {acc.id: f"{acc.broker_name} / {acc.name}" for acc in accounts}

        page_header("/operations")
        with ui.column().classes("w-full gap-6"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Операции").classes("text-h5")
                ui.button(
                    "Добавить", icon="add", on_click=partial(self._open_create_dialog, state)
                ).props("color=primary")

            table = ui.table(columns=_COLUMNS, rows=[], row_key="id")
            style_table(table)
            state.table = table

        with ui.dialog() as create_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.create_dialog = create_dialog
            ui.label("Новая операция").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                state.create_account_select = ui.select(
                    state.account_options, label="Брокер / Счёт"
                )
                state.create_type_select = ui.select(
                    _TYPE_OPTIONS, value="DEPOSIT", label="Тип операции"
                )
                state.create_amount_input = ui.number("Сумма", min=0)
                state.create_date_input = ui.date_input("Дата операции")
                state.create_note_input = ui.input("Примечание (необязательно)")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Готово", icon="check", on_click=partial(self._handle_create, state))
                ui.button("Отмена", on_click=create_dialog.close).props("flat")

        with ui.dialog() as edit_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.edit_dialog = edit_dialog
            ui.label("Редактирование операции").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                state.edit_account_select = ui.select(state.account_options, label="Брокер / Счёт")
                state.edit_type_select = ui.select(_TYPE_OPTIONS, label="Тип операции")
                state.edit_amount_input = ui.number("Сумма", min=0)
                state.edit_date_input = ui.date_input("Дата операции")
                state.edit_note_input = ui.input("Примечание (необязательно)")
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

        table.on("rowClick", partial(self._on_table_select, state))

        await self._reload(state)
