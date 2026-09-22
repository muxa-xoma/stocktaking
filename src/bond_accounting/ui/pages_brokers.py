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
from bond_accounting.ui.common import page_header, style_table
from bond_accounting.ui.crud_mixin import CrudPageMixin

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.brokers.service import BrokerService

logger = logging.getLogger(__name__)

#: Варианты интерпретации минимальной комиссии: значение DTO -> подпись в UI.
_MIN_COMMISSION_TYPE_OPTIONS: dict[str, str] = {
    "PERCENT": "Процент",
    "RUBLES": "Рубли",
}

#: Подсказка к полю минимальной комиссии: смысл значения зависит от типа.
_MIN_COMMISSION_HINT = (
    "Значение зависит от типа мин. комиссии: "
    "процент от суммы сделки или фиксированная сумма в рублях"
)

_COLUMNS: list[dict[str, Any]] = [
    {"name": "name", "label": "Название", "field": "name", "align": "left", "sortable": True},
    {"name": "commission", "label": "Комиссия, %", "field": "commission", "align": "right"},
    {
        "name": "min_commission",
        "label": "Мин. комиссия",
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
    (поля ``cache``, ``selected_id``, ``table``, ``create_dialog``,
    ``edit_dialog``, ``confirm_delete_dialog`` — часть интерфейса миксина).
    Виджетные поля заполняются по мере построения страницы в
    :meth:`render`.
    """

    cache: dict[int, Any]
    selected_id: int | None
    table: Any = None
    create_dialog: Any = None
    edit_dialog: Any = None
    confirm_delete_dialog: Any = None
    confirm_message: Any = None
    name_input: Any = None
    commission_input: Any = None
    min_commission_input: Any = None
    min_commission_type_select: Any = None
    description_input: Any = None
    edit_name: Any = None
    edit_commission: Any = None
    edit_min_commission: Any = None
    edit_min_commission_type: Any = None
    edit_description: Any = None


def _fmt_min_commission(value: float | None, commission_type: str) -> str:
    """Форматировать минимальную комиссию с единицей, зависящей от типа."""
    if value is None:
        return "—"
    return f"{value:.2f} ₽" if commission_type == "RUBLES" else f"{value:.2f}%"


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
    no_selection_text: ClassVar[str] = "Брокер не выбран — кликните по строке в таблице"
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
                "commission": f"{broker.commission:.2f}%",
                "min_commission": _fmt_min_commission(
                    broker.min_commission,
                    getattr(broker, "min_commission_type", "PERCENT"),
                ),
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
        state.edit_min_commission_type.value = getattr(entity, "min_commission_type", "PERCENT")
        state.edit_description.value = entity.description or ""
        state.confirm_message.set_text(
            f"Вы уверены, что хотите удалить {self.entity_accusative} '{entity.name}'?"
        )

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
            min_commission_type=state.min_commission_type_select.value or "PERCENT",
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
            min_commission_type=state.edit_min_commission_type.value or None,
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

    def _reset_create_form(self, state: _BrokersState) -> None:
        state.name_input.value = None
        state.commission_input.value = 0
        state.min_commission_input.value = None
        state.min_commission_type_select.value = "PERCENT"
        state.description_input.value = None

    # -- Страница ------------------------------------------------------------

    async def render(self, user: TokenPayload) -> None:
        """Таблица брокеров и модальные окна создания/правки/удаления."""
        #: State создаётся до построения виджетов, чтобы обработчики (связанные
        #: через ``functools.partial``) ссылались на уже существующий объект
        #: ещё в момент построения кнопок.
        state = _BrokersState(cache={}, selected_id=None)

        page_header("/brokers")
        with ui.column().classes("w-full gap-6"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Брокеры").classes("text-h5")
                ui.button(
                    "Добавить", icon="add", on_click=partial(self._open_create_dialog, state)
                ).props("color=primary")

            table = ui.table(columns=_COLUMNS, rows=[], row_key="id")
            style_table(table)
            state.table = table

        with ui.dialog() as create_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.create_dialog = create_dialog
            ui.label("Новый брокер").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                state.name_input = ui.input("Название")
                state.commission_input = ui.number("Комиссия, %", value=0, min=0)
                state.min_commission_input = ui.number("Мин. комиссия", min=0).tooltip(
                    _MIN_COMMISSION_HINT
                )
                state.min_commission_type_select = ui.select(
                    _MIN_COMMISSION_TYPE_OPTIONS, value="PERCENT", label="Тип мин. комиссии"
                )
                state.description_input = ui.input("Описание (необязательно)")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Готово", icon="check", on_click=partial(self._handle_create, state))
                ui.button("Отмена", on_click=create_dialog.close).props("flat")

        with ui.dialog() as edit_dialog, ui.card().classes("bg-[#1e293b] text-white w-96"):
            state.edit_dialog = edit_dialog
            ui.label("Редактирование брокера").classes("text-h6")
            with ui.column().classes("w-full gap-2"):
                state.edit_name = ui.input("Название")
                state.edit_commission = ui.number("Комиссия, %", min=0)
                state.edit_min_commission = ui.number("Мин. комиссия", min=0).tooltip(
                    _MIN_COMMISSION_HINT
                )
                state.edit_min_commission_type = ui.select(
                    _MIN_COMMISSION_TYPE_OPTIONS, value="PERCENT", label="Тип мин. комиссии"
                )
                state.edit_description = ui.input("Описание (необязательно)")
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
