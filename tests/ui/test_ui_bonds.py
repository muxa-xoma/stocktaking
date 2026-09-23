"""UI-тесты справочника облигаций (``/bonds``): список, модальный CRUD и
поиск по ISIN в справочнике MOEX (SC-7).

Сервисы замоканы (``create_autospec``). Клик по строке таблицы (Quasar
``rowClick``) эмулируется прямой отправкой события с аргументами
``[evt, row, index]`` — как это делает браузер (см. ``test_operations.py``).
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest
from nicegui import ui

from bond_accounting.market_data import (
    BondReference,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from tests.ui._ui_utils import (
    _fire,
    authenticate,
    click,
    elements,
    eventually,
    make_bond,
    one,
    tables,
)


def _open_bonds_page(ui_user, valid_token, bond_service, bonds: list | None = None) -> None:
    """Открыть /bonds с заданным справочником облигаций."""
    bond_service.list_all.return_value = [] if bonds is None else bonds
    authenticate(ui_user, valid_token)


def _dialogs(user: Any) -> tuple[Any, Any, Any]:
    """Диалоги страницы в порядке создания: создание, правка, подтверждение."""
    found = elements(user, kind=ui.dialog)
    assert len(found) == 3, f"expected 3 dialogs, found {len(found)}"
    return found[0], found[1], found[2]


def _click_row(user: Any, row: dict[str, Any]) -> None:
    """Кликнуть строку таблицы (Quasar rowClick: evt, row, index)."""
    [table] = tables(user)
    _fire(user, table, "rowClick", [{}, row, 0])


def _open_create_dialog(user: Any) -> Any:
    """Нажать «Добавить» на странице и вернуть открывшееся окно создания."""
    create_dialog, _edit_dialog, _confirm_dialog = _dialogs(user)
    click(user, one(user, kind=ui.button, content="Добавить"))
    assert create_dialog.value is True
    return create_dialog


async def test_bonds_table_populated(ui_user, valid_token, bond_service) -> None:
    """Таблица облигаций: период купона, даты и пустой эмитент."""
    _open_bonds_page(
        ui_user,
        valid_token,
        bond_service,
        [
            make_bond(),
            make_bond(id=2, isin="RU000A0JX0J4", coupon_period_days=91, issuer="Минфин"),
            make_bond(id=3, isin="RU000A0JX0J6", coupon_period_days=0),
        ],
    )

    await ui_user.open("/bonds")
    await ui_user.should_see("Облигации")

    [table] = tables(ui_user)
    assert table.rows[0] == {
        "id": 1,
        "isin": "RU000A0JX0J2",
        "name": "ОФЗ 26207",
        "nominal": 1000,
        "coupon_rate": 8.15,
        "coupon_period_days": 182,
        "maturity_date": "04.02.2027",
        "issuer": "—",
    }
    assert table.rows[1]["coupon_period_days"] == 91
    assert table.rows[1]["issuer"] == "Минфин"
    # Бескупонная облигация: период 0.
    assert table.rows[2]["coupon_period_days"] == 0


async def test_bonds_load_error_shows_notification(ui_user, valid_token, bond_service) -> None:
    """Сбой загрузки справочника: тост с ошибкой."""
    bond_service.list_all.side_effect = RuntimeError("db down")
    authenticate(ui_user, valid_token)

    await ui_user.open("/bonds")
    await ui_user.should_see("Ошибка: db down")


async def test_bond_create_success(ui_user, valid_token, bond_service) -> None:
    """Окно создания: поля передаются в сервис, таблица перезагружается."""
    _open_bonds_page(ui_user, valid_token, bond_service)
    bond_service.create.return_value = make_bond()

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)

    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)
    name_input = one(ui_user, kind=ui.input, content="Название", within=create_dialog)
    nominal_input = one(ui_user, kind=ui.number, content="Номинал", within=create_dialog)
    rate_input = one(ui_user, kind=ui.number, content="Купонная ставка", within=create_dialog)
    period_days_input = one(ui_user, kind=ui.number, content="Период купона", within=create_dialog)
    maturity_input = one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=create_dialog
    )

    isin_input.value = "RU000A0JX0J2"
    name_input.value = "ОФЗ 26207"
    nominal_input.value = 2000
    rate_input.value = 7.5
    period_days_input.value = 91
    maturity_input.value = "2028-03-01"

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await ui_user.should_see("Облигация RU000A0JX0J2 добавлена")
    created = bond_service.create.await_args.args[0]
    assert created.isin == "RU000A0JX0J2"
    assert created.name == "ОФЗ 26207"
    assert created.nominal == 2000
    assert created.coupon_rate == 7.5
    assert created.coupon_period_days == 91
    assert created.maturity_date == date(2028, 3, 1)
    assert created.issuer is None
    assert bond_service.create.await_args.kwargs == {"user_id": 1}
    await eventually(lambda: bond_service.list_all.await_count == 2)
    assert create_dialog.value is False


async def test_bond_create_validation_error(ui_user, valid_token, bond_service) -> None:
    """Некорректный ISIN: pydantic отклоняет создание, показывается ошибка."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)

    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)
    name_input = one(ui_user, kind=ui.input, content="Название", within=create_dialog)
    rate_input = one(ui_user, kind=ui.number, content="Купонная ставка", within=create_dialog)
    maturity_input = one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=create_dialog
    )

    isin_input.value = "SHORT"
    name_input.value = "ОФЗ 26207"
    rate_input.value = 8.15
    maturity_input.value = "2027-02-04"

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await ui_user.should_see("Некорректные данные облигации")
    bond_service.create.assert_not_awaited()
    assert create_dialog.value is True


