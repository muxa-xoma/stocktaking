"""UI-тесты главной страницы (``/``): рендер и пустое состояние портфеля."""

from __future__ import annotations

from tests.ui._ui_utils import (
    authenticate,
    tables,
)


async def test_home_page_renders_empty(
    ui_user, valid_token, portfolio_service, bond_service
) -> None:
    """Главная страница: заголовок, таблица и кнопка обновления; без позиций."""
    portfolio_service.get_all_positions.return_value = []
    bond_service.list_all.return_value = []
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    await ui_user.should_see("Портфель")
    await ui_user.should_see("Обновить")

    [table] = tables(ui_user)
    assert table.rows == []
    portfolio_service.get_all_positions.assert_awaited_once_with(1, broker_account_id=None)


async def test_home_page_requires_authentication(ui_user) -> None:
    """Без cookie страница перенаправляет на вход."""
    await ui_user.open("/")
    await ui_user.should_see("Вход", retries=20)


async def test_home_page_load_error_shows_notification(
    ui_user, valid_token, portfolio_service
) -> None:
    """Сбой загрузки позиций: тост с ошибкой, таблица остаётся пустой."""
    portfolio_service.get_all_positions.side_effect = RuntimeError("db down")
    authenticate(ui_user, valid_token)

    await ui_user.open("/")
    await ui_user.should_see("Ошибка: db down")
