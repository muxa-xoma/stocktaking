"""UI-тесты обзора портфеля на главной странице (``/``, см. ``pages_home.py``).

Отдельного модуля ``pages_portfolio.py`` в проекте нет: обзор портфеля
(таблица позиций) реализован на главной странице ``/``.
"""

from __future__ import annotations

from nicegui import ui

from tests._ui_utils import (
    authenticate,
    click,
    eventually,
    make_bond,
    make_position,
    one,
    tables,
)


async def test_portfolio_table_populated(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Позиции обогащаются данными облигаций и форматируются в таблице."""
    portfolio_service.get_all_positions.return_value = [make_position()]
    bond_service.list_all.return_value = [make_bond()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    await ui_user.should_see("Портфель")

    [table] = tables(ui_user)
    assert table.rows == [
        {
            "isin": "RU000A0JX0J2",
            "name": "ОФЗ 26207",
            "quantity": 10,
            "avg_buy_price": "980.50",
            "total_invested": "9805.00",
        }
    ]
    bond_service.list_all.assert_awaited_with()
    portfolio_service.get_all_positions.assert_awaited_with(1)


async def test_portfolio_skips_position_with_missing_bond(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Позиция со ссылкой на удалённую облигацию не попадает в таблицу."""
    portfolio_service.get_all_positions.return_value = [
        make_position(bond_id=1),
        make_position(bond_id=999),
    ]
    bond_service.list_all.return_value = [make_bond()]
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    [table] = tables(ui_user)
    assert len(table.rows) == 1
    assert table.rows[0]["isin"] == "RU000A0JX0J2"


async def test_portfolio_refresh_button_reloads_data(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Кнопка «Обновить» повторно загружает позиции."""
    portfolio_service.get_all_positions.return_value = []
    bond_service.list_all.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    refresh_button = one(ui_user, kind=ui.button, content="Обновить")
    click(ui_user, refresh_button)

    await eventually(lambda: portfolio_service.get_all_positions.await_count == 2)


async def test_portfolio_closed_position_not_listed_by_service(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Сервис отдаёт только открытые позиции — UI отображает их как есть."""
    portfolio_service.get_all_positions.return_value = [
        make_position(),
        make_position(bond_id=2, quantity=25, avg_buy_price=700.0, total_invested=17500.0),
    ]
    bond_service.list_all.return_value = [make_bond(), make_bond(id=2, isin="RU000A0JX0J4")]
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    [table] = tables(ui_user)
    assert {row["isin"] for row in table.rows} == {"RU000A0JX0J2", "RU000A0JX0J4"}