async def test_bond_create_empty_date(ui_user, valid_token, bond_service) -> None:
    """Пустая дата погашения: parse_date отклоняет форму."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)

    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)
    isin_input.value = "RU000A0JX0J2"

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await ui_user.should_see("Некорректные данные облигации")
    bond_service.create.assert_not_awaited()


async def test_bond_create_unexpected_error(ui_user, valid_token, bond_service) -> None:
    """Сбой сервиса при создании: общий тост об ошибке."""
    _open_bonds_page(ui_user, valid_token, bond_service)
    bond_service.create.side_effect = RuntimeError("db down")

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)

    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)
    name_input = one(ui_user, kind=ui.input, content="Название", within=create_dialog)
    rate_input = one(ui_user, kind=ui.number, content="Купонная ставка", within=create_dialog)
    maturity_input = one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=create_dialog
    )

    isin_input.value = "RU000A0JX0J2"
    name_input.value = "ОФЗ 26207"
    rate_input.value = 8.15
    maturity_input.value = "2027-02-04"

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await ui_user.should_see("Ошибка: db down")


async def test_isin_input_validation_rule(ui_user, valid_token, bond_service) -> None:
    """Правило валидации ISIN: неверный формат помечает поле ошибкой."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    isin_input = one(ui_user, kind=ui.input, content="ISIN")

    isin_input.value = "SHORT"
    assert isin_input.validate() is False
    assert isin_input.error == "ISIN — ровно 12 символов (A-Z, 0-9)"

    isin_input.value = "RU000A0JX0J2"
    assert isin_input.validate() is True
    # После успешной валидации поле ошибки сбрасывается.
    # strict None check is intentional: pyrefly narrows error to str
    # from the assert above, but NiceGUI resets it to None after
    # successful validation (untracked by flow analysis).
    assert isin_input.error is None  # pyrefly: ignore[unnecessary-comparison]


