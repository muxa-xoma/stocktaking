"""Помощники UI-тестов: поиск элементов, клики, события, фабрики DTO.

Работают поверх ``nicegui.testing.User``: поиск через ``ElementFilter``
(с ограничением области конкретным элементом-контейнером), вызов
обработчиков через ``nicegui.events.handle_event`` — так же, как это
делает официальный ``UserInteraction.trigger``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from nicegui import ElementFilter, events, ui

from bond_accounting.analytics.dto import (
    Cashflow,
    CouponDue,
    PortfolioSummary,
    PositionAnalytics,
    RealizedPnl,
)
from bond_accounting.bonds.dto import BondDTO
from bond_accounting.brokers.dto import BrokerAccountDTO
from bond_accounting.portfolio.dto import PositionDTO, TransactionDTO

if TYPE_CHECKING:
    from collections.abc import Callable

    from nicegui.element import Element
    from nicegui.testing import User


# --------------------------------------------------------------------------- #
# поиск элементов
# --------------------------------------------------------------------------- #


def elements(
    user: User,
    *,
    kind: type[Element] | None = None,
    content: str | None = None,
    within: Element | None = None,
) -> list[Any]:
    """Элементы страницы в порядке создания, опционально внутри контейнера ``within``."""
    client = user.client
    assert client is not None, "user simulation client is not started"
    with client:
        if kind is None:
            # Перегрузки ElementFilter не допускают kind=None, поэтому две ветки.
            element_filter = ElementFilter(content=content, local_scope=False)
        else:
            element_filter = ElementFilter(kind=kind, content=content, local_scope=False)
        if within is not None:
            element_filter = element_filter.within(instance=within)
        return list(element_filter)


def one(
    user: User,
    *,
    kind: type[Element] | None = None,
    content: str | None = None,
    within: Element | None = None,
) -> Any:
    """Единственный элемент, соответствующий фильтру (иначе — ошибка теста)."""
    found = elements(user, kind=kind, content=content, within=within)
    assert len(found) == 1, (
        f"expected exactly one element (kind={kind}, content={content!r}, "
        f"within={within}), found {len(found)}: {found}"
    )
    return found[0]


def cards(user: User) -> list[Any]:
    """Все карточки страницы в порядке создания."""
    return elements(user, kind=ui.card)


def tables(user: User) -> list[Any]:
    """Все таблицы страницы в порядке создания."""
    return elements(user, kind=ui.table)


def authenticate(user: User, token: str) -> None:
    """Выставить cookie сессии, как это сделал бы браузер после входа."""
    user.http_client.cookies.set("token", token)


# --------------------------------------------------------------------------- #
# взаимодействие
# --------------------------------------------------------------------------- #


def _fire(user: User, element: Any, event_type: str, args: dict | list) -> None:
    """Вызвать обработчики события ``event_type`` элемента (как UserInteraction.trigger)."""
    client = user.client
    assert client is not None, "user simulation client is not started"
    with client:
        for listener in element._event_listeners.values():
            if listener.type != event_type:
                continue
            events.handle_event(
                listener.handler,
                events.GenericEventArguments(sender=element, client=client, args=args),
            )


def click(user: User, element: Any) -> None:
    """Клик по элементу: запускает асинхронные обработчики как фоновые задачи."""
    _fire(user, element, "click", {})


def select_table_row(user: User, table: Any, row: dict) -> None:
    """Отметить строку таблицы (событие selection, как из браузера)."""
    _fire(user, table, "selection", {"added": True, "rows": [row], "keys": [row["id"]]})


def deselect_table_rows(user: User, table: Any, key: Any) -> None:
    """Снять выделение строк (событие selection с removed-режимом)."""
    _fire(user, table, "selection", {"added": False, "rows": [], "keys": [key]})


async def eventually(
    condition: Callable[[], bool], timeout: float = 2.0, message: str = ""
) -> None:
    """Дождаться условия (фоновые задачи NiceGUI завершаются асинхронно)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s: {message or condition}")


# --------------------------------------------------------------------------- #
# фабрики DTO
# --------------------------------------------------------------------------- #


