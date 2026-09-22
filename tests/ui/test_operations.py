"""UI-тесты страницы операций по счёту (``/operations``): список и модальный CRUD.

Сервисы замоканы (``create_autospec``), пользователь симулируется через
``ui_user`` (``SimUser``). Клик по строке таблицы (Quasar ``rowClick``)
эмулируется прямой отправкой события с аргументами ``[evt, row, index]`` —
как это делает браузер.
"""

from __future__ import annotations

import datetime
from typing import Any

from nicegui import ui

from bond_accounting.account_operations.dto import AccountOperationDTO
from tests.ui._ui_utils import (
    _fire,
    authenticate,
    click,
    elements,
    eventually,
    make_account,
    one,
    tables,
)


def make_operation(**overrides: Any) -> AccountOperationDTO:
    """Операция по умолчанию: пополнение 1000.00 от 15.01.2026 на счёте №1."""
    defaults: dict[str, Any] = {
        "id": 1,
        "user_id": 1,
        "broker_account_id": 1,
        "type": "DEPOSIT",
        "amount": 1000.0,
        "date": datetime.date(2026, 1, 15),
        "note": None,
        "created_at": datetime.datetime(2026, 1, 15, 12, 0),
    }
    return AccountOperationDTO(**{**defaults, **overrides})


def _dialogs(user: Any) -> tuple[Any, Any, Any]:
    """Диалоги страницы в порядке создания: создание, правка, подтверждение."""
    found = elements(user, kind=ui.dialog)
    assert len(found) == 3, f"expected 3 dialogs, found {len(found)}"
    return found[0], found[1], found[2]


def _click_row(user: Any, row: dict[str, Any]) -> None:
    """Кликнуть строку таблицы (Quasar rowClick: evt, row, index)."""
    [table] = tables(user)
    _fire(user, table, "rowClick", [{}, row, 0])