async def test_select_row_fills_edit_form(ui_user, valid_token, bond_service) -> None:
    """Клик по строке открывает окно правки с данными облигации."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond(issuer="Минфин")])

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    assert edit_dialog.value is False

    [table] = tables(ui_user)
    row = table.rows[0]
    _click_row(ui_user, {"id": row["id"]})

    assert edit_dialog.value
    edit_name = one(ui_user, kind=ui.input, content="Название", within=edit_dialog)
    edit_nominal = one(ui_user, kind=ui.number, content="Номинал", within=edit_dialog)
    edit_rate = one(ui_user, kind=ui.number, content="Купонная ставка", within=edit_dialog)
    edit_period_days = one(ui_user, kind=ui.number, content="Период купона", within=edit_dialog)
    edit_maturity = one(ui_user, kind=ui.date_input, content="Дата погашения", within=edit_dialog)
    edit_issuer = one(ui_user, kind=ui.input, content="Эмитент", within=edit_dialog)

    assert edit_name.value == "ОФЗ 26207"
    assert edit_nominal.value == 1000
    assert edit_rate.value == 8.15
    assert edit_period_days.value == 182
    assert edit_maturity.value == "2027-02-04"
    assert edit_issuer.value == "Минфин"
    assert row["isin"] == "RU000A0JX0J2"


async def test_reload_after_update_resets_selection(ui_user, valid_token, bond_service) -> None:
    """После успешного сохранения выделение сбрасывается (перезагрузка таблицы).

    Преемник старого теста на снятие выделения: отдельного элемента
    «снять выделение» в новом UI нет — выбор живёт ровно до перезагрузки.
    """
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.update.return_value = make_bond(name="ОФЗ 26208")

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})
    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await eventually(lambda: bond_service.update.await_count == 1)
    await eventually(lambda: bond_service.list_all.await_count == 2)

    # Выделение сброшено перезагрузкой: сохранение требует снова выбрать строку.
    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))
    await ui_user.should_see("Сначала выберите облигацию в таблице")
    assert bond_service.update.await_count == 1


async def test_update_without_selection_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Кнопка «Сохранить» без выбора строки — предупреждение."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await ui_user.should_see("Сначала выберите облигацию в таблице")
    bond_service.update.assert_not_awaited()


async def test_update_without_name_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Пустое название при правке — ошибка валидации."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    edit_name = one(ui_user, kind=ui.input, content="Название", within=edit_dialog)
    edit_name.value = ""

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await ui_user.should_see("Название обязательно")
    bond_service.update.assert_not_awaited()


async def test_update_success(ui_user, valid_token, bond_service) -> None:
    """Правка выбранной облигации передаёт BondUpdate в сервис."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.update.return_value = make_bond(name="ОФЗ 26208")

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    one(ui_user, kind=ui.input, content="Название", within=edit_dialog).value = "ОФЗ 26208"
    one(ui_user, kind=ui.number, content="Номинал", within=edit_dialog).value = 2000
    one(ui_user, kind=ui.number, content="Купонная ставка", within=edit_dialog).value = 7.5
    one(ui_user, kind=ui.number, content="Период купона", within=edit_dialog).value = 91
    one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=edit_dialog
    ).value = "2028-03-01"
    one(ui_user, kind=ui.input, content="Эмитент", within=edit_dialog).value = "ВТБ"

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await ui_user.should_see("Облигация RU000A0JX0J2 обновлена")
    bond_id, update = bond_service.update.await_args.args
    assert bond_id == 1
    assert update.name == "ОФЗ 26208"
    assert update.nominal == 2000
    assert update.coupon_rate == 7.5
    assert update.coupon_period_days == 91
    assert update.maturity_date == date(2028, 3, 1)
    assert update.issuer == "ВТБ"
    assert bond_service.update.await_args.kwargs == {"user_id": 1}
    await eventually(lambda: bond_service.list_all.await_count == 2)
    assert edit_dialog.value is False


async def test_update_not_found_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Облигация удалена другим клиентом: предупреждение и перезагрузка."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.update.return_value = None

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await ui_user.should_see("Облигация не найдена")


async def test_update_invalid_date_shows_validation_error(
    ui_user, valid_token, bond_service
) -> None:
    """Некорректная дата погашения в форме правки — ошибка валидации."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=edit_dialog
    ).value = "31.02.2027"

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await ui_user.should_see("Некорректные данные облигации")
    bond_service.update.assert_not_awaited()


async def test_update_unexpected_error(ui_user, valid_token, bond_service) -> None:
    """Сбой сервиса при правке: общий тост об ошибке."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.update.side_effect = RuntimeError("db down")

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))

    await ui_user.should_see("Ошибка: db down")


async def test_delete_without_selection_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Кнопка удаления без выбора строки — предупреждение."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_dialog))

    await ui_user.should_see("Сначала выберите облигацию в таблице")
    bond_service.delete.assert_not_awaited()


