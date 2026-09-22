"""Страница брокерских счетов (``/accounts``): создание, просмотр, правка, удаление."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from nicegui import ui

from bond_accounting.brokers.dto import BrokerAccountCreate, BrokerAccountUpdate
from bond_accounting.brokers.exceptions import BrokerAccountHasTransactionsError
from bond_accounting.ui.base_page import BasePage
from bond_accounting.ui.common import fmt_date, notify_error, page_header, parse_date, style_table
from bond_accounting.ui.crud_mixin import CrudPageMixin

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.brokers.service import BrokerService

logger = logging.getLogger(__name__)

#: Варианты типа счёта: значение DTO -> подпись в UI.
_ACCOUNT_TYPE_OPTIONS: dict[str, str] = {
    "STANDARD": "Стандарт",
    "IIS": "ИИС",
    "LTD": "ЛДТ",
}

_COLUMNS: list[dict[str, Any]] = [
    {"name": "broker_name", "label": "Брокер", "field": "broker_name", "align": "left"},
    {"name": "name", "label": "Название", "field": "name", "align": "left", "sortable": True},
    {
        "name": "account_number",
        "label": "Номер счёта",
        "field": "account_number",
        "align": "left",
    },
    {"name": "account_type", "label": "Тип", "field": "account_type", "align": "left"},
    {"name": "opened_at", "label": "Открыт", "field": "opened_at", "align": "right"},
    {"name": "closed_at", "label": "Закрыт", "field": "closed_at", "align": "right"},
]


@dataclass
class _AccountsState:
    """Локальное состояние страницы счетов: виджеты, кэш и текущий выбор.

    Создаётся в :meth:`AccountsPage.render` в локальной области видимости и
    передаётся первым аргументом в общие методы :class:`CrudPageMixin`
    (поля ``cache``, ``selected_id``, ``table``, ``create_dialog``,
    ``edit_dialog``, ``confirm_delete_dialog`` — часть интерфейса миксина).
    Виджетные поля заполняются по мере построения страницы в
    :meth:`render`.
    """

    owner_id: int
    cache: dict[int, Any]
    selected_id: int | None
    table: Any = None
    create_dialog: Any = None
    edit_dialog: Any = None
    confirm_delete_dialog: Any = None
    confirm_message: Any = None
    broker_select: Any = None
    name_input: Any = None
    number_input: Any = None
    type_select: Any = None
    opened_at_input: Any = None
    edit_name: Any = None
    edit_number: Any = None
    edit_type: Any = None
    edit_opened_at: Any = None
    edit_closed_at: Any = None


class AccountsPage(BasePage, CrudPageMixin):
    """Страница брокерских счетов (``/accounts``).

    Именование сущностей выбрано в мужском роде ед. числа; `BasePage` указан
    первым в списке базовых классов, поэтому его ``render``/``register`` не
    перекрываются методами миксина.
    """

    path = "/accounts"

    #: Морфология сообщений CRUD (см. :class:`CrudPageMixin`).
    entity_title: ClassVar[str] = "Счёт"
    entity_accusative: ClassVar[str] = "счёт"
    entity_genitive: ClassVar[str] = "счёта"
    selected_verb: ClassVar[str] = "Выбран счёт:"
    no_selection_text: ClassVar[str] = "Счёт не выбран — кликните по строке в таблице"
    name_required_message: ClassVar[str] = "Название счёта обязательно"
    created_suffix: ClassVar[str] = "добавлен"
    updated_suffix: ClassVar[str] = "обновлён"
    deleted_message: ClassVar[str] = "Счёт удалён"
    not_found_message: ClassVar[str] = "Счёт не найден"
    delete_guard_error: ClassVar[type[Exception] | tuple[type[Exception], ...]] = (
        BrokerAccountHasTransactionsError,
    )

    def __init__(self, jwt_service: JwtService, broker_service: BrokerService) -> None:
        super().__init__(jwt_service)
        self.broker_service = broker_service

    # -- Хуки CRUD ----------------------------------------------------------

    async def _load_rows(self, state: _AccountsState) -> list[dict[str, Any]]:
        """Загрузить счета пользователя и обновить кэш для формы правки."""
        accounts = await self.broker_service.list_accounts_for_user(state.owner_id)
        state.cache.clear()
        state.cache.update({account.id: account for account in accounts})
        return [
            {
                "id": account.id,
                "broker_name": account.broker_name,
                "name": account.name,
                "account_number": account.account_number or "—",
                "account_type": _ACCOUNT_TYPE_OPTIONS.get(
                    account.account_type, account.account_type
                ),
                "opened_at": fmt_date(account.opened_at),
                "closed_at": fmt_date(account.closed_at),
            }
            for account in accounts
        ]

    def _fill_edit_form(self, state: _AccountsState, entity: Any) -> None:
        """Заполнить форму правки значениями выбранного счёта."""
        state.edit_name.value = entity.name
        state.edit_number.value = entity.account_number or ""
        state.edit_type.value = entity.account_type
        state.edit_opened_at.value = (
            entity.opened_at.isoformat() if entity.opened_at is not None else None
        )
        state.edit_closed_at.value = (
            entity.closed_at.isoformat() if entity.closed_at is not None else None
        )
        state.confirm_message.set_text(
            f"Вы уверены, что хотите удалить {self.entity_accusative} '{entity.name}'?"
        )

    def _entity_name(self, entity: Any) -> str:
        """Краткое имя счёта для сообщений о создании/обновлении."""
        return entity.name

    def _entity_caption(self, entity: Any) -> str:
        """Полная подпись счёта в label после выбора в таблице."""
        return f"{entity.broker_name} / {entity.name}"

    def _build_create_dto(self, state: _AccountsState) -> BrokerAccountCreate:
        """Собрать ``BrokerAccountCreate`` из полей формы создания."""
        return BrokerAccountCreate(
            broker_id=state.broker_select.value,
            name=state.name_input.value,
            account_number=state.number_input.value or None,
            account_type=state.type_select.value,
            opened_at=parse_date(state.opened_at_input.value, "Дата открытия")
            if state.opened_at_input.value
            else None,
        )

    def _build_update_dto(self, state: _AccountsState) -> BrokerAccountUpdate:
        """Собрать ``BrokerAccountUpdate`` из полей формы правки."""
        return BrokerAccountUpdate(
            name=state.edit_name.value,
            account_number=state.edit_number.value or None,
            account_type=state.edit_type.value,
            opened_at=parse_date(state.edit_opened_at.value, "Дата открытия")
            if state.edit_opened_at.value
            else None,
            closed_at=parse_date(state.edit_closed_at.value, "Дата закрытия")
            if state.edit_closed_at.value
            else None,
        )

    def _prevalidate_create(self, state: _AccountsState) -> str | None:
        """При создании счёта обязателен брокер и название."""
        if state.broker_select.value is None:
            return "Выберите брокера"
        if not state.name_input.value:
            return "Название счёта обязательно"
        return None

    def _prevalidate_update(self, state: _AccountsState) -> str | None:
        """Название счёта обязательно при сохранении изменений."""
        if not state.edit_name.value:
            return self.name_required_message
        return None

    async def _service_create(self, state: _AccountsState, dto: BrokerAccountCreate) -> Any:
        """Создать счёт через сервис и залогировать факт создания."""
        account = await self.broker_service.create_account(dto, user_id=state.owner_id)
        logger.info("UI: создан счёт id=%s user_id=%s", account.id, state.owner_id)
        return account

    async def _service_update(
        self, state: _AccountsState, entity_id: int, dto: BrokerAccountUpdate
    ) -> Any:
        """Обновить счёт через сервис."""
        return await self.broker_service.update_account(entity_id, dto, user_id=state.owner_id)

    async def _service_delete(self, state: _AccountsState, entity_id: int) -> bool:
        """Удалить счёт через сервис и залогировать факт удаления."""
        deleted = await self.broker_service.delete_account(entity_id, user_id=state.owner_id)
        if deleted:
            logger.info("UI: удалён счёт id=%s user_id=%s", entity_id, state.owner_id)
        return deleted

    def _reset_create_form(self, state: _AccountsState) -> None:
        state.broker_select.value = None
        state.name_input.value = None
        state.number_input.value = None
        state.type_select.value = "STANDARD"
        state.opened_at_input.value = None

    # -- Страница ------------------------------------------------------------

    async def render(self, user: TokenPayload) -> None:
        """Таблица счетов и модальные окна создания/правки/удаления."""
        #: State создаётся до построения виджетов, чтобы обработчики (связанные
        #: через ``functools.partial``) ссылались на уже существующий объект
        #: ещё в момент построения кнопок.
        state = _AccountsState(owner_id=user.user_id, cache={}, selected_id=None)

        try:
            brokers = await self.broker_service.list_all_brokers()
        except Exception as exc:
            notify_error(exc)
            brokers = []
        broker_options: dict[int, str] = {broker.id: broker.name for broker in brokers}

        page_header("/accounts")
        with ui.column().classes("w-full gap-6"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Брокерские счета").classes("text-h5")
                ui.button(
                    "Добавить", icon="add", on_click=partial(self._open_create_dialog, state)
                ).props("color=primary")

            table = ui.table(columns=_COLUMNS, rows=[], row_key="id")
            style_table(table)
            state.table = table

        with ui.dialog() as create_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.create_dialog = create_dialog
            ui.label("Новый счёт").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                state.broker_select = ui.select(broker_options, label="Брокер")
                state.name_input = ui.input("Название счёта")
                state.number_input = ui.input("Номер счёта (необязательно)")
                state.type_select = ui.select(_ACCOUNT_TYPE_OPTIONS, value="STANDARD", label="Тип")
                state.opened_at_input = ui.date_input("Дата открытия")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Готово", icon="check", on_click=partial(self._handle_create, state))
                ui.button("Отмена", on_click=create_dialog.close).props("flat")

        with ui.dialog() as edit_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.edit_dialog = edit_dialog
            ui.label("Редактирование счёта").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                state.edit_name = ui.input("Название счёта")
                state.edit_number = ui.input("Номер счёта")
                state.edit_type = ui.select(_ACCOUNT_TYPE_OPTIONS, label="Тип")
                state.edit_opened_at = ui.date_input("Дата открытия")
                state.edit_closed_at = ui.date_input("Дата закрытия")
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
