"""UI-тесты страницы сделок (``/transactions``): список и ввод BUY/SELL/MATURE."""

from __future__ import annotations

from nicegui import ui

from bond_accounting.portfolio.service import PortfolioError
from tests._ui_utils import (
    authenticate,
    click,
    eventually,
    make_bond,
    make_transaction,
    one,
    tables,
)


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
            "date": "10.01.2026",
            "isin": "RU000A0JX0J2",
            "type": "Покупка",
            "quantity": 10,
            "price": "980.50",
            "commission": "15.00",
        },
        {
            "date": "10.01.2026",
            "isin": "RU000A0JX0J2",
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
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Форма сделки: значения передаются в сервис, история обновляется."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.return_value = []
    portfolio_service.add_transaction.return_value = make_transaction()
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")

    bond_select = one(ui_user, kind=ui.select, content="Облигация")
    type_select = one(ui_user, kind=ui.select, content="Тип")
    quantity_input = one(ui_user, kind=ui.number, content="Количество")
    price_input = one(ui_user, kind=ui.number, content="Цена")
    date_input = one(ui_user, kind=ui.date_input, content="Дата")
    commission_input = one(ui_user, kind=ui.number, content="Комиссия")

    bond_select.value = 1
    type_select.value = "SELL"
    quantity_input.value = 5
    price_input.value = 1010.5
    date_input.value = "2026-02-01"
    commission_input.value = 10

    click(ui_user, one(ui_user, kind=ui.button, content="Добавить сделку"))

    await ui_user.should_see("Сделка записана")
    user_id, txn = portfolio_service.add_transaction.await_args.args
    assert user_id == 1
    assert txn.bond_id == 1
    assert txn.type == "SELL"
    assert txn.quantity == 5
    assert txn.price == 1010.5
    assert txn.date.isoformat() == "2026-02-01"
    assert txn.commission == 10
    await eventually(lambda: portfolio_service.list_transactions.await_count == 2)


async def test_add_transaction_without_bond_shows_notification(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Облигация не выбрана: предупреждение, сервис не вызывается."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    click(ui_user, one(ui_user, kind=ui.button, content="Добавить сделку"))

    await ui_user.should_see("Выберите облигацию")
    portfolio_service.add_transaction.assert_not_awaited()


async def test_add_transaction_invalid_date_shows_validation_error(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Пустая дата сделки: ошибка валидации, сервис не вызывается."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    one(ui_user, kind=ui.select, content="Облигация").value = 1
    click(ui_user, one(ui_user, kind=ui.button, content="Добавить сделку"))

    await ui_user.should_see("Некорректные данные сделки")
    portfolio_service.add_transaction.assert_not_awaited()


async def test_add_transaction_portfolio_error_shows_notification(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Бизнес-ошибка сервиса (например, продажа без позиции) показывается тостом."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.return_value = []
    portfolio_service.add_transaction.side_effect = PortfolioError("Недостаточно облигаций")
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    one(ui_user, kind=ui.select, content="Облигация").value = 1
    one(ui_user, kind=ui.date_input, content="Дата").value = "2026-02-01"
    one(ui_user, kind=ui.number, content="Количество").value = 5
    # A positive price is required: TransactionCreate rejects price <= 0 with a
    # ValidationError before the mocked PortfolioError path would run.
    one(ui_user, kind=ui.number, content="Цена").value = 1010.5
    click(ui_user, one(ui_user, kind=ui.button, content="Добавить сделку"))

    await ui_user.should_see("Недостаточно облигаций")


async def test_add_transaction_unexpected_error(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Сбой сервиса при записи сделки: общий тост об ошибке."""
    bond_service.list_all.return_value = [make_bond()]
    portfolio_service.list_transactions.return_value = []
    portfolio_service.add_transaction.side_effect = RuntimeError("db down")
    authenticate(ui_user, valid_token)

    await ui_user.open("/transactions")
    one(ui_user, kind=ui.select, content="Облигация").value = 1
    one(ui_user, kind=ui.number, content="Количество").value = 5
    one(ui_user, kind=ui.number, content="Цена").value = 1010.5
    one(ui_user, kind=ui.date_input, content="Дата").value = "2026-02-01"
    click(ui_user, one(ui_user, kind=ui.button, content="Добавить сделку"))

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