async def test_delete_success(ui_user, valid_token, bond_service) -> None:
    """Удаление через диалог подтверждения."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.delete.return_value = True

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_dialog))

    assert confirm_dialog.value is True
    await ui_user.should_see("Вы уверены, что хотите удалить облигацию")
    await ui_user.should_see("ОФЗ 26207")

    click(ui_user, one(ui_user, kind=ui.button, content="Да", within=confirm_dialog))

    await ui_user.should_see("Облигация удалена")
    bond_service.delete.assert_awaited_once_with(1, user_id=1)
    await eventually(lambda: bond_service.list_all.await_count == 2)
    assert not confirm_dialog.value


async def test_delete_not_found_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Облигация уже удалена: предупреждение и перезагрузка."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.delete.return_value = False

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_dialog))
    await ui_user.should_see("Вы уверены, что хотите удалить облигацию")

    click(ui_user, one(ui_user, kind=ui.button, content="Да", within=confirm_dialog))

    await ui_user.should_see("Облигация не найдена")


async def test_delete_cancel_keeps_bond(ui_user, valid_token, bond_service) -> None:
    """Отмена в диалоге подтверждения не удаляет облигацию."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_dialog))
    await ui_user.should_see("Вы уверены, что хотите удалить облигацию")

    click(ui_user, one(ui_user, kind=ui.button, content="Нет", within=confirm_dialog))

    await eventually(lambda: not confirm_dialog.value)
    bond_service.delete.assert_not_awaited()


async def test_delete_service_error_shows_notification(ui_user, valid_token, bond_service) -> None:
    """Сбой сервиса при удалении: тост об ошибке, без перезагрузки таблицы."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.delete.side_effect = RuntimeError("db down")

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})

    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_dialog))
    await ui_user.should_see("Вы уверены, что хотите удалить облигацию")

    click(ui_user, one(ui_user, kind=ui.button, content="Да", within=confirm_dialog))

    await ui_user.should_see("Ошибка: db down")


async def test_select_row_with_stale_bond_id_resets_selection(
    ui_user, valid_token, bond_service
) -> None:
    """Клик по строке с идентификатором вне кэша игнорируется."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 999})

    # Окно правки не открылось: выделение не установлено.
    assert edit_dialog.value is False
    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить", within=edit_dialog))
    await ui_user.should_see("Сначала выберите облигацию в таблице")
    bond_service.update.assert_not_awaited()


async def test_edit_dialog_cancel_button_closes(ui_user, valid_token, bond_service) -> None:
    """F4: в окне правки есть кнопка «Отмена», клик закрывает окно."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    _create_dialog, edit_dialog, _confirm_dialog = _dialogs(ui_user)
    _click_row(ui_user, {"id": 1})
    assert edit_dialog.value is True

    cancel = one(ui_user, kind=ui.button, content="Отмена", within=edit_dialog)
    click(ui_user, cancel)
    assert not edit_dialog.value


async def test_create_dialog_resets_after_success(ui_user, valid_token, bond_service) -> None:
    """F5: форма создания очищается после успешного создания записи."""
    _open_bonds_page(ui_user, valid_token, bond_service)
    bond_service.create.return_value = make_bond()

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)

    one(ui_user, kind=ui.input, content="ISIN", within=create_dialog).value = "RU000A0JX0J2"
    one(ui_user, kind=ui.input, content="Название", within=create_dialog).value = "Тест"
    one(ui_user, kind=ui.number, content="Номинал", within=create_dialog).value = 2000
    one(ui_user, kind=ui.number, content="Купонная ставка", within=create_dialog).value = 7.5
    one(ui_user, kind=ui.number, content="Период купона", within=create_dialog).value = 91
    one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=create_dialog
    ).value = "2028-03-01"
    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))

    await ui_user.should_see("Облигация RU000A0JX0J2 добавлена")
    assert create_dialog.value is False

    _open_create_dialog(ui_user)
    assert one(ui_user, kind=ui.input, content="ISIN", within=create_dialog).value is None
    assert one(ui_user, kind=ui.input, content="Название", within=create_dialog).value is None
    assert one(ui_user, kind=ui.number, content="Период купона", within=create_dialog).value == 182


async def test_create_dialog_resets_after_cancel(ui_user, valid_token, bond_service) -> None:
    """F5: введённые значения не сохраняются после отмены создания."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)

    one(ui_user, kind=ui.input, content="ISIN", within=create_dialog).value = "RU000A0JX0J4"
    one(ui_user, kind=ui.input, content="Название", within=create_dialog).value = "Тест"
    click(ui_user, one(ui_user, kind=ui.button, content="Отмена", within=create_dialog))
    assert create_dialog.value is False

    _open_create_dialog(ui_user)
    assert one(ui_user, kind=ui.input, content="ISIN", within=create_dialog).value is None
    assert one(ui_user, kind=ui.input, content="Название", within=create_dialog).value is None


