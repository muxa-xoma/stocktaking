"""UI-тесты страницы сделок (``/transactions``): история и модальный ввод сделок.

Сервисы замоканы (``create_autospec``). Окно создания открывается кнопкой
«Добавить» на странице; внутри окна — поля формы и кнопка подтверждения.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from nicegui import ui

from bond_accounting.portfolio.service import PortfolioError
from bond_accounting.ui.pages_transactions import (
    TransactionsPage,
    _ceil_to_2,
    _TransactionForm,
    _TransactionsState,
)
from tests.ui._ui_utils import (
    authenticate,
    click,
    elements,
    eventually,
    make_account,
    make_bond,
    make_transaction,
    one,
    tables,
)


def _dialogs(user: Any) -> tuple[Any, Any, Any]:
    """Диалоги страницы в порядке создания: создание, правка, подтверждение."""
    found = elements(user, kind=ui.dialog)
    assert len(found) == 3, f"expected 3 dialogs, found {len(found)}"
    return found[0], found[1], found[2]


def _open_create_dialog(user: Any) -> Any:
    """Нажать «Добавить» на странице и вернуть открывшееся окно создания.

    На странице две кнопки «Добавить»: заголовок (открывает окно) и
    подтверждение внутри самого окна; по порядку создания заголовок — первая.
    """
    create_dialog, _edit_dialog, _confirm_dialog = _dialogs(user)
    add_buttons = elements(user, kind=ui.button, content="Добавить")
    assert len(add_buttons) == 2, "expected header and dialog 'Добавить' buttons"
    click(user, add_buttons[0])
    assert create_dialog.value is True
    return create_dialog


def _submit_create_dialog(user: Any, create_dialog: Any) -> None:
    """Нажать «Добавить» внутри окна создания (кнопка подтверждения)."""
    click(user, one(user, kind=ui.button, content="Добавить", within=create_dialog))


async def test_transactions_page_renders_history(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Таблица сделок: форматирование даты, типа, цены и комиссии."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.return_value = [
        make_transaction(),
        make_transaction(id=2, type="SELL", quantity=3, price=1010.25, commission=0),
    ]
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    await ui_user.should_see("Сделки")
    await ui_user.should_see("Новая сделка")

    [table] = tables(ui_user)
    assert table.rows == [
        {
            "id": 1,
            "date": "10.01.2026",
            "isin": "RU000A0JX0J2",
            "account": "#1",
            "type": "Покупка",
            "quantity": 10,
            "price": "980.50",
            "commission": "15.00",
        },
        {
            "id": 2,
            "date": "10.01.2026",
            "isin": "RU000A0JX0J2",
            "account": "#1",
            "type": "Продажа",
            "quantity": 3,
            "price": "1010.25",
            "commission": "0.00",
        },
    ]
    portfolio_service.list_transactions.assert_awaited_with(1)


async def test_transactions_bond_load_error_shows_notification(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Сбой загрузки справочника: тост с ошибкой, страница рендерится."""
    bond_service.list_all.side_effect = RuntimeError("db down")
    portfolio_service.list_transactions.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    await ui_user.should_see("Ошибка: db down")
    await ui_user.should_see("Новая сделка")


