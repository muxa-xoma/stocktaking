"""UI-тесты справочника облигаций (``/bonds``): список, создание, правка, удаление."""

from __future__ import annotations

from datetime import date

from nicegui import ui

from tests._ui_utils import (
    authenticate,
    cards,
    click,
    deselect_table_rows,
    eventually,
    make_bond,
    one,
    select_table_row,
    tables,
)


def _open_bonds_page(ui_user, valid_token, bond_service, bonds: list | None = None) -> None:
    """Открыть /bonds с заданным справочником облигаций."""
    bond_service.list_all.return_value = [] if bonds is None else bonds
    authenticate(ui_user, valid_token)


async def test_bonds_table_populated(ui_user, valid_token, bond_service) -> None:
    """Таблица облигаций: форматирование частоты, даты и пустого эмитента."""
    _open_bonds_page(
        ui_user,
        valid_token,
        bond_service,
        [
            make_bond(),
            make_bond(id=2, isin="RU000A0JX0J4", coupon_frequency="SEMI_ANNUAL", issuer="Минфин"),
            make_bond(id=3, isin="RU000A0JX0J6", coupon_frequency="MONTHLY"),
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
        "coupon_frequency": "Ежегодно",
        "maturity_date": "04.02.2027",
        "issuer": "—",
    }
    assert table.rows[1]["coupon_frequency"] == "Раз в полгода"
    assert table.rows[1]["issuer"] == "Минфин"
    # Неизвестная частота отображается как есть (fallback словаря подписей).
    assert table.rows[2]["coupon_frequency"] == "MONTHLY"


async def test_bonds_load_error_shows_notification(ui_user, valid_token, bond_service) -> None:
    """Сбой загрузки справочника: тост с ошибкой."""
    bond_service.list_all.side_effect = RuntimeError("db down")
    authenticate(ui_user, valid_token)

    await ui_user.open("/bonds")
    await ui_user.should_see("Ошибка: db down")


async def test_bond_create_success(ui_user, valid_token, bond_service) -> None:
    """Форма создания: поля передаются в сервис, таблица перезагружается."""
    _open_bonds_page(ui_user, valid_token, bond_service)
    bond_service.create.return_value = make_bond()

    await ui_user.open("/bonds")
    (create_card, _edit_card, _dialog_card) = cards(ui_user)

    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_card)
    name_input = one(ui_user, kind=ui.input, content="Название", within=create_card)
    nominal_input = one(ui_user, kind=ui.number, content="Номинал", within=create_card)
    rate_input = one(ui_user, kind=ui.number, content="Купонная ставка", within=create_card)
    frequency_select = one(ui_user, kind=ui.select, content="Частота купона", within=create_card)
    maturity_input = one(ui_user, kind=ui.date_input, content="Дата погашения", within=create_card)

    isin_input.value = "RU000A0JX0J2"
    name_input.value = "ОФЗ 26207"
    nominal_input.value = 2000
    rate_input.value = 7.5
    frequency_select.value = "QUARTERLY"
    maturity_input.value = "2028-03-01"

    add_button = one(ui_user, kind=ui.button, content="Добавить")
    click(ui_user, add_button)

    await ui_user.should_see("Облигация RU000A0JX0J2 добавлена")
    created = bond_service.create.await_args.args[0]
    assert created.isin == "RU000A0JX0J2"
    assert created.name == "ОФЗ 26207"
    assert created.nominal == 2000
    assert created.coupon_rate == 7.5
    assert created.coupon_frequency == "QUARTERLY"
    assert created.maturity_date == date(2028, 3, 1)
    assert created.issuer is None
    await eventually(lambda: bond_service.list_all.await_count == 2)


async def test_bond_create_validation_error(ui_user, valid_token, bond_service) -> None:
    """Некорректный ISIN: pydantic отклоняет создание, показывается ошибка."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    (create_card, _edit_card, _dialog_card) = cards(ui_user)

    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_card)
    name_input = one(ui_user, kind=ui.input, content="Название", within=create_card)
    rate_input = one(ui_user, kind=ui.number, content="Купонная ставка", within=create_card)
    maturity_input = one(ui_user, kind=ui.date_input, content="Дата погашения", within=create_card)

    isin_input.value = "SHORT"
    name_input.value = "ОФЗ 26207"
    rate_input.value = 8.15
    maturity_input.value = "2027-02-04"

    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))

    await ui_user.should_see("Некорректные данные облигации")
    bond_service.create.assert_not_awaited()


async def test_bond_create_empty_date(ui_user, valid_token, bond_service) -> None:
    """Пустая дата погашения: parse_date отклоняет форму."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    (create_card, _edit_card, _dialog_card) = cards(ui_user)

    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_card)
    isin_input.value = "RU000A0JX0J2"

    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))

    await ui_user.should_see("Некорректные данные облигации")
    bond_service.create.assert_not_awaited()


