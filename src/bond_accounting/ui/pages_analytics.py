"""Страница аналитики (``/analytics``): сводка портфеля пользователя."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nicegui import ui

from bond_accounting.analytics.dto import (
    CASHFLOWS_HORIZON_DAYS_DEFAULT,
    CASHFLOWS_HORIZON_DAYS_MAX,
    CASHFLOWS_HORIZON_DAYS_MIN,
    CASHFLOWS_LIMIT_DEFAULT,
    CASHFLOWS_LIMIT_MAX,
    CASHFLOWS_LIMIT_MIN,
    NEXT_COUPONS_HORIZON_DAYS_DEFAULT,
    NEXT_COUPONS_HORIZON_DAYS_MAX,
    NEXT_COUPONS_HORIZON_DAYS_MIN,
    NEXT_COUPONS_LIMIT_DEFAULT,
    NEXT_COUPONS_LIMIT_MAX,
    NEXT_COUPONS_LIMIT_MIN,
)
from bond_accounting.ui.common import (
    fmt_date,
    fmt_datetime,
    fmt_money,
    fmt_percent,
    get_current_user,
    notify_error,
    page_header,
)

if TYPE_CHECKING:
    from bond_accounting.analytics.dto import PortfolioSummary
    from bond_accounting.analytics.service import AnalyticsService
    from bond_accounting.auth.jwt_service import JwtService

logger = logging.getLogger(__name__)

_POSITION_COLUMNS: list[dict[str, Any]] = [
    {"name": "isin", "label": "ISIN", "field": "isin", "align": "left", "sortable": True},
    {"name": "name", "label": "Название", "field": "name", "align": "left"},
    {"name": "quantity", "label": "Количество", "field": "quantity", "align": "right"},
    {"name": "avg_buy_price", "label": "Средняя цена", "field": "avg_buy_price", "align": "right"},
    {"name": "total_invested", "label": "Вложено", "field": "total_invested", "align": "right"},
    {"name": "ytm", "label": "YTM", "field": "ytm", "align": "right"},
    {
        "name": "current_yield",
        "label": "Текущая доходность",
        "field": "current_yield",
        "align": "right",
    },
    {"name": "accrued_coupon", "label": "НКД", "field": "accrued_coupon", "align": "right"},
    {
        "name": "next_coupon_date",
        "label": "Следующий купон",
        "field": "next_coupon_date",
        "align": "right",
    },
    {"name": "maturity_date", "label": "Погашение", "field": "maturity_date", "align": "right"},
]

_COUPON_COLUMNS: list[dict[str, Any]] = [
    {"name": "date", "label": "Дата", "field": "date", "align": "right", "sortable": True},
    {"name": "isin", "label": "ISIN", "field": "isin", "align": "left"},
    {"name": "name", "label": "Название", "field": "name", "align": "left"},
    {"name": "amount", "label": "Сумма", "field": "amount", "align": "right"},
]

_CASHFLOW_COLUMNS: list[dict[str, Any]] = [
    {"name": "date", "label": "Дата", "field": "date", "align": "right", "sortable": True},
    {"name": "isin", "label": "ISIN", "field": "isin", "align": "left"},
    {"name": "kind", "label": "Тип", "field": "kind", "align": "left"},
    {"name": "amount", "label": "Сумма", "field": "amount", "align": "right"},
]

#: Подписи типов будущих выплат.
_CASHFLOW_KINDS: dict[str, str] = {
    "COUPON": "Купон",
    "MATURITY": "Погашение",
}


def _int_value(number: ui.number, default: int) -> int:
    """Значение числового поля как int (default — если поле очищено)."""
    value = number.value
    return int(value) if value is not None else default


def _summary_rows(summary: PortfolioSummary) -> list[dict[str, Any]]:
    """Строки таблицы позиций с аналитикой из сводки."""
    return [
        {
            "isin": position.isin,
            "name": position.name,
            "quantity": position.quantity,
            "avg_buy_price": fmt_money(position.avg_buy_price),
            "total_invested": fmt_money(position.total_invested),
            "ytm": fmt_percent(position.ytm),
            "current_yield": fmt_percent(position.current_yield),
            "accrued_coupon": fmt_money(position.accrued_coupon),
            "next_coupon_date": fmt_date(position.next_coupon_date),
            "maturity_date": fmt_date(position.maturity_date),
        }
        for position in summary.positions
    ]


def _coupon_rows(summary: PortfolioSummary) -> list[dict[str, Any]]:
    """Строки таблицы ближайших купонов."""
    return [
        {
            "date": fmt_date(coupon.date),
            "isin": coupon.isin,
            "name": coupon.name,
            "amount": fmt_money(coupon.amount),
        }
        for coupon in summary.next_coupons
    ]


def _cashflow_rows(summary: PortfolioSummary) -> list[dict[str, Any]]:
    """Строки таблицы будущих денежных потоков."""
    return [
        {
            "date": fmt_date(cashflow.date),
            "isin": cashflow.isin,
            "kind": _CASHFLOW_KINDS.get(cashflow.kind, cashflow.kind),
            "amount": fmt_money(cashflow.amount),
        }
        for cashflow in summary.upcoming_cashflows
    ]


def register_analytics_pages(jwt_service: JwtService, analytics_service: AnalyticsService) -> None:
    """Зарегистрировать страницу ``/analytics`` со сводкой портфеля.

    Args:
        jwt_service: Сервис проверки JWT из cookie.
        analytics_service: Сервис расчёта аналитики портфеля.
    """

    @ui.page("/analytics")
    async def analytics_page() -> None:
        """Сводка портфеля: итоги, реализованный результат, купоны, потоки."""
        user = get_current_user(jwt_service)
        if user is None:
            return

        async def reload() -> None:
            """Пересчитать сводку и обновить все элементы страницы."""
            try:
                summary = await analytics_service.get_portfolio_summary(
                    user.user_id,
                    horizon_days=_int_value(horizon_input, NEXT_COUPONS_HORIZON_DAYS_DEFAULT),
                    limit=_int_value(limit_input, NEXT_COUPONS_LIMIT_DEFAULT),
                    cashflows_horizon_days=_int_value(
                        cashflows_horizon_input, CASHFLOWS_HORIZON_DAYS_DEFAULT
                    ),
                    cashflows_limit=_int_value(cashflows_limit_input, CASHFLOWS_LIMIT_DEFAULT),
                )
            except Exception as exc:
                notify_error(exc)
                return
            total_label.set_text(f"Всего вложено: {fmt_money(summary.total_invested)}")
            pnl_label.set_text(
                f"Реализованный результат — продажи: {fmt_money(summary.realized_pnl.sells)}, "
                f"погашения: {fmt_money(summary.realized_pnl.maturities)}, "
                f"комиссии: {fmt_money(summary.realized_pnl.commissions)}, "
                f"итого: {fmt_money(summary.realized_pnl.total)}"
            )
            generated_label.set_text(f"Сформировано: {fmt_datetime(summary.generated_at)}")
            positions_table.rows = _summary_rows(summary)
            coupons_table.rows = _coupon_rows(summary)
            cashflows_table.rows = _cashflow_rows(summary)

        page_header("/analytics")
        with ui.column().classes("w-full max-w-6xl mx-auto gap-6"):
            ui.label("Аналитика портфеля").classes("text-h5")
            with ui.row().classes("items-end"):
                horizon_input = ui.number(
                    "Горизонт купонов, дней",
                    value=NEXT_COUPONS_HORIZON_DAYS_DEFAULT,
                    min=NEXT_COUPONS_HORIZON_DAYS_MIN,
                    max=NEXT_COUPONS_HORIZON_DAYS_MAX,
                    step=1,
                )
                limit_input = ui.number(
                    "Купонов максимум",
                    value=NEXT_COUPONS_LIMIT_DEFAULT,
                    min=NEXT_COUPONS_LIMIT_MIN,
                    max=NEXT_COUPONS_LIMIT_MAX,
                    step=1,
                )
                cashflows_horizon_input = ui.number(
                    "Горизонт потоков, дней",
                    value=CASHFLOWS_HORIZON_DAYS_DEFAULT,
                    min=CASHFLOWS_HORIZON_DAYS_MIN,
                    max=CASHFLOWS_HORIZON_DAYS_MAX,
                    step=1,
                )
                cashflows_limit_input = ui.number(
                    "Потоков максимум",
                    value=CASHFLOWS_LIMIT_DEFAULT,
                    min=CASHFLOWS_LIMIT_MIN,
                    max=CASHFLOWS_LIMIT_MAX,
                    step=1,
                )
                ui.button("Обновить", icon="refresh", on_click=reload)

            with ui.card().classes("w-full"):
                ui.label("Сводка").classes("text-subtitle1")
                total_label = ui.label()
                pnl_label = ui.label()
                generated_label = ui.label().classes("text-caption")

            with ui.card().classes("w-full"):
                ui.label("Позиции").classes("text-subtitle1")
                positions_table = ui.table(
                    columns=_POSITION_COLUMNS, rows=[], row_key="isin"
                ).classes("w-full")

            with ui.card().classes("w-full"):
                ui.label("Ближайшие купоны").classes("text-subtitle1")
                coupons_table = ui.table(columns=_COUPON_COLUMNS, rows=[]).classes("w-full")

            with ui.card().classes("w-full"):
                ui.label("Будущие денежные потоки").classes("text-subtitle1")
                cashflows_table = ui.table(columns=_CASHFLOW_COLUMNS, rows=[]).classes("w-full")

        await reload()
