"""Страница справочника облигаций (``/bonds``): создание, просмотр, правка, удаление."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nicegui import ui
from pydantic import ValidationError

from bond_accounting.bonds.dto import BondCreate, BondUpdate
from bond_accounting.ui.common import (
    fmt_date,
    get_current_user,
    notify_error,
    page_header,
    parse_date,
)

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService
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

_ISIN_RULES = {
    "ISIN — ровно 12 символов (A-Z, 0-9)": lambda value: _is_valid_isin(value),
}


def _is_valid_isin(value: str | None) -> bool:
    """Проверить форму ISIN: 12 символов из заглавных латинских букв и цифр."""
    return (
        value is not None
        and len(value) == 12
        and value.isascii()
        and value.isalnum()
        and value.isupper()
    )


def register_bond_pages(jwt_service: JwtService, bond_service: BondService) -> None:
    """Зарегистрировать страницу ``/bonds`` с полным CRUD по облигациям.

    Args:
        jwt_service: Сервис проверки JWT из cookie.
        bond_service: Сервис CRUD облигаций.
    """

    @ui.page("/bonds")
    async def bonds_page() -> None:
        """Таблица облигаций, форма создания и форма правки/удаления выбранной."""
        user = get_current_user(jwt_service)
        if user is None:
            return

        #: Текущий пользователь — владелец создаваемых/изменяемых облигаций.
        owner_id = user.user_id

        #: Кэш загруженных облигаций по id — источник сырых значений для правки.
        bonds_cache: dict[int, Any] = {}
        selected_bond_id: int | None = None

        async def load_rows() -> list[dict[str, Any]]:
            """Загрузить облигации и обновить кэш для формы правки."""
            bonds = await bond_service.list_all()
            bonds_cache.clear()
            bonds_cache.update({bond.id: bond for bond in bonds})
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

        async def reload() -> None:
            """Перезагрузить таблицу и сбросить выбор облигации."""
            nonlocal selected_bond_id
            try:
                table.rows = await load_rows()
            except Exception as exc:
                notify_error(exc)
                return
            selected_bond_id = None
            table.selected = []
            selected_label.set_text("Облигация не выбрана — отметьте строку в таблице")

        def on_table_select(e: Any) -> None:
            """Заполнить форму правки при выборе строки таблицы."""
            nonlocal selected_bond_id
            selection = e.selection
            if not selection:
                selected_bond_id = None
                return
            row = selection[0]
            bond = bonds_cache.get(row["id"])
            if bond is None:
                selected_bond_id = None
                return
            selected_bond_id = bond.id
            selected_label.set_text(f"Выбрана облигация: {bond.isin} — {bond.name}")
            edit_name.value = bond.name
            edit_nominal.value = float(bond.nominal)
            edit_rate.value = bond.coupon_rate
            edit_frequency.value = bond.coupon_frequency
            edit_maturity.value = bond.maturity_date.isoformat()
            edit_issuer.value = bond.issuer or ""

        async def handle_create() -> None:
            """Создать облигацию из формы создания."""
            try:
                bond = await bond_service.create(
                    BondCreate(
                        isin=isin_input.value or "",
                        name=name_input.value or "",
                        nominal=int(nominal_input.value or 0),
                        coupon_rate=float(coupon_rate_input.value or 0),
                        coupon_frequency=frequency_select.value,
                        maturity_date=parse_date(maturity_input.value, "Дата погашения"),
                        issuer=issuer_input.value or None,
                    ),
                    user_id=owner_id,
                )
            except (ValidationError, ValueError) as exc:
                ui.notify(f"Некорректные данные облигации: {exc}", type="negative")
                return
            except Exception as exc:
                notify_error(exc)
                return
            logger.info("UI: создана облигация isin=%s", bond.isin)
            ui.notify(f"Облигация {bond.isin} добавлена", type="positive")
            await reload()

        async def handle_update() -> None:
            """Сохранить изменения выбранной облигации."""
            if selected_bond_id is None:
                ui.notify("Сначала выберите облигацию в таблице", type="warning")
                return
            if not edit_name.value:
                ui.notify("Название обязательно", type="negative")
                return
            try:
                updated = await bond_service.update(
                    selected_bond_id,
                    BondUpdate(
                        name=edit_name.value,
                        nominal=int(edit_nominal.value) if edit_nominal.value is not None else None,
                        coupon_rate=edit_rate.value,
                        coupon_frequency=edit_frequency.value,
                        maturity_date=(
                            parse_date(edit_maturity.value, "Дата погашения")
                            if edit_maturity.value
                            else None
                        ),
                        issuer=edit_issuer.value or None,
                    ),
                    user_id=owner_id,
                )
            except (ValidationError, ValueError) as exc:
                ui.notify(f"Некорректные данные облигации: {exc}", type="negative")
                return
            except Exception as exc:
                notify_error(exc)
                return
            if updated is None:
                ui.notify("Облигация не найдена", type="warning")
                await reload()
                return
            ui.notify(f"Облигация {updated.isin} обновлена", type="positive")
            await reload()

        async def handle_delete() -> None:
            """Открыть подтверждение удаления выбранной облигации."""
            if selected_bond_id is None:
                ui.notify("Сначала выберите облигацию в таблице", type="warning")
                return
            delete_dialog.open()

        async def confirm_delete() -> None:
            """Удалить выбранную облигацию после подтверждения."""
            delete_dialog.close()
            if selected_bond_id is None:
                return
            try:
                deleted = await bond_service.delete(selected_bond_id, user_id=owner_id)
            except Exception as exc:
                notify_error(exc)
                return
            if deleted:
                logger.info("UI: удалена облигация id=%s", selected_bond_id)
                ui.notify("Облигация удалена", type="positive")
            else:
                ui.notify("Облигация не найдена", type="warning")
            await reload()

        page_header("/bonds")
        with ui.column().classes("w-full max-w-5xl mx-auto gap-6"):
            ui.label("Облигации").classes("text-h5")
            table = ui.table(
                columns=_COLUMNS,
                rows=[],
                row_key="id",
                selection="single",
                on_select=on_table_select,
            ).classes("w-full")

            with ui.card().classes("w-full"):
                ui.label("Новая облигация").classes("text-subtitle1")
                with ui.row().classes("w-full items-start gap-2"):
                    isin_input = ui.input("ISIN", validation=_ISIN_RULES)
                    name_input = ui.input("Название")
                    nominal_input = ui.number("Номинал", value=1000, min=1)
                    coupon_rate_input = ui.number("Купонная ставка, %")
                    frequency_select = ui.select(
                        _FREQUENCY_OPTIONS, value="ANNUAL", label="Частота купона"
                    )
                    maturity_input = ui.date_input("Дата погашения")
                    issuer_input = ui.input("Эмитент (необязательно)")
                ui.button("Добавить", icon="add", on_click=handle_create)

            with ui.card().classes("w-full"):
                selected_label = ui.label(
                    "Облигация не выбрана — отметьте строку в таблице"
                ).classes("text-subtitle1")
                with ui.row().classes("w-full items-start gap-2"):
                    edit_name = ui.input("Название")
                    edit_nominal = ui.number("Номинал")
                    edit_rate = ui.number("Купонная ставка, %")
                    edit_frequency = ui.select(_FREQUENCY_OPTIONS, label="Частота купона")
                    edit_maturity = ui.date_input("Дата погашения")
                    edit_issuer = ui.input("Эмитент (необязательно)")
                with ui.row().classes("gap-2"):
                    ui.button("Сохранить изменения", icon="save", on_click=handle_update)
                    ui.button("Удалить", icon="delete", on_click=handle_delete).props(
                        "color=negative"
                    )

            with ui.dialog() as delete_dialog, ui.card():
                ui.label("Удалить выбранную облигацию?")
                with ui.row():
                    ui.button("Удалить", on_click=confirm_delete).props("color=negative")
                    ui.button("Отмена", on_click=delete_dialog.close).props("flat")

        await reload()