async def test_bond_create_unexpected_error(ui_user, valid_token, bond_service) -> None:
    """Сбой сервиса при создании: общий тост об ошибке."""
    _open_bonds_page(ui_user, valid_token, bond_service)
    bond_service.create.side_effect = RuntimeError("db down")

    await ui_user.open("/bonds")
    (create_card, _edit_card, _dialog_card) = cards(ui_user)
    isin_input = one(ui_user, kind=ui.input, content="ISIN", within=create_card)
    name_input = one(ui_user, kind=ui.input, content="Название", within=create_card)
    rate_input = one(ui_user, kind=ui.number, content="Купонная ставка", within=create_card)
    maturity_input = one(ui_user, kind=ui.date_input, content="Дата погашения", within=create_card)

    isin_input.value = "RU000A0JX0J2"
    name_input.value = "ОФЗ 26207"
    rate_input.value = 8.15
    maturity_input.value = "2027-02-04"

    click(ui_user, one(ui_user, kind=ui.button, content="Добавить"))

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
    assert isin_input.error is None


async def _select_first_bond(ui_user) -> dict:
    """Выбрать первую строку таблицы и вернуть строку таблицы."""
    [table] = tables(ui_user)
    row = table.rows[0]
    select_table_row(ui_user, table, row)
    return row


async def test_select_row_fills_edit_form(ui_user, valid_token, bond_service) -> None:
    """Выбор строки заполняет форму правки значениями облигации."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond(issuer="Минфин")])

    await ui_user.open("/bonds")
    row = await _select_first_bond(ui_user)

    await ui_user.should_see("Выбрана облигация: RU000A0JX0J2 — ОФЗ 26207")

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    edit_name = one(ui_user, kind=ui.input, content="Название", within=edit_card)
    edit_nominal = one(ui_user, kind=ui.number, content="Номинал", within=edit_card)
    edit_rate = one(ui_user, kind=ui.number, content="Купонная ставка", within=edit_card)
    edit_frequency = one(ui_user, kind=ui.select, content="Частота купона", within=edit_card)
    edit_maturity = one(ui_user, kind=ui.date_input, content="Дата погашения", within=edit_card)
    edit_issuer = one(ui_user, kind=ui.input, content="Эмитент", within=edit_card)

    assert edit_name.value == "ОФЗ 26207"
    assert edit_nominal.value == 1000
    assert edit_rate.value == 8.15
    assert edit_frequency.value == "ANNUAL"
    assert edit_maturity.value == "2027-02-04"
    assert edit_issuer.value == "Минфин"
    assert row["isin"] == "RU000A0JX0J2"


async def test_deselect_row_resets_selection(ui_user, valid_token, bond_service) -> None:
    """Снятие выделения сбрасывает выбранную облигацию."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    row = await _select_first_bond(ui_user)
    [table] = tables(ui_user)
    deselect_table_rows(ui_user, table, row["id"])

    # Обновление без выбранной облигации показывает предупреждение.
    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))
    await ui_user.should_see("Сначала выберите облигацию в таблице")


async def test_update_without_selection_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Кнопка «Сохранить изменения» без выбора строки — предупреждение."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))

    await ui_user.should_see("Сначала выберите облигацию в таблице")
    bond_service.update.assert_not_awaited()


async def test_update_without_name_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Пустое название при правке — ошибка валидации."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    edit_name = one(ui_user, kind=ui.input, content="Название", within=edit_card)
    edit_name.value = ""

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))

    await ui_user.should_see("Название обязательно")
    bond_service.update.assert_not_awaited()