def make_bond(**overrides: Any) -> BondDTO:
    """Облигация по умолчанию (ОФЗ 26207, годовой купон)."""
    defaults: dict[str, Any] = {
        "id": 1,
        "owner_id": 1,
        "isin": "RU000A0JX0J2",
        "name": "ОФЗ 26207",
        "nominal": 1000,
        "coupon_rate": 8.15,
        "coupon_frequency": "ANNUAL",
        "maturity_date": date(2027, 2, 4),
        "issuer": None,
    }
    return BondDTO(**{**defaults, **overrides})


def make_position(**overrides: Any) -> PositionDTO:
    """Позиция по умолчанию: 10 штук по 980.50."""
    defaults: dict[str, Any] = {
        "user_id": 1,
        "bond_id": 1,
        "quantity": 10,
        "avg_buy_price": 980.5,
        "total_invested": 9805.0,
    }
    return PositionDTO(**{**defaults, **overrides})


def make_transaction(**overrides: Any) -> TransactionDTO:
    """Сделка по умолчанию: покупка 10 штук 10.01.2026 на счёте №1."""
    defaults: dict[str, Any] = {
        "id": 1,
        "user_id": 1,
        "bond_id": 1,
        "broker_account_id": 1,
        "type": "BUY",
        "quantity": 10,
        "price": 980.5,
        "date": date(2026, 1, 10),
        "commission": 15.0,
        "created_at": datetime(2026, 1, 10, 12, 0),
    }
    return TransactionDTO(**{**defaults, **overrides})


def make_account(**overrides: Any) -> BrokerAccountDTO:
    """Брокерский счёт по умолчанию: счёт №1 пользователя №1 у «Test Broker»."""
    defaults: dict[str, Any] = {
        "id": 1,
        "user_id": 1,
        "broker_id": 1,
        "broker_name": "Test Broker",
        "name": "Основной",
        "account_number": "AB-001",
        "account_type": "STANDARD",
        "opened_at": None,
        "closed_at": None,
        "created_at": datetime(2026, 1, 1, 0, 0),
    }
    return BrokerAccountDTO(**{**defaults, **overrides})


def make_summary(**overrides: Any) -> PortfolioSummary:
    """Сводка портфеля с двумя позициями, купоном и денежными потоками."""
    position = PositionAnalytics(
        bond_id=1,
        isin="RU000A0JX0J2",
        name="ОФЗ 26207",
        quantity=10,
        avg_buy_price=980.5,
        total_invested=9805.0,
        ytm=0.0915,
        current_yield=0.083,
        accrued_coupon=12.34,
        next_coupon_date=date(2026, 6, 4),
        maturity_date=date(2027, 2, 4),
    )
    position_without_yield = PositionAnalytics(
        bond_id=2,
        isin="RU000A0JX0J4",
        name="ОФЗ 26212",
        quantity=5,
        avg_buy_price=700.0,
        total_invested=3500.0,
        ytm=None,
        current_yield=None,
        accrued_coupon=0.0,
        next_coupon_date=None,
        maturity_date=date(2028, 1, 19),
    )
    coupon = CouponDue(
        bond_id=1,
        isin="RU000A0JX0J2",
        name="ОФЗ 26207",
        date=date(2026, 6, 4),
        amount=407.5,
    )
    pnl = RealizedPnl(sells=100.0, maturities=50.0, commissions=5.0, total=145.0)
    cashflows = [
        Cashflow(
            date=date(2026, 6, 4), bond_id=1, isin="RU000A0JX0J2", kind="COUPON", amount=407.5
        ),
        Cashflow(
            date=date(2027, 2, 4), bond_id=1, isin="RU000A0JX0J2", kind="MATURITY", amount=10000.0
        ),
    ]
    defaults: dict[str, Any] = {
        "user_id": 1,
        "generated_at": datetime(2026, 2, 1, 10, 30),
        "positions": [position, position_without_yield],
        "total_invested": 13305.0,
        "next_coupons": [coupon],
        "realized_pnl": pnl,
        "upcoming_cashflows": cashflows,
    }
    return PortfolioSummary(**{**defaults, **overrides})
