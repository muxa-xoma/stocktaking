"""UI-тесты страницы брокерских счетов (``/accounts``): отмена в окне правки (F4).

Сервисы замоканы (``create_autospec``). Клик по строке таблицы (Quasar
``rowClick``) эмулируется прямой отправкой события с аргументами
``[evt, row, index]`` — как в ``test_operations.py``.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from tests.ui._ui_utils import (
    _fire,
    authenticate,
    click,
    elements,
    make_account,
    one,
    tables,
)


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
    """F4: в окне правки счёта есть кнопка «Отмена», клик закрывает окно."""
    broker_service.list_accounts_for_user.return_value = [make_account()]
    broker_service.list_all_brokers.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/accounts")
    await ui_user.should_see("Брокерские счета")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    assert edit_dialog.value is False

    _click_row(ui_user, {"id": 1})
    assert edit_dialog.value

    cancel = one(ui_user, kind=ui.button, content="Отмена", within=edit_dialog)
    click(ui_user, cancel)
    assert edit_dialog.value is False