async def test_operations_page_renders_history(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """Таблица операций: дата, счёт, тип, сумма; навигация и кнопка «Добавить»."""
    account_operation_service.list_for_user.return_value = [make_operation()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    await ui_user.should_see("Операции")
    await ui_user.should_see("Добавить")

    [table] = tables(ui_user)
    assert table.rows == [
        {
            "id": 1,
            "date": "15.01.2026",
            "account": "Test Broker / Основной",
            "type": "Пополнение",
            "amount": "1000.00",
            "note": "—",
        }
    ]
    account_operation_service.list_for_user.assert_awaited_with(1)


async def test_operations_load_error_shows_notification(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """Сбой загрузки операций: тост с ошибкой, страница рендерится."""
    account_operation_service.list_for_user.side_effect = RuntimeError("db down")
    broker_service.list_accounts_for_user.return_value = [make_account()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    await ui_user.should_see("Ошибка: db down")
    await ui_user.should_see("Добавить")


async def test_add_operation_calls_service(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """«Добавить» открывает модалку; «Готово» передаёт DTO в сервис."""
    account_operation_service.list_for_user.return_value = []
    broker_service.list_accounts_for_user.return_value = [make_account()]
    account_operation_service.create.return_value = make_operation(
        amount=250.0, date=datetime.date(2026, 2, 1)
    )
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    create_dialog, _, _ = _dialogs(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))
    assert create_dialog.value is True

    account_select = one(ui_user, kind=ui.select, content="Брокер / Счёт", within=create_dialog)
    type_select = one(ui_user, kind=ui.select, content="Тип операции", within=create_dialog)
    amount_input = one(ui_user, kind=ui.number, content="Сумма", within=create_dialog)
    date_input = one(ui_user, kind=ui.date_input, content="Дата операции", within=create_dialog)

    account_select.value = 1
    type_select.value = "WITHDRAWAL"
    amount_input.value = 250
    date_input.value = "2026-02-01"

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await eventually(lambda: account_operation_service.create.await_count == 1)
    dto = account_operation_service.create.await_args.args[0]
    assert account_operation_service.create.await_args.kwargs == {"user_id": 1}
    assert dto.broker_account_id == 1
    assert dto.type == "WITHDRAWAL"
    assert dto.amount == 250.0
    assert dto.date == datetime.date(2026, 2, 1)
    assert dto.note is None
    await ui_user.should_see("добавлена")
    assert not create_dialog.value


async def test_add_operation_without_account_shows_warning(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """Счёт не выбран: предупреждение, сервис не вызывается."""
    account_operation_service.list_for_user.return_value = []
    broker_service.list_accounts_for_user.return_value = [make_account()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    create_dialog, _, _ = _dialogs(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))

    # Счёт остаётся невыбранным; сумма не задана.
    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await ui_user.should_see("Выберите брокерский счёт")
    account_operation_service.create.assert_not_awaited()


async def test_row_click_opens_edit_modal_prefilled(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """Клик по строке открывает окно правки с данными операции."""
    account_operation_service.list_for_user.return_value = [make_operation()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    _, edit_dialog, _ = _dialogs(ui_user)
    assert edit_dialog.value is False

    _click_row(ui_user, {"id": 1})

    assert edit_dialog.value
    account_select = one(ui_user, kind=ui.select, content="Брокер / Счёт", within=edit_dialog)
    type_select = one(ui_user, kind=ui.select, content="Тип операции", within=edit_dialog)
    amount_input = one(ui_user, kind=ui.number, content="Сумма", within=edit_dialog)
    date_input = one(ui_user, kind=ui.date_input, content="Дата операции", within=edit_dialog)
    note_input = one(ui_user, kind=ui.input, content="Примечание", within=edit_dialog)

    assert account_select.value == 1
    assert type_select.value == "DEPOSIT"
    assert amount_input.value == 1000.0
    assert date_input.value == "2026-01-15"
    assert note_input.value == ""


async def test_edit_operation_calls_service(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """«Сохранить» передаёт обновлённые поля в сервис."""
    account_operation_service.list_for_user.return_value = [make_operation()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    account_operation_service.update.return_value = make_operation(amount=3000.0)
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    _, edit_dialog, _ = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    amount_input = one(ui_user, kind=ui.number, content="Сумма", within=edit_dialog)
    note_input = one(ui_user, kind=ui.input, content="Примечание", within=edit_dialog)
    amount_input.value = 3000
    note_input.value = "новая заметка"

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await eventually(lambda: account_operation_service.update.await_count == 1)
    args, kwargs = account_operation_service.update.await_args
    assert args[0] == 1  # operation id
    assert kwargs == {"user_id": 1}
    dto = args[1]
    assert dto.amount == 3000.0
    assert dto.note == "новая заметка"
    await ui_user.should_see("обновлена")


async def test_delete_button_opens_confirm_dialog(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """«Удалить» в окне правки открывает подтверждение; «Да» вызывает сервис."""
    account_operation_service.list_for_user.return_value = [make_operation()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    account_operation_service.delete.return_value = True
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    _, edit_dialog, confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_dialog))

    assert confirm_dialog.value is True
    await ui_user.should_see("Вы уверены, что хотите удалить операцию")
    await ui_user.should_see("Пополнение 1000.00")

    click(ui_user, one(ui_user, kind=ui.button, content="Да", within=confirm_dialog))

    await eventually(lambda: account_operation_service.delete.await_count == 1)
    assert account_operation_service.delete.await_args.args == (1,)
    assert account_operation_service.delete.await_args.kwargs == {"user_id": 1}
    await ui_user.should_see("Операция удалена")


async def test_drawer_has_operations_link(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """Drawer содержит ссылку «Операции», ведущую на /operations."""
    account_operation_service.list_for_user.return_value = []
    broker_service.list_accounts_for_user.return_value = [make_account()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    operations_link = one(ui_user, kind=ui.link, content="Операции")
    assert operations_link.props["href"] == "/operations"


async def test_edit_dialog_cancel_button_closes(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """F4: в окне правки есть кнопка «Отмена», клик закрывает окно."""
    account_operation_service.list_for_user.return_value = [make_operation()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})
    assert edit_dialog.value is True

    cancel = one(ui_user, kind=ui.button, content="Отмена", within=edit_dialog)
    click(ui_user, cancel)
    assert not edit_dialog.value


async def test_create_dialog_resets_after_cancel(
    ui_user, valid_token, account_operation_service, broker_service
) -> None:
    """F5: значения формы создания не сохраняются после отмены."""
    account_operation_service.list_for_user.return_value = []
    broker_service.list_accounts_for_user.return_value = [make_account()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/operations")
    create_dialog, _, _ = _dialogs(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))
    assert create_dialog.value

    one(ui_user, kind=ui.select, content="Брокер / Счёт", within=create_dialog).value = 1
    one(ui_user, kind=ui.select, content="Тип операции", within=create_dialog).value = "WITHDRAWAL"
    one(ui_user, kind=ui.number, content="Сумма", within=create_dialog).value = 250
    one(
        ui_user, kind=ui.date_input, content="Дата операции", within=create_dialog
    ).value = "2026-02-01"
    one(
        ui_user, kind=ui.input, content="Примечание", within=create_dialog
    ).value = "временная заметка"

    click(ui_user, one(ui_user, kind=ui.button, content="Отмена", within=create_dialog))
    assert not create_dialog.value

    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))
    assert create_dialog.value
    assert one(ui_user, kind=ui.select, content="Брокер / Счёт", within=create_dialog).value is None
    assert (
        one(ui_user, kind=ui.select, content="Тип операции", within=create_dialog).value
        == "DEPOSIT"
    )
    assert one(ui_user, kind=ui.number, content="Сумма", within=create_dialog).value is None
    assert (
        one(ui_user, kind=ui.date_input, content="Дата операции", within=create_dialog).value
        is None
    )
    assert one(ui_user, kind=ui.input, content="Примечание", within=create_dialog).value is None