# --------------------------------------------------------------------- #
# Поиск по справочнику MOEX в форме создания (SC-7)


def _make_reference(**overrides: Any) -> BondReference:
    """Ссылка MOEX по умолчанию (ОФЗ 26207, купон раз в 182 дня)."""
    defaults: dict[str, Any] = {
        "isin": "RU000A0JX0J2",
        "name": "ОФЗ 26207",
        "nominal": 1000,
        "coupon_rate": 8.15,
        "coupon_period_days": 182,
        "maturity_date": date(2041, 2, 26),
        "issuer": "Минфин",
    }
    return BondReference(**{**defaults, **overrides})


async def test_isin_typing_triggers_debounced_search(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """Ввод ISIN запускает поиск по справочнику после 300 мс дебаунса.

    Короткий запрос (меньше 2 символов) не доходит до справочника;
    быстрая допечатка отменяет предыдущий поиск — до сервиса доезжает
    только финальный запрос (с лимитом 10 кандидатов).
    """
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)

    isin_input.value = "R"
    await asyncio.sleep(0.4)
    bond_reference_service.search.assert_not_awaited()

    isin_input.value = "RU000A0JX"
    isin_input.value = "RU000A0JX0J2"
    await eventually(lambda: bond_reference_service.search.await_count == 1)
    assert bond_reference_service.search.await_args.args == ("RU000A0JX0J2",)
    assert bond_reference_service.search.await_args.kwargs == {"limit": 10}


async def test_isin_candidate_selection_fills_form(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """Выбор кандидата из справочника заполняет всю форму создания."""
    _open_bonds_page(ui_user, valid_token, bond_service)
    reference = _make_reference()
    bond_reference_service.search.return_value = [reference]

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)

    isin_input.value = reference.isin
    await eventually(
        lambda: (
            elements(ui_user, kind=ui.button, content=reference.isin, within=create_dialog) != []
        )
    )
    # Подпись кандидата: «ISIN — имя — погашение (ГГГГ-ММ)».
    await ui_user.should_see(f"{reference.isin} — {reference.name} — погашение (2041-02)")

    click(ui_user, one(ui_user, kind=ui.button, content=reference.isin, within=create_dialog))

    assert isin_input.value == reference.isin
    assert (
        one(ui_user, kind=ui.input, content="Название", within=create_dialog).value
        == reference.name
    )
    assert one(ui_user, kind=ui.number, content="Номинал", within=create_dialog).value == 1000
    assert (
        one(ui_user, kind=ui.number, content="Купонная ставка", within=create_dialog).value == 8.15
    )
    assert one(ui_user, kind=ui.number, content="Период купона", within=create_dialog).value == 182
    assert (
        one(ui_user, kind=ui.date_input, content="Дата погашения", within=create_dialog).value
        == "2041-02-26"
    )
    assert one(ui_user, kind=ui.input, content="Эмитент", within=create_dialog).value == "Минфин"
    # Список кандидатов скрыт после автозаполнения.
    assert not elements(ui_user, kind=ui.button, content=reference.isin, within=create_dialog)


@pytest.mark.parametrize(
    "error",
    [ProviderUnavailableError("MOEX down"), ProviderTimeoutError("MOEX timed out")],
)
async def test_isin_search_provider_error_shows_hint(
    ui_user, valid_token, bond_service, bond_reference_service, error: Exception
) -> None:
    """Недоступность MOEX — ненавязчивая подсказка, форма остаётся рабочей."""
    _open_bonds_page(ui_user, valid_token, bond_service)
    bond_reference_service.search.side_effect = error

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)

    isin_input.value = "RU000A0JX0J2"
    await eventually(lambda: bond_reference_service.search.await_count == 1)
    await ui_user.should_see("Справочник MOEX: сервис временно недоступен, введите данные вручную")

    # Форма остаётся доступной для ручного ввода, кандидаты скрыты.
    name_input = one(ui_user, kind=ui.input, content="Название", within=create_dialog)
    name_input.value = "Ручной ввод"
    assert name_input.value == "Ручной ввод"
    assert not elements(ui_user, kind=ui.button, content="RU000A0JX0J2", within=create_dialog)


