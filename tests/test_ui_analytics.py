"""UI-тесты страницы аналитики (``/analytics``): сводка и таблицы портфеля."""

from __future__ import annotations

from datetime import date

from nicegui import ui

from bond_accounting.analytics.dto import Cashflow
from tests._ui_utils import (
    authenticate,
    click,
    eventually,
    make_summary,
    one,
    tables,
)


async def test_analytics_page_renders_summary(ui_user, valid_token, analytics_service) -> None:
    """Сводка: итоги, реализованный результат, время генерации."""
    analytics_service.get_portfolio_summary.return_value = make_summary()
    authenticate(ui_user, valid_token)

    await ui_user.open("/analytics")
    await ui_user.should_see("Аналитика портфеля")
    await ui_user.should_see("Всего вложено: 13305.00")
    await ui_user.should_see("Реализованный результат — продажи: 100.00")
    await ui_user.should_see("погашения: 50.00")
    await ui_user.should_see("комиссии: 5.00")
    await ui_user.should_see("итого: 145.00")
    await ui_user.should_see("Сформировано: 01.02.2026 10:30")
    analytics_service.get_portfolio_summary.assert_awaited_once_with(
        1, horizon_days=730, limit=100, cashflows_horizon_days=3650, cashflows_limit=500
    )


async def test_analytics_tables_populated(ui_user, valid_token, analytics_service) -> None:
    """Таблицы позиций, купонов и потоков заполняются отформатированными данными."""
    analytics_service.get_portfolio_summary.return_value = make_summary()
    authenticate(ui_user, valid_token)

    await ui_user.open("/analytics")
    positions_table, coupons_table, cashflows_table = tables(ui_user)

    assert positions_table.rows[0] == {
        "isin": "RU000A0JX0J2",
        "name": "ОФЗ 26207",
        "quantity": 10,
        "avg_buy_price": "980.50",
        "total_invested": "9805.00",
        "ytm": "9.15%",
        "current_yield": "8.30%",
        "accrued_coupon": "12.34",
        "next_coupon_date": "04.06.2026",
        "maturity_date": "04.02.2027",
    }
    # Позиция без данных о доходности отображает прочерки.
    assert positions_table.rows[1]["ytm"] == "—"
    assert positions_table.rows[1]["current_yield"] == "—"
    assert positions_table.rows[1]["next_coupon_date"] == "—"

    assert coupons_table.rows == [
        {
            "date": "04.06.2026",
            "isin": "RU000A0JX0J2",
            "name": "ОФЗ 26207",
            "amount": "407.50",
        }
    ]

    assert cashflows_table.rows == [
        {
            "date": "04.06.2026",
            "isin": "RU000A0JX0J2",
            "kind": "Купон",
            "amount": "407.50",
        },
        {
            "date": "04.02.2027",
            "isin": "RU000A0JX0J2",
            "kind": "Погашение",
            "amount": "10000.00",
        },
    ]


async def test_analytics_cashflow_unknown_kind_shown_as_is(
    ui_user, valid_token, analytics_service
) -> None:
    """Неизвестный тип выплаты отображается без перевода."""
    summary = make_summary(
        upcoming_cashflows=[
            Cashflow(
                date=date(2026, 6, 4), bond_id=1, isin="RU000A0JX0J2", kind="OTHER", amount=1.0
            )
        ]
    )
    analytics_service.get_portfolio_summary.return_value = summary
    authenticate(ui_user, valid_token)

    await ui_user.open("/analytics")
    _positions_table, _coupons_table, cashflows_table = tables(ui_user)
    assert cashflows_table.rows[0]["kind"] == "OTHER"


async def test_analytics_load_error_shows_notification(
    ui_user, valid_token, analytics_service
) -> None:
    """Сбой расчёта сводки: тост с ошибкой, таблицы пусты."""
    analytics_service.get_portfolio_summary.side_effect = RuntimeError("calc failed")
    authenticate(ui_user, valid_token)

    await ui_user.open("/analytics")
    await ui_user.should_see("Ошибка: calc failed")


async def test_analytics_refresh_button_recalculates(
    ui_user, valid_token, analytics_service
) -> None:
    """Кнопка «Обновить» пересчитывает сводку."""
    analytics_service.get_portfolio_summary.return_value = make_summary()
    authenticate(ui_user, valid_token)

    await ui_user.open("/analytics")
    click(ui_user, one(ui_user, kind=ui.button, content="Обновить"))

    await eventually(lambda: analytics_service.get_portfolio_summary.await_count == 2)
