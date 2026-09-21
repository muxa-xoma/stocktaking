"""Страница справочника облигаций (``/bonds``): создание, просмотр, правка, удаление."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from nicegui import ui

from bond_accounting.bonds.dto import BondCreate, BondUpdate
from bond_accounting.ui.base_page import BasePage
from bond_accounting.ui.common import fmt_date, page_header, parse_date
from bond_accounting.ui.crud_mixin import CrudPageMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.bonds.service import BondService

logger = logging.getLogger(__name__)

#: Варианты частоты выплаты купона: значение DTO -> подпись в UI.
_FREQUENCY_OPTIONS: dict[str, str] = {
    "ANNUAL": "Ежегодно",
    "SEMI_ANNUAL": "Раз в полгода",
    "QUARTERLY": "Ежеквартально",
}

_COLUMNS: list[dict[str, Any]] = [
    {"name": "isin", "label": "ISIN", "field": "isin", "align": "left", "sortable": True},
    {"name": "name", "label": "Название", "field": "name", "align": "left"},
    {"name": "nominal", "label": "Номинал", "field": "nominal", "align": "right"},
    {"name": "coupon_rate", "label": "Купон, %", "field": "coupon_rate", "align": "right"},
    {
        "name": "coupon_frequency",
        "label": "Частота купона",
        "field": "coupon_frequency",
        "align": "left",
    },
    {"name": "maturity_date", "label": "Погашение", "field": "maturity_date", "align": "right"},
    {"name": "issuer", "label": "Эмитент", "field": "issuer", "align": "left"},
]

_ISIN_RULES: dict[str, Callable[[Any], bool]] = {
    "ISIN — ровно 12 символов (A-Z, 0-9)": lambda value: _is_valid_isin(value),
}


@dataclass
class _BondsState:
    """Локальное состояние страницы облигаций: виджеты, кэш и текущий выбор.

    Создаётся в :meth:`BondsPage.render` в локальной области видимости и
    передаётся первым аргументом в общие методы :class:`CrudPageMixin`
    (поля ``cache``, ``selected_id``, ``table``, ``selected_label``,
    ``delete_dialog`` — часть интерфейса миксина). Виджетные поля заполняются
    по мере построения страницы в :meth:`render`.
    """

    owner_id: int
    cache: dict[int, Any]
    selected_id: int | None
    table: Any = None
    selected_label: Any = None
    isin_input: Any = None
    name_input: Any = None
    nominal_input: Any = None
    coupon_rate_input: Any = None
    frequency_select: Any = None
    maturity_input: Any = None
    issuer_input: Any = None
    edit_name: Any = None
    edit_nominal: Any = None
    edit_rate: Any = None
    edit_frequency: Any = None
    edit_maturity: Any = None
    edit_issuer: Any = None
    delete_dialog: Any = None


def _is_valid_isin(value: str | None) -> bool:
    """Проверить форму ISIN: 12 символов из заглавных латинских букв и цифр."""
    return (
        value is not None
        and len(value) == 12
        and value.isascii()
        and value.isalnum()
        and value.isupper()
    )


class BondsPage(BasePage, CrudPageMixin):
    """Страница справочника облигаций (``/bonds``).

    Именование сущностей выбрано в женском роде ед. числа; `BasePage` указан
    первым в списке базовых классов, поэтому его ``render``/``register`` не
    перекрываются методами миксина.
    """

    path = "/bonds"

    #: Морфология сообщений CRUD (см. :class:`CrudPageMixin`).
    entity_title: ClassVar[str] = "Облигация"
    entity_accusative: ClassVar[str] = "облигацию"
    entity_genitive: ClassVar[str] = "облигации"
    selected_verb: ClassVar[str] = "Выбрана облигация:"
    no_selection_text: ClassVar[str] = "Облигация не выбрана — отметьте строку в таблице"
    name_required_message: ClassVar[str] = "Название обязательно"
    created_suffix: ClassVar[str] = "добавлена"
    updated_suffix: ClassVar[str] = "обновлена"
    deleted_message: ClassVar[str] = "Облигация удалена"
    not_found_message: ClassVar[str] = "Облигация не найдена"
    delete_guard_error: ClassVar[type[Exception] | tuple[type[Exception], ...]] = ()

    def __init__(self, jwt_service: JwtService, bond_service: BondService) -> None:
        super().__init__(jwt_service)
        self.bond_service = bond_service

    # -- Хуки CRUD ----------------------------------------------------------

    async def _load_rows(self, state: _BondsState) -> list[dict[str, Any]]:
        """Загрузить облигации и обновить кэш для формы правки."""
        bonds = await self.bond_service.list_all()
        state.cache.clear()
        state.cache.update({bond.id: bond for bond in bonds})
        return [
            {
                "id": bond.id,
                "isin": bond.isin,
                "name": bond.name,
                "nominal": bond.nominal,
                "coupon_rate": bond.coupon_rate,
                "coupon_frequency": _FREQUENCY_OPTIONS.get(
                    bond.coupon_frequency, bond.coupon_frequency
                ),
                "maturity_date": fmt_date(bond.maturity_date),
                "issuer": bond.issuer or "—",
            }
            for bond in bonds
        ]

    def _fill_edit_form(self, state: _BondsState, entity: Any) -> None:
        """Заполнить форму правки значениями выбранной облигации."""
        state.edit_name.value = entity.name
        state.edit_nominal.value = float(entity.nominal)
        state.edit_rate.value = entity.coupon_rate
        state.edit_frequency.value = entity.coupon_frequency
        state.edit_maturity.value = entity.maturity_date.isoformat()
        state.edit_issuer.value = entity.issuer or ""

    def _entity_name(self, entity: Any) -> str:
        """Краткое имя облигации — её ISIN."""
        return entity.isin

    def _entity_caption(self, entity: Any) -> str:
        """Полная подпись облигации в label после выбора в таблице."""
        return f"{entity.isin} — {entity.name}"

    def _build_create_dto(self, state: _BondsState) -> BondCreate:
        """Собрать ``BondCreate`` из полей формы создания."""
        return BondCreate(
            isin=state.isin_input.value or "",
            name=state.name_input.value or "",
            nominal=int(state.nominal_input.value or 0),
            coupon_rate=float(state.coupon_rate_input.value or 0),
            coupon_frequency=state.frequency_select.value,
            maturity_date=parse_date(state.maturity_input.value, "Дата погашения"),
            issuer=state.issuer_input.value or None,
        )

    def _build_update_dto(self, state: _BondsState) -> BondUpdate:
        """Собрать ``BondUpdate`` из полей формы правки."""
        return BondUpdate(
            name=state.edit_name.value,
            nominal=int(state.edit_nominal.value) if state.edit_nominal.value is not None else None,
            coupon_rate=state.edit_rate.value,
            coupon_frequency=state.edit_frequency.value,
            maturity_date=(
                parse_date(state.edit_maturity.value, "Дата погашения")
                if state.edit_maturity.value
                else None
            ),
            issuer=state.edit_issuer.value or None,
        )

    def _prevalidate_update(self, state: _BondsState) -> str | None:
        """Название обязательно при сохранении изменений."""
        if not state.edit_name.value:
            return self.name_required_message
        return None

    async def _service_create(self, state: _BondsState, dto: BondCreate) -> Any:
        """Создать облигацию через сервис и залогировать факт создания."""
        bond = await self.bond_service.create(dto, user_id=state.owner_id)
        logger.info("UI: создана облигация isin=%s", bond.isin)
        return bond

    async def _service_update(self, state: _BondsState, entity_id: int, dto: BondUpdate) -> Any:
        """Обновить облигацию через сервис."""
        return await self.bond_service.update(entity_id, dto, user_id=state.owner_id)

    async def _service_delete(self, state: _BondsState, entity_id: int) -> bool:
        """Удалить облигацию через сервис и залогировать факт удаления."""
        deleted = await self.bond_service.delete(entity_id, user_id=state.owner_id)
        if deleted:
            logger.info("UI: удалена облигация id=%s", entity_id)
        return deleted

    # -- Страница ------------------------------------------------------------

    async def render(self, user: TokenPayload) -> None:
        """Таблица облигаций, форма создания и форма правки/удаления выбранной."""
        #: State создаётся до построения виджетов, чтобы обработчики (связанные
        #: через ``functools.partial``) ссылались на уже существующий объект
        #: ещё в момент построения кнопок.
        state = _BondsState(owner_id=user.user_id, cache={}, selected_id=None)

        page_header("/bonds")
        with ui.column().classes("w-full max-w-5xl mx-auto gap-6"):
            ui.label("Облигации").classes("text-h5")
            table = ui.table(
                columns=_COLUMNS,
                rows=[],
                row_key="id",
                selection="single",
            ).classes("w-full")
            state.table = table

            with ui.card().classes("w-full"):
                ui.label("Новая облигация").classes("text-subtitle1")
                with ui.row().classes("w-full items-start gap-2"):
                    state.isin_input = ui.input("ISIN", validation=_ISIN_RULES)
                    state.name_input = ui.input("Название")
                    state.nominal_input = ui.number("Номинал", value=1000, min=1)
                    state.coupon_rate_input = ui.number("Купонная ставка, %")
                    state.frequency_select = ui.select(
                        _FREQUENCY_OPTIONS, value="ANNUAL", label="Частота купона"
                    )
                    state.maturity_input = ui.date_input("Дата погашения")
                    state.issuer_input = ui.input("Эмитент (необязательно)")
                ui.button("Добавить", icon="add", on_click=partial(self._handle_create, state))

            with ui.card().classes("w-full"):
                state.selected_label = ui.label(
                    "Облигация не выбрана — отметьте строку в таблице"
                ).classes("text-subtitle1")
                with ui.row().classes("w-full items-start gap-2"):
                    state.edit_name = ui.input("Название")
                    state.edit_nominal = ui.number("Номинал")
                    state.edit_rate = ui.number("Купонная ставка, %")
                    state.edit_frequency = ui.select(_FREQUENCY_OPTIONS, label="Частота купона")
                    state.edit_maturity = ui.date_input("Дата погашения")
                    state.edit_issuer = ui.input("Эмитент (необязательно)")
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
                ui.label("Удалить выбранную облигацию?")
                with ui.row():
                    ui.button("Удалить", on_click=partial(self._confirm_delete, state)).props(
                        "color=negative"
                    )
                    ui.button("Отмена", on_click=delete_dialog.close).props("flat")

        table.on_select(partial(self._on_table_select, state))

        await self._reload(state)
