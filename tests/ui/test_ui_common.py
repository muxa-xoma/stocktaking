"""Тесты общих помощников UI: чистые функции, auth-guard, навигация, уведомления."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from nicegui import ui

from bond_accounting.ui.common import (
    FALLBACK_MAX_AGE,
    TOKEN_COOKIE,
    fmt_date,
    fmt_datetime,
    fmt_money,
    fmt_percent,
    notify_error,
    parse_date,
    token_max_age,
)
from tests.ui._ui_utils import (
    authenticate,
    click,
    one,
)

# --------------------------------------------------------------------------- #
# чистые функции
# --------------------------------------------------------------------------- #


def test_token_cookie_max_age(jwt_service) -> None:
    """Имя cookie; Max-Age равен сроку жизни выданного JWT."""
    assert TOKEN_COOKIE == "token"

    token = jwt_service.create_token(user_id=1, username="alice", expires_minutes=30)
    assert 29 * 60 <= token_max_age(token) <= 30 * 60

    # Неразборчивый payload — запасной TTL (дефолт jwt_expires_minutes).
    assert token_max_age("not-a-jwt") == FALLBACK_MAX_AGE


def test_parse_date_valid() -> None:
    assert parse_date("2026-01-15", "Дата сделки") == date(2026, 1, 15)


@pytest.mark.parametrize("value", [None, ""])
def test_parse_date_empty_raises(value: str | None) -> None:
    with pytest.raises(ValueError, match="Дата сделки: укажите дату"):
        parse_date(value, "Дата сделки")


def test_parse_date_invalid_format_raises() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        parse_date("15.01.2026", "Дата сделки")


def test_fmt_money() -> None:
    assert fmt_money(None) == "—"
    assert fmt_money(980.5) == "980.50"
    assert fmt_money(0) == "0.00"


def test_fmt_percent() -> None:
    assert fmt_percent(None) == "—"
    assert fmt_percent(0.083) == "8.30%"
    assert fmt_percent(1) == "100.00%"


def test_fmt_date() -> None:
    assert fmt_date(None) == "—"
    assert fmt_date(date(2027, 2, 4)) == "04.02.2027"


def test_fmt_datetime() -> None:
    assert fmt_datetime(datetime(2026, 2, 1, 10, 30)) == "01.02.2026 10:30"


# --------------------------------------------------------------------------- #
# auth guard и навигация (через user simulation)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/", "/bonds", "/transactions", "/analytics"])
async def test_protected_pages_redirect_unauthenticated(ui_user, path: str) -> None:
    """Без cookie сессии защищённые страницы отправляют на /login."""
    await ui_user.open(path)
    await ui_user.should_see("Вход", retries=20)
    assert ui_user.back_history[-1] == "/login"


async def test_protected_page_redirects_invalid_token(ui_user) -> None:
    """Невалидный JWT в cookie — тоже редирект на /login."""
    ui_user.http_client.cookies.set(TOKEN_COOKIE, "not-a-jwt")
    await ui_user.open("/bonds")
    await ui_user.should_see("Вход", retries=20)


async def test_navbar_rendered_for_authenticated_user(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Шапка с навигацией и кнопкой выхода видна авторизованному."""
    portfolio_service.get_all_positions.return_value = []
    bond_service.list_all.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    await ui_user.should_see("Bond Accounting")
    await ui_user.should_see("Облигации")
    await ui_user.should_see("Сделки")
    await ui_user.should_see("Аналитика")
    await ui_user.should_see("Выйти")


async def test_logout_clears_cookie_and_returns_to_login(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Выход: cookie сбрасывается, пользователь попадает на /login."""
    portfolio_service.get_all_positions.return_value = []
    bond_service.list_all.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    logout_button = one(ui_user, kind=ui.button, content="Выйти")
    click(ui_user, logout_button)

    await ui_user.should_see("Вход", retries=20)
    assert ui_user.http_client.cookies.get(TOKEN_COOKIE) is None

    # После выхода защищённая страница снова редиректит на вход.
    await ui_user.open("/")
    await ui_user.should_see("Вход", retries=20)


async def test_notify_error_shows_negative_notification(ui_user) -> None:
    """notify_error показывает тост с текстом исключения."""
    await ui_user.open("/login")
    notify_error(ValueError("boom"))
    assert ui_user.notify.contains("Ошибка: boom")


async def test_navbar_rendered_on_bonds_and_transactions_pages(
    ui_user, valid_token, bond_service, portfolio_service
) -> None:
    """Пункты навигации и «Выйти» видны на каждой защищённой странице."""
    bond_service.list_all.return_value = []
    portfolio_service.list_transactions.return_value = []
    authenticate(ui_user, valid_token)

    for path in ["/bonds", "/transactions"]:
        await ui_user.open(path)
        await ui_user.should_see("Bond Accounting")
        await ui_user.should_see("Выйти")
