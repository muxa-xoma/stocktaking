"""Страница брокеров (``/brokers``): создание, просмотр, правка, удаление."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from nicegui import ui

from bond_accounting.brokers.dto import BrokerCreate, BrokerUpdate
from bond_accounting.brokers.exceptions import BrokerHasAccountsError
from bond_accounting.ui.base_page import BasePage
from bond_accounting.ui.common import fmt_raw_percent, page_header
from bond_accounting.ui.crud_mixin import CrudPageMixin

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.brokers.service import BrokerService

logger = logging.getLogger(__name__)

_COLUMNS: list[dict[str, Any]] = [
    {"name": "name", "label": "Название", "field": "name", "align": "left", "sortable": True},
    {"name": "commission", "label": "Комиссия, %", "field": "commission", "align": "right"},
    {
        "name": "min_commission",
        "label": "Мин. комиссия, %",
        "field": "min_commission",
        "align": "right",
    },
    {"name": "description", "label": "Описание", "field": "description", "align": "left"},
]


@dataclass
class _BrokersState:
    """Локальное состояние страницы брокеров: виджеты, кэш и текущий выбор.

    Создаётся в :meth:`BrokersPage.render` в локальной области видимости и
    передаётся первым аргументом в общие методы :class:`CrudPageMixin`
    (поля ``cache``, ``selected_id``, ``table``, ``selected_label``,
    ``delete_dialog`` — часть интерфейса миксина). Виджетные поля заполняются
    по мере построения страницы в :meth:`render`.
    """

    cache: dict[int, Any]
    selected_id: int | None
    table: Any = None
    selected_label: Any = None
    name_input: Any = None
    commission_input: Any = None
    min_commission_input: Any = None
    description_input: Any = None
    edit_name: Any = None
    edit_commission: Any = None
    edit_min_commission: Any = None
    edit_description: Any = None
    delete_dialog: Any = None


class BrokersPage(BasePage, CrudPageMixin):
    """Страница брокеров (``/brokers``) — общий справочник.

    Именование сущностей выбрано в мужском роде ед. числа; `BasePage` указан
    первым в списке базовых классов, поэтому его ``render``/``register`` не
    перекрываются методами миксина.
    """

    path = "/brokers"

    #: Морфология сообщений CRUD (см. :class:`CrudPageMixin`).
    entity_title: ClassVar[str] = "Брокер"
    entity_accusative: ClassVar[str] = "брокера"
    entity_genitive: ClassVar[str] = "брокера"
    selected_verb: ClassVar[str] = "Выбран брокер:"
    no_selection_text: ClassVar[str] = "Брокер не выбран — отметьте строку в таблице"
    name_required_message: ClassVar[str] = "Название обязательно"
    created_suffix: ClassVar[str] = "добавлен"
    updated_suffix: ClassVar[str] = "обновлён"
    deleted_message: ClassVar[str] = "Брокер удалён"
    not_found_message: ClassVar[str] = "Брокер не найден"
    delete_guard_error: ClassVar[type[Exception] | tuple[type[Exception], ...]] = (
        BrokerHasAccountsError,
    )

    def __init__(self, jwt_service: JwtService, broker_service: BrokerService) -> None:
        super().__init__(jwt_service)
        self.broker_service = broker_service

    # -- Хуки CRUD ----------------------------------------------------------

    async def _load_rows(self, state: _BrokersState) -> list[dict[str, Any]]:
        """Загрузить брокеров и обновить кэш для формы правки."""
        brokers = await self.broker_service.list_all_brokers()
        state.cache.clear()
        state.cache.update({broker.id: broker for broker in brokers})
        return [
            {
                "id": broker.id,
                "name": broker.name,
                "commission": fmt_raw_percent(broker.commission),
                "min_commission": fmt_raw_percent(broker.min_commission),
                "description": broker.description or "—",
            }
            for broker in brokers
        ]

    def _fill_edit_form(self, state: _BrokersState, entity: Any) -> None:
        """Заполнить форму правки значениями выбранного брокера."""
        state.edit_name.value = entity.name
        state.edit_commission.value = entity.commission
        state.edit_min_commission.value = (
            entity.min_commission if entity.min_commission is not None else None
        )
        state.edit_description.value = entity.description or ""

    def _entity_name(self, entity: Any) -> str:
        """Краткое имя брокера для сообщений о создании/обновлении."""
        return entity.name

    def _entity_caption(self, entity: Any) -> str:
        """Полная подпись брокера в label после выбора в таблице."""
        return entity.name

    def _build_create_dto(self, state: _BrokersState) -> BrokerCreate:
        """Собрать ``BrokerCreate`` из полей формы создания."""
        return BrokerCreate(
            name=state.name_input.value or "",
            commission=float(state.commission_input.value or 0),
            min_commission=float(state.min_commission_input.value)
            if state.min_commission_input.value is not None
            else None,
            description=state.description_input.value or None,
        )

    def _build_update_dto(self, state: _BrokersState) -> BrokerUpdate:
        """Собрать ``BrokerUpdate`` из полей формы правки."""
        return BrokerUpdate(
            name=state.edit_name.value,
            commission=float(state.edit_commission.value)
            if state.edit_commission.value is not None
            else None,
            min_commission=float(state.edit_min_commission.value)
            if state.edit_min_commission.value is not None
            else None,
            description=state.edit_description.value or None,
        )

    def _prevalidate_update(self, state: _BrokersState) -> str | None:
        """Название обязательно при сохранении изменений."""
        if not state.edit_name.value:
            return self.name_required_message
        return None

    async def _service_create(self, state: _BrokersState, dto: BrokerCreate) -> Any:
        """Создать брокера через сервис и залогировать факт создания."""
        broker = await self.broker_service.create_broker(dto)
        logger.info("UI: создан брокер name=%s", broker.name)
        return broker

    async def _service_update(self, state: _BrokersState, entity_id: int, dto: BrokerUpdate) -> Any:
        """Обновить брокера через сервис."""
        return await self.broker_service.update_broker(entity_id, dto)

    async def _service_delete(self, state: _BrokersState, entity_id: int) -> bool:
        """Удалить брокера через сервис и залогировать факт удаления."""
        deleted = await self.broker_service.delete_broker(entity_id)
        if deleted:
            logger.info("UI: удалён брокер id=%s", entity_id)
        return deleted

    # -- Страница ------------------------------------------------------------

    async def render(self, user: TokenPayload) -> None:
        """Таблица брокеров, форма создания и форма правки/удаления выбранного."""
        #: State создаётся до построения виджетов, чтобы обработчики (связанные
        #: через ``functools.partial``) ссылались на уже существующий объект
        #: ещё в момент построения кнопок.
        state = _BrokersState(cache={}, selected_id=None)

        page_header("/brokers")
        with ui.column().classes("w-full max-w-5xl mx-auto gap-6"):
            ui.label("Брокеры").classes("text-h5")
            table = ui.table(
                columns=_COLUMNS,
                rows=[],
                row_key="id",
                selection="single",
            ).classes("w-full")
            state.table = table

            with ui.card().classes("w-full"):
                ui.label("Новый брокер").classes("text-subtitle1")
                with ui.row().classes("w-full items-start gap-2"):
                    state.name_input = ui.input("Название")
                    state.commission_input = ui.number("Комиссия, %", value=0, min=0)
                    state.min_commission_input = ui.number("Мин. комиссия, %", min=0)
                    state.description_input = ui.input("Описание (необязательно)")
                ui.button("Добавить", icon="add", on_click=partial(self._handle_create, state))

            with ui.card().classes("w-full"):
                state.selected_label = ui.label(
                    "Брокер не выбран — отметьте строку в таблице"
                ).classes("text-subtitle1")
                with ui.row().classes("w-full items-start gap-2"):
                    state.edit_name = ui.input("Название")
                    state.edit_commission = ui.number("Комиссия, %", min=0)
                    state.edit_min_commission = ui.number("Мин. комиссия, %", min=0)
                    state.edit_description = ui.input("Описание (необязательно)")
                with ui.row().classes("gap-2"):
                    ui.button(
                        "Сохранить изменения",
                        icon="save",
                        on_click=partial(self._handle_update, state),
                    )
                    ui.button(
                        "Удалить", icon="delete", on_click=partial(self._handle_delete, state)
                    ).props("color=negative")

            with ui.dialog() as delete_dialog, ui.card():
                state.delete_dialog = delete_dialog
                ui.label("Удалить выбранного брокера?")
                with ui.row():
                    ui.button("Удалить", on_click=partial(self._confirm_delete, state)).props(
                        "color=negative"
                    )
                    ui.button("Отмена", on_click=delete_dialog.close).props("flat")

        table.on_select(partial(self._on_table_select, state))

        await self._reload(state)