async def test_transactions_history_load_error_shows_notification(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Сбой загрузки истории сделок: тост с ошибкой."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.side_effect = RuntimeError("db down")
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    await ui_user.should_see("Ошибка: db down")


async def test_transaction_with_deleted_bond_shows_placeholder(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Сделка по удалённой облигации показывает #id вместо ISIN."""
    bond_service.list_all.return_value = []
    portfolio_service.list_transactions.return_value = [make_transaction(bond_id=999)]
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    [table] = tables(ui_user)
    assert table.rows[0]["isin"] == "#999"


async def test_add_transaction_success(
    ui_user, valid_token, portfolio_service, bond_service, broker_service
) -> None:
    """Форма сделки: значения передаются в сервис, история обновляется."""
    bond_service.list_all.return_value = [make_bond()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    portfolio_service.list_transactions.return_value = []
    portfolio_service.add_transaction.return_value = make_transaction()
    # Брокер счёта не найден — подсказка комиссии показывает «—», без арифметики.
    broker_service.get_account.return_value = None
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    create_dialog = _open_create_dialog(ui_user)

    bond_select = one(ui_user, kind=ui.select, content="Облигация", within=create_dialog)
    account_select = one(ui_user, kind=ui.select, content="Брокер / Счёт", within=create_dialog)
    type_select = one(ui_user, kind=ui.select, content="Тип", within=create_dialog)
    quantity_input = one(ui_user, kind=ui.number, content="Количество", within=create_dialog)
    price_input = one(ui_user, kind=ui.number, content="Цена", within=create_dialog)
    date_input = one(ui_user, kind=ui.date_input, content="Дата", within=create_dialog)
    commission_input = one(ui_user, kind=ui.number, content="Комиссия", within=create_dialog)

    # Первый (и единственный) счёт предвыбран селектором автоматически.
    assert account_select.value == 1
    bond_select.value = 1
    type_select.value = "SELL"
    quantity_input.value = 5
    price_input.value = 1010.5
    date_input.value = "2026-02-01"
    commission_input.value = 10

    _submit_create_dialog(ui_user, create_dialog)

    await ui_user.should_see("Сделка записана")
    user_id, txn = portfolio_service.add_transaction.await_args.args
    assert user_id == 1
    assert txn.bond_id == 1
    assert txn.broker_account_id == 1
    assert txn.type == "SELL"
    assert txn.quantity == 5
    assert txn.price == 1010.5
    assert txn.date.isoformat() == "2026-02-01"
    assert txn.commission == 10
    await eventually(lambda: portfolio_service.list_transactions.await_count == 2)
    assert create_dialog.value is False


async def test_add_transaction_without_bond_shows_notification(
    ui_user, valid_token, portfolio_service, bond_service, broker_service
) -> None:
    """Облигация не выбрана: предупреждение, сервис не вызывается."""
    bond_service.list_all.return_value = [make_bond()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    portfolio_service.list_transactions.return_value = []
    broker_service.get_account.return_value = None
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    create_dialog = _open_create_dialog(ui_user)
    _submit_create_dialog(ui_user, create_dialog)

    await ui_user.should_see("Выберите облигацию")
    portfolio_service.add_transaction.assert_not_awaited()


async def test_add_transaction_invalid_date_shows_validation_error(
    ui_user, valid_token, portfolio_service, bond_service, broker_service
) -> None:
    """Пустая дата сделки: ошибка валидации, сервис не вызывается."""
    bond_service.list_all.return_value = [make_bond()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    portfolio_service.list_transactions.return_value = []
    broker_service.get_account.return_value = None
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    create_dialog = _open_create_dialog(ui_user)
    one(ui_user, kind=ui.select, content="Облигация", within=create_dialog).value = 1
    _submit_create_dialog(ui_user, create_dialog)

    await ui_user.should_see("Некорректные данные сделки")
    portfolio_service.add_transaction.assert_not_awaited()


async def test_add_transaction_portfolio_error_shows_notification(
    ui_user, valid_token, portfolio_service, bond_service, broker_service
) -> None:
    """Бизнес-ошибка сервиса (например, продажа без позиции) показывается тостом."""
    bond_service.list_all.return_value = [make_bond()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    portfolio_service.list_transactions.return_value = []
    portfolio_service.add_transaction.side_effect = PortfolioError("Недостаточно облигаций")
    broker_service.get_account.return_value = None
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    create_dialog = _open_create_dialog(ui_user)
    one(ui_user, kind=ui.select, content="Облигация", within=create_dialog).value = 1
    one(ui_user, kind=ui.date_input, content="Дата", within=create_dialog).value = "2026-02-01"
    one(ui_user, kind=ui.number, content="Количество", within=create_dialog).value = 5
    # A positive price is required: TransactionCreate rejects price <= 0 with a
    # ValidationError before the mocked PortfolioError path would run.
    one(ui_user, kind=ui.number, content="Цена", within=create_dialog).value = 1010.5
    _submit_create_dialog(ui_user, create_dialog)

    await ui_user.should_see("Недостаточно облигаций")


async def test_add_transaction_unexpected_error(
    ui_user, valid_token, portfolio_service, bond_service, broker_service
) -> None:
    """Сбой сервиса при записи сделки: общий тост об ошибке."""
    bond_service.list_all.return_value = [make_bond()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    portfolio_service.list_transactions.return_value = []
    portfolio_service.add_transaction.side_effect = RuntimeError("db down")
    broker_service.get_account.return_value = None
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    create_dialog = _open_create_dialog(ui_user)
    one(ui_user, kind=ui.select, content="Облигация", within=create_dialog).value = 1
    one(ui_user, kind=ui.number, content="Количество", within=create_dialog).value = 5
    one(ui_user, kind=ui.number, content="Цена", within=create_dialog).value = 1010.5
    one(ui_user, kind=ui.date_input, content="Дата", within=create_dialog).value = "2026-02-01"
    _submit_create_dialog(ui_user, create_dialog)

    await ui_user.should_see("Ошибка: db down")


async def test_refresh_button_reloads_history(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Кнопка «Обновить» повторно загружает историю."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    click(ui_user, one(ui_user, kind=ui.button, content="Обновить"))

    await eventually(lambda: portfolio_service.list_transactions.await_count == 2)


# --------------------------------------------------------------------------- #
# _ceil_to_2: округление вверх до 2 знаков (чистая функция, без UI)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(2.351, 2.36, id="basic-ceil"),
        pytest.param(2.36, 2.36, id="exact-value-not-bumped"),
        pytest.param(7.0 / 3, 2.34, id="repeating-fraction"),
        pytest.param(100.0 / 3, 33.34, id="large-repeating-fraction"),
        pytest.param(0.005, 0.01, id="half-cent-up"),
        pytest.param(0.001, 0.01, id="sub-cent-up"),
        pytest.param(5.0, 5.0, id="whole-number-unchanged"),
        pytest.param(0.0, 0.0, id="zero-unchanged"),
        # Шум двоичной арифметики: 0.07 * 3 / 3 == 0.07000000000000001,
        # «сырой» ceil даст 0.08 — guard через round(..., 9) должен вернуть 0.07.
        pytest.param(0.07 * 3 / 3, 0.07, id="float-noise-guard"),
    ],
)
def test_ceil_to_2(value: float, expected: float) -> None:
    """``_ceil_to_2`` округляет вверх до 2 знаков, не поднимая ровные значения."""
    assert _ceil_to_2(value) == expected


async def test_create_dialog_resets_after_success(
    ui_user, valid_token, portfolio_service, bond_service, broker_service
) -> None:
    """F5: форма новой сделки очищается после успешной записи."""
    bond_service.list_all.return_value = [make_bond()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    portfolio_service.list_transactions.return_value = []
    portfolio_service.add_transaction.return_value = make_transaction()
    broker_service.get_account.return_value = None
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    create_dialog = _open_create_dialog(ui_user)

    one(ui_user, kind=ui.select, content="Облигация", within=create_dialog).value = 1
    one(ui_user, kind=ui.select, content="Тип", within=create_dialog).value = "SELL"
    one(ui_user, kind=ui.number, content="Количество", within=create_dialog).value = 5
    one(ui_user, kind=ui.number, content="Цена", within=create_dialog).value = 1010.5
    one(ui_user, kind=ui.date_input, content="Дата", within=create_dialog).value = "2026-02-01"
    one(ui_user, kind=ui.number, content="Комиссия", within=create_dialog).value = 10

    _submit_create_dialog(ui_user, create_dialog)
    await ui_user.should_see("Сделка записана")
    assert create_dialog.value is False

    _open_create_dialog(ui_user)
    assert one(ui_user, kind=ui.select, content="Тип", within=create_dialog).value == "BUY"
    assert one(ui_user, kind=ui.number, content="Количество", within=create_dialog).value is None
    assert one(ui_user, kind=ui.number, content="Цена", within=create_dialog).value is None
    assert one(ui_user, kind=ui.number, content="Комиссия", within=create_dialog).value == 0


async def test_create_dialog_resets_after_cancel(
    ui_user, valid_token, portfolio_service, bond_service, broker_service
) -> None:
    """F5: введённые значения не сохраняются после отмены новой сделки."""
    bond_service.list_all.return_value = [make_bond()]
    broker_service.list_accounts_for_user.return_value = [make_account()]
    portfolio_service.list_transactions.return_value = []
    broker_service.get_account.return_value = None
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    create_dialog = _open_create_dialog(ui_user)

    one(ui_user, kind=ui.select, content="Облигация", within=create_dialog).value = 1
    one(ui_user, kind=ui.select, content="Тип", within=create_dialog).value = "SELL"
    one(ui_user, kind=ui.number, content="Количество", within=create_dialog).value = 5
    one(ui_user, kind=ui.number, content="Цена", within=create_dialog).value = 1010.5
    one(ui_user, kind=ui.date_input, content="Дата", within=create_dialog).value = "2026-02-01"
    one(ui_user, kind=ui.number, content="Комиссия", within=create_dialog).value = 10

    click(ui_user, one(ui_user, kind=ui.button, content="Отмена", within=create_dialog))
    assert create_dialog.value is False

    _open_create_dialog(ui_user)
    assert one(ui_user, kind=ui.select, content="Тип", within=create_dialog).value == "BUY"
    assert one(ui_user, kind=ui.number, content="Количество", within=create_dialog).value is None
    assert one(ui_user, kind=ui.number, content="Цена", within=create_dialog).value is None
    assert one(ui_user, kind=ui.number, content="Комиссия", within=create_dialog).value == 0


# --------------------------------------------------------------------------- #
# _gather_form: приведение цены/комиссии из режима ввода к формату хранения.
# Единичные вызовы метода страницы с заранее заполненной формой (легковесные
# поля через SimpleNamespace — методу нужны только ``.value`` и none из этих
# путей не доходит до ``ui.notify``/``parse_date`` сбрасывают только ошибки).
# --------------------------------------------------------------------------- #


def _form(**values: Any) -> _TransactionForm:
    """Форма с реальными виджетами, где это нужно, иначе лёгкие заглушки ``.value``."""
    defaults = {
        "account_select": SimpleNamespace(value=1),
        "bond_select": SimpleNamespace(value=1),
        "type_select": SimpleNamespace(value="BUY"),
        "quantity_input": SimpleNamespace(value=5),
        "price_input": SimpleNamespace(value=1010.5),
        "price_mode_toggle": SimpleNamespace(value="paper"),
        "commission_input": SimpleNamespace(value=10),
        "commission_mode_toggle": SimpleNamespace(value="paper"),
        "trade_date_input": SimpleNamespace(value="2026-02-01"),
        "commission_hint": None,
    }
    defaults.update(values)
    return _TransactionForm(**defaults)


def _make_broker(
    *, commission: float = 0.1, min_commission: float = 50, min_commission_type: str = "PERCENT"
) -> SimpleNamespace:
    """Лёгкий объект тарифа брокера: только нужные подсказке атрибуты."""
    return SimpleNamespace(
        commission=commission,
        min_commission=min_commission,
        min_commission_type=min_commission_type,
    )


def test_gather_form_trade_price_divides_by_quantity(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """Режим «за сделку»: цена = цена_сделки / количество, с ceil до 2 знаков."""
    page = TransactionsPage(jwt_service, portfolio_service, bond_service, broker_service)
    form = _form(price_mode_toggle=SimpleNamespace(value="trade"))
    result = page._gather_form(_TransactionsState(user_id=1), form)
    assert result is not None
    assert result["price"] == _ceil_to_2(1010.5 / 5) == pytest.approx(202.1)


def test_gather_form_paper_price_is_raw(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """Режим «за бумагу»: цена берётся как введена (без деления)."""
    page = TransactionsPage(jwt_service, portfolio_service, bond_service, broker_service)
    form = _form(price_mode_toggle=SimpleNamespace(value="paper"))
    result = page._gather_form(_TransactionsState(user_id=1), form)
    assert result is not None
    assert result["price"] == 1010.5


def test_gather_form_trade_commission_divides_by_quantity(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """Режим «за сделку»: комиссия = комиссия_сделки / количество."""
    page = TransactionsPage(jwt_service, portfolio_service, bond_service, broker_service)
    form = _form(commission_mode_toggle=SimpleNamespace(value="trade"))
    result = page._gather_form(_TransactionsState(user_id=1), form)
    assert result is not None
    assert result["commission"] == _ceil_to_2(10 / 5) == pytest.approx(2.0)


def test_gather_form_paper_commission_is_raw(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """Режим «за бумагу»: комиссия берётся как введена (без деления)."""
    page = TransactionsPage(jwt_service, portfolio_service, bond_service, broker_service)
    form = _form(commission_mode_toggle=SimpleNamespace(value="paper"))
    result = page._gather_form(_TransactionsState(user_id=1), form)
    assert result is not None
    assert result["commission"] == 10


def test_gather_form_mature_forces_zero_commission(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """Тип MATURE: комиссия всегда 0, даже в режиме «за сделку» с вводом."""
    page = TransactionsPage(jwt_service, portfolio_service, bond_service, broker_service)
    form = _form(
        type_select=SimpleNamespace(value="MATURE"),
        commission_mode_toggle=SimpleNamespace(value="trade"),
        commission_input=SimpleNamespace(value=10),
    )
    result = page._gather_form(_TransactionsState(user_id=1), form)
    assert result is not None
    assert result["type"] == "MATURE"
    assert result["commission"] == 0.0


# --------------------------------------------------------------------------- #
# _update_commission_hint: расчёт подсказки по тарифу брокера счёта.
# Чёрный ящик: реальный метод страницы + мок-сервисы; поля формы — заглушки
# с ``.value``; подсказка — лёгкий лейбл, записывающий ``set_text`` (реальный
# ``ui.label`` из рендера недоступен, т.к. страница не рендерится — см. ниже).
# --------------------------------------------------------------------------- #


class _FakeLabel:
    """Мини-лейбл: ``set_text`` пишет в ``text``, как NiceGUI ``ui.label``."""

    def __init__(self) -> None:
        self.text = ""

    def set_text(self, text: str) -> None:
        self.text = text


def _page_with_broker(
    jwt_service,
    portfolio_service,
    bond_service,
    broker_service,
    broker: Any,
    *,
    has_account: bool = True,
) -> TransactionsPage:
    """Страница с загруженным тарифом брокера (или его отсутствием)."""
    broker_service.get_account.return_value = make_account() if has_account else None
    if has_account:
        broker_service.get_broker.return_value = broker
    return TransactionsPage(jwt_service, portfolio_service, bond_service, broker_service)


async def test_commission_hint_percent_max_rate(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """PERCENT: ставка берётся как max(commission, min_commission) процентов.

    paper, quantity=10, price=1000 → total=10000, effective=50% → 5000.00.
    """
    page = _page_with_broker(
        jwt_service,
        portfolio_service,
        bond_service,
        broker_service,
        _make_broker(commission=0.1, min_commission=50, min_commission_type="PERCENT"),
    )
    state = _TransactionsState(user_id=1)
    form = _form(
        quantity_input=SimpleNamespace(value=10),
        price_input=SimpleNamespace(value=1000),
        price_mode_toggle=SimpleNamespace(value="paper"),
        commission_hint=_FakeLabel(),
    )
    await page._update_commission_hint(state, form)
    assert form.commission_hint.text == "Примерная комиссия: 5000.00"


async def test_commission_hint_rubles_min_amount(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """RUBLES: берётся max(процент_от_суммы, min_commission) рублей.

    paper, quantity=10, price=1000 → total=10000, calc=10 ₽, max(10,50)=50 ₽.
    """
    page = _page_with_broker(
        jwt_service,
        portfolio_service,
        bond_service,
        broker_service,
        _make_broker(commission=0.1, min_commission=50, min_commission_type="RUBLES"),
    )
    state = _TransactionsState(user_id=1)
    form = _form(
        quantity_input=SimpleNamespace(value=10),
        price_input=SimpleNamespace(value=1000),
        price_mode_toggle=SimpleNamespace(value="paper"),
        commission_hint=_FakeLabel(),
    )
    await page._update_commission_hint(state, form)
    assert form.commission_hint.text == "Примерная комиссия: 50.00"


async def test_commission_hint_trade_uses_per_trade_base(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """Режим «за сделку»: базой служит цена сделки, не количество × цена."""
    page = _page_with_broker(
        jwt_service,
        portfolio_service,
        bond_service,
        broker_service,
        _make_broker(commission=0.1, min_commission=0, min_commission_type="RUBLES"),
    )
    state = _TransactionsState(user_id=1)
    # total=1000 (база «за сделку»): 1000*0.1/100 = 1.00; «за бумагу» было бы 10.00.
    form = _form(
        quantity_input=SimpleNamespace(value=10),
        price_input=SimpleNamespace(value=1000),
        price_mode_toggle=SimpleNamespace(value="trade"),
        commission_hint=_FakeLabel(),
    )
    await page._update_commission_hint(state, form)
    assert form.commission_hint.text == "Примерная комиссия: 1.00"


async def test_commission_hint_mature_shows_dash(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """MATURE: подсказка не считается — показывается «—»."""
    page = _page_with_broker(
        jwt_service,
        portfolio_service,
        bond_service,
        broker_service,
        _make_broker(),
    )
    state = _TransactionsState(user_id=1)
    # При MATURE ветка возвращается до арифметики — даже с валидными полями.
    form = _form(
        type_select=SimpleNamespace(value="MATURE"),
        quantity_input=SimpleNamespace(value=10),
        price_input=SimpleNamespace(value=1000),
        commission_hint=_FakeLabel(),
    )
    await page._update_commission_hint(state, form)
    assert form.commission_hint.text == "Примерная комиссия: —"


async def test_commission_hint_missing_broker_shows_dash(
    jwt_service, portfolio_service, bond_service, broker_service
) -> None:
    """Брокер счёта не найден: подсказка не считается — «—»."""
    page = _page_with_broker(
        jwt_service,
        portfolio_service,
        bond_service,
        broker_service,
        broker=None,
        has_account=False,
    )
    state = _TransactionsState(user_id=1)
    form = _form(
        quantity_input=SimpleNamespace(value=10),
        price_input=SimpleNamespace(value=1000),
        commission_hint=_FakeLabel(),
    )
    await page._update_commission_hint(state, form)
    assert form.commission_hint.text == "Примерная комиссия: —"