async def test_reference_disabled_shows_static_hint(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """Выключенная интеграция — статичная подсказка, поиск не запускается.

    NB: подсказка рендерится при построении страницы; повторное открытие
    окна создания сбрасывает её вместе с формой (``_reset_create_form``
    скрывает ``isin_hint``) — зарепортировано в open_questions.
    """
    bond_reference_service.enabled = False
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    hint = one(ui_user, kind=ui.label, content="Справочник MOEX отключён — введите данные вручную")
    assert hint.visible

    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)
    isin_input.value = "RU000A0JX0J2"
    await asyncio.sleep(0.4)
    bond_reference_service.search.assert_not_awaited()
    assert not elements(ui_user, kind=ui.button, content="RU000A0JX0J2", within=create_dialog)


# --------------------------------------------------------------------- #
# Отмена отложенного ISIN-поиска (DC-6) + авто-заполнение купонов (DC-5)


def _gated_isin_search(bond_reference_service, entered: asyncio.Event) -> None:
    """Подменить поиск коронтиной, фиксирующей момент запуска.

    ``entered`` ставится, только если коронтина ``search`` реально запущена
    (т.е. дебаунс отработал и поиск пошёл). Отмена задачи в окне дебаунса
    не даёт коронтине запуститься — ``entered`` остаётся пустым.
    """

    async def search(query: str, limit: int = 10) -> list[BondReference]:
        entered.set()
        return []

    bond_reference_service.search.side_effect = search


async def test_dialog_close_cancels_pending_isin_search(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """DC-6a: «Отмена» в окне создания отменяет висящий дебаунс-поиск.

    Открываем окно, вводим ISIN (дебаунс-задача поставлена), сразу жмём
    «Отмена» в пределах окна дебаунса (300 мс) и убеждаемся, что поиск
    так и не запустился — задача отменена обработчиком ``_on_create_dialog_change``.
    """
    _open_bonds_page(ui_user, valid_token, bond_service)
    entered = asyncio.Event()
    _gated_isin_search(bond_reference_service, entered)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)

    # Ввод ставит отложенный поиск; ждём чуть меньше дебаунса, чтобы задача
    # точно была создана, но ещё не отработала.
    isin_input.value = "RU000A0JX0J2"
    await asyncio.sleep(0.15)
    bond_reference_service.search.assert_not_called()

    # Закрываем окно через «Отмена» в пределах окна дебаунса.
    click(ui_user, one(ui_user, kind=ui.button, content="Отмена", within=create_dialog))
    assert create_dialog.value is False

    # Ждём, когда дебаунс гарантированно истёк бы, если бы поиск не отменили.
    await asyncio.sleep(0.4)
    bond_reference_service.search.assert_not_awaited()
    assert not entered.is_set(), "отложенный поиск не был отменён при закрытии окна"


async def test_unmount_cancels_pending_isin_search(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """DC-6b: teardown страницы/клиента отменяет висящий дебаунс-поиск.

    ``context.client.on_delete`` (зарегистрированный в render) вызывается при
    удалении клиента; метод ``Client.delete()`` симуляции триггерит обработчики
    ``delete_handlers``, которые отменяют поиск до того, как дебаунс отработает.
    """
    _open_bonds_page(ui_user, valid_token, bond_service)
    entered = asyncio.Event()
    _gated_isin_search(bond_reference_service, entered)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)

    isin_input.value = "RU000A0JX0J2"
    await asyncio.sleep(0.15)
    bond_reference_service.search.assert_not_called()

    # Разбираем клиент — представление «пользователь закрыл вкладку».
    ui_user.client.delete()

    await asyncio.sleep(0.4)
    bond_reference_service.search.assert_not_awaited()
    assert not entered.is_set(), "отложенный поиск не был отменён при удалении клиента"