async def test_update_success(ui_user, valid_token, bond_service) -> None:
    """Правка выбранной облигации передаёт BondUpdate в сервис."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.update.return_value = make_bond(name="ОФЗ 26208")

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    one(ui_user, kind=ui.input, content="Название", within=edit_card).value = "ОФЗ 26208"
    one(ui_user, kind=ui.number, content="Номинал", within=edit_card).value = 2000
    one(ui_user, kind=ui.number, content="Купонная ставка", within=edit_card).value = 7.5
    one(ui_user, kind=ui.select, content="Частота купона", within=edit_card).value = "SEMI_ANNUAL"
    one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=edit_card
    ).value = "2028-03-01"
    one(ui_user, kind=ui.input, content="Эмитент", within=edit_card).value = "ВТБ"

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))

    await ui_user.should_see("Облигация RU000A0JX0J2 обновлена")
    bond_id, update = bond_service.update.await_args.args
    assert bond_id == 1
    assert update.name == "ОФЗ 26208"
    assert update.nominal == 2000
    assert update.coupon_rate == 7.5
    assert update.coupon_frequency == "SEMI_ANNUAL"
    assert update.maturity_date == date(2028, 3, 1)
    assert update.issuer == "ВТБ"
    await eventually(lambda: bond_service.list_all.await_count == 2)


async def test_update_not_found_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Облигация удалена другим клиентом: предупреждение и перезагрузка."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.update.return_value = None

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))

    await ui_user.should_see("Облигация не найдена")


async def test_update_invalid_date_shows_validation_error(
    ui_user, valid_token, bond_service
) -> None:
    """Некорректная дата погашения в форме правки — ошибка валидации."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    one(
        ui_user, kind=ui.date_input, content="Дата погашения", within=edit_card
    ).value = "31.02.2027"

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))

    await ui_user.should_see("Некорректные данные облигации")
    bond_service.update.assert_not_awaited()


async def test_update_unexpected_error(ui_user, valid_token, bond_service) -> None:
    """Сбой сервиса при правке: общий тост об ошибке."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.update.side_effect = RuntimeError("db down")

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))

    await ui_user.should_see("Ошибка: db down")


async def test_delete_without_selection_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Кнопка удаления без выбора строки — предупреждение."""
    _open_bonds_page(ui_user, valid_token, bond_service)

    await ui_user.open("/bonds")
    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_card))

    await ui_user.should_see("Сначала выберите облигацию в таблице")
    bond_service.delete.assert_not_awaited()


async def test_delete_success(ui_user, valid_token, bond_service) -> None:
    """Удаление через диалог подтверждения."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.delete.return_value = True

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    delete_button = one(ui_user, kind=ui.button, content="Удалить", within=edit_card)
    click(ui_user, delete_button)

    await ui_user.should_see("Удалить выбранную облигацию?")

    dialog = one(ui_user, kind=ui.dialog)
    confirm_button = one(ui_user, kind=ui.button, content="Удалить", within=dialog)
    click(ui_user, confirm_button)

    await ui_user.should_see("Облигация удалена")
    bond_service.delete.assert_awaited_once_with(1, user_id=1)
    await eventually(lambda: bond_service.list_all.await_count == 2)


async def test_delete_not_found_shows_warning(ui_user, valid_token, bond_service) -> None:
    """Облигация уже удалена: предупреждение и перезагрузка."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.delete.return_value = False

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_card))
    await ui_user.should_see("Удалить выбранную облигацию?")

    dialog = one(ui_user, kind=ui.dialog)
    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=dialog))

    await ui_user.should_see("Облигация не найдена")


async def test_delete_cancel_keeps_bond(ui_user, valid_token, bond_service) -> None:
    """Отмена в диалоге не удаляет облигацию."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_card))
    await ui_user.should_see("Удалить выбранную облигацию?")

    dialog = one(ui_user, kind=ui.dialog)
    click(ui_user, one(ui_user, kind=ui.button, content="Отмена", within=dialog))

    await eventually(lambda: not dialog.value)
    bond_service.delete.assert_not_awaited()


async def test_delete_service_error_shows_notification(ui_user, valid_token, bond_service) -> None:
    """Сбой сервиса при удалении: тост об ошибке, без перезагрузки таблицы."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])
    bond_service.delete.side_effect = RuntimeError("db down")

    await ui_user.open("/bonds")
    await _select_first_bond(ui_user)

    (_create_card, edit_card, _dialog_card) = cards(ui_user)
    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=edit_card))
    await ui_user.should_see("Удалить выбранную облигацию?")

    dialog = one(ui_user, kind=ui.dialog)
    click(ui_user, one(ui_user, kind=ui.button, content="Удалить", within=dialog))

    await ui_user.should_see("Ошибка: db down")


async def test_select_row_with_stale_bond_id_resets_selection(
    ui_user, valid_token, bond_service
) -> None:
    """Выбор строки с идентификатором вне кэша сбрасывает выделение."""
    _open_bonds_page(ui_user, valid_token, bond_service, [make_bond()])

    await ui_user.open("/bonds")
    [table] = tables(ui_user)
    select_table_row(ui_user, table, {"id": 999})

    # Выделение сброшено: сохранение требует сначала выбрать облигацию.
    click(ui_user, one(ui_user, kind=ui.button, content="Сохранить изменения"))
    await ui_user.should_see("Сначала выберите облигацию в таблице")
    bond_service.update.assert_not_awaited()
