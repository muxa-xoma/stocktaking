"""UI-тесты страницы брокеров (``/brokers``): отмена в окне правки (F4).

Сервисы замоканы (``create_autospec``). Клик по строке таблицы (Quasar
``rowClick``) эмулируется прямой отправкой события с аргументами
``[evt, row, index]`` — как в ``test_operations.py``.
"""

from __future__ import annotations

import datetime
from typing import Any

from nicegui import ui

from bond_accounting.brokers.dto import BrokerDTO
from bond_accounting.brokers.exceptions import BrokerHasAccountsError
from tests.ui._ui_utils import (
    _fire,
    authenticate,
    click,
    elements,
    eventually,
    one,
    tables,
)


def make_broker(**overrides: Any) -> BrokerDTO:
    """Брокер по умолчанию: «Test Broker», комиссия 5%."""
    defaults: dict[str, Any] = {
        "id": 1,
        "name": "Test Broker",
        "commission": 5.0,
        "min_commission": 10.0,
        "min_commission_type": "RUBLES",
        "description": "Основной брокер",
        "created_at": datetime.datetime(2026, 1, 1, 0, 0),
    }
    return BrokerDTO(**{**defaults, **overrides})


def _dialogs(user: Any) -> tuple[Any, Any, Any]:
    """Диалоги страницы в порядке создания: создание, правка, подтверждение."""
    found = elements(user, kind=ui.dialog)
    assert len(found) == 3, f"expected 3 dialogs, found {len(found)}"
    return found[0], found[1], found[2]


def _click_row(user: Any, row: dict[str, Any]) -> None:
    """Кликнуть строку таблицы (Quasar rowClick: evt, row, index)."""
    [table] = tables(user)
    _fire(user, table, "rowClick", [{}, row, 0])


async def test_edit_dialog_cancel_button_closes(ui_user, valid_token, broker_service) -> None:
    """F4: в окне правки брокера есть кнопка «Отмена», клик закрывает окно."""
    broker_service.list_all_brokers.return_value = [make_broker()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/brokers")
    await ui_user.should_see("Брокеры")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    assert edit_dialog.value is False

    _click_row(ui_user, {"id": 1})
    assert edit_dialog.value

    cancel = one(ui_user, kind=ui.button, content="Отмена", within=edit_dialog)
    click(ui_user, cancel)
    assert edit_dialog.value is False


async def test_create_forwards_min_commission_type(ui_user, valid_token, broker_service) -> None:
    """Создание: выбор «Рубли» (RUBLES) в «Тип мин. комиссии» уходит в BrokerCreate."""
    broker_service.create_broker.return_value = make_broker(
        name="Новый Брокер", min_commission=25.0, min_commission_type="RUBLES"
    )
    broker_service.list_all_brokers.return_value = [
        make_broker(name="Новый Брокер", min_commission=25.0, min_commission_type="RUBLES")
    ]
    authenticate(ui_user, valid_token)

    await ui_user.open("/brokers")
    await ui_user.should_see("Брокеры")
    create_dialog, _edit_dialog, _confirm_dialog = _dialogs(ui_user)

    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))
    assert create_dialog.value

    one(ui_user, kind=ui.input, content="Название", within=create_dialog).value = "Новый Брокер"
    one(ui_user, kind=ui.number, content="Мин. комиссия", within=create_dialog).value = 25
    one(ui_user, kind=ui.select, content="Тип мин. комиссии", within=create_dialog).value = "RUBLES"

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await eventually(lambda: broker_service.create_broker.await_count == 1)
    dto = broker_service.create_broker.await_args.args[0]
    assert dto.min_commission_type == "RUBLES"
    assert dto.min_commission == 25.0


async def test_table_shows_min_commission_formatting(ui_user, valid_token, broker_service) -> None:
    """Таблица: мин. комиссия форматируется с единицей по типу (Рубли/Процент)."""
    broker_service.list_all_brokers.return_value = [
        make_broker(id=1, min_commission_type="RUBLES"),
        make_broker(id=2, min_commission_type="PERCENT"),
    ]
    authenticate(ui_user, valid_token)

    await ui_user.open("/brokers")
    await ui_user.should_see("Брокеры")

    [table] = tables(ui_user)
    assert table.rows[0]["min_commission"] == "10.00 ₽"
    assert table.rows[1]["min_commission"] == "10.00%"


async def test_edit_forwards_min_commission_type(ui_user, valid_token, broker_service) -> None:
    """Правка: смена «Тип мин. комиссии» в окне правки уходит в BrokerUpdate."""
    broker_service.list_all_brokers.return_value = [make_broker()]  # RUBLES
    broker_service.update_broker.return_value = make_broker()
    authenticate(ui_user, valid_token)

    await ui_user.open("/brokers")
    await ui_user.should_see("Брокеры")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)

    _click_row(ui_user, {"id": 1})
    assert edit_dialog.value

    type_select = one(ui_user, kind=ui.select, content="Тип мин. комиссии", within=edit_dialog)
    assert type_select.value == "RUBLES"
    type_select.value = "PERCENT"

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await eventually(lambda: broker_service.update_broker.await_count == 1)
    dto = broker_service.update_broker.await_args.args[1]
    assert dto.min_commission_type == "PERCENT"


async def test_delete_guard_reopens_edit_dialog(ui_user, valid_token, broker_service) -> None:
    """T25: delete-guard — после ошибки удаления edit-диалог открыт снова с заполненной формой."""
    broker_service.list_all_brokers.return_value = [make_broker()]
    broker_service.delete_broker.side_effect = BrokerHasAccountsError("брокер со счетами")
    authenticate(ui_user, valid_token)

    await ui_user.open("/brokers")
    await ui_user.should_see("Брокеры")
    _create_dialog, edit_dialog, confirm_dialog = _dialogs(ui_user)
    assert edit_dialog.value is False

    _click_row(ui_user, {"id": 1})
    assert edit_dialog.value

    delete_btn = one(ui_user, kind=ui.button, content="Удалить", within=edit_dialog)
    click(ui_user, delete_btn)
    assert edit_dialog.value is False
    assert confirm_dialog.value

    yes_btn = one(ui_user, kind=ui.button, content="Да", within=confirm_dialog)
    click(ui_user, yes_btn)
    await ui_user.should_see("Нельзя удалить")
    assert confirm_dialog.value is False
    assert edit_dialog.value

    edit_name = one(ui_user, kind=ui.input, content="Название", within=edit_dialog)
    assert edit_name.value == "Test Broker"