async def test_new_search_cancels_previous_isin_search(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """DC-6c: быстрая смена ISIN отменяет предыдущий дебаунс-поиск.

    До справочника доходит только финальный запрос; промежуточный ввод
    (после паузы < 300 мс) не исполняется — задача отменяется в
    ``_on_isin_change`` перед постановкой новой.
    """
    _open_bonds_page(ui_user, valid_token, bond_service)
    entered = asyncio.Event()
    _gated_isin_search(bond_reference_service, entered)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)

    isin_input.value = "RU000A0JX"
    await asyncio.sleep(0.15)
    bond_reference_service.search.assert_not_called()

    # Смена значения до истечения дебаунса должна отменить первую задачу.
    isin_input.value = "RU000A0JX0J2"
    await eventually(lambda: bond_reference_service.search.await_count == 1)
    assert bond_reference_service.search.call_count == 1
    assert bond_reference_service.search.await_args.args == ("RU000A0JX0J2",)


async def test_create_from_candidate_fires_schedule_populate_coupons(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """DC-5 (UI): создание из кандидата MOEX запускает авто-заполнение купонов.

    При успешном создании ``_schedule_populate_coupons`` вызывает
    ``schedule_populate_coupons(isin)`` fire-and-forget (синхронно, без await),
    не влияя на успешное сохранение и уведомление пользователя.
    """
    _open_bonds_page(ui_user, valid_token, bond_service)
    reference = _make_reference()
    bond_reference_service.search.return_value = [reference]
    bond_service.create.return_value = make_bond(isin=reference.isin)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_dialog)

    # Автозаполнение формы из кандидата MOEX.
    isin_input.value = reference.isin
    await eventually(
        lambda: (
            elements(ui_user, kind=ui.button, content=reference.isin, within=create_dialog) != []
        )
    )
    click(ui_user, one(ui_user, kind=ui.button, content=reference.isin, within=create_dialog))

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))
    await ui_user.should_see(f"Облигация {reference.isin} добавлена")
    assert create_dialog.value is False
    # Fire-and-forget: записи ровно один вызов с ISIN созданной облигации.
    bond_reference_service.schedule_populate_coupons.assert_called_once_with(reference.isin)


async def test_create_populate_raising_stub_keeps_save_success(
    ui_user, valid_token, bond_service, bond_reference_service
) -> None:
    """DC-5 (UI): сбой запуска авто-заполнения не ломает сохранение.

    Даже если ``schedule_populate_coupons`` бросает исключение (например,
    нет session_factory), создание облигации и уведомление об успехе
    сохраняются — ошибка проглатывается в ``_schedule_populate_coupons``.
    """
    bond_reference_service.schedule_populate_coupons.side_effect = RuntimeError(
        "no session factory"
    )
    _open_bonds_page(ui_user, valid_token, bond_service)
    reference = _make_reference()
    bond_service.create.return_value = make_bond(isin=reference.isin)

    await ui_user.open("/bonds")
    create_dialog = _open_create_dialog(ui_user)

    # Заполняем форму создания вручную (не через автозаполнение).
    one(ui_user, kind=ui.input, content="ISIN", within=create_dialog).value = reference.isin
    one(ui_user, kind=ui.input, content="Название", within=create_dialog).value = reference.name
    one(ui_user, kind=ui.number, content="Номинал", within=create_dialog).value = reference.nominal
    one(
        ui_user, kind=ui.number, content="Купонная ставка", within=create_dialog
    ).value = reference.coupon_rate
    one(
        ui_user, kind=ui.number, content="Период купона", within=create_dialog
    ).value = reference.coupon_period_days
    one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=create_dialog
    ).value = reference.maturity_date.isoformat()
    one(ui_user, kind=ui.input, content="Эмитент", within=create_dialog).value = reference.issuer

    click(ui_user, one(ui_user, kind=ui.button, content="Готово", within=create_dialog))
    await ui_user.should_see(f"Облигация {reference.isin} добавлена")
    assert create_dialog.value is False
    assert bond_service.create.await_count == 1
    # Триггер сработал, но его исключение проглочено и не изменило ответ.
    bond_reference_service.schedule_populate_coupons.assert_called_once_with(reference.isin)
