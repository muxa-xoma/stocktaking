"""UI-тесты страниц входа и регистрации (``/login``, ``/register``)."""

from __future__ import annotations

from bond_accounting.auth.service import AuthError
from tests._ui_utils import eventually


async def test_login_page_renders(ui_user) -> None:
    """Страница входа содержит форму и ссылку на регистрацию."""
    await ui_user.open("/login")
    await ui_user.should_see("Вход")
    await ui_user.should_see("Логин")
    await ui_user.should_see("Пароль")
    await ui_user.should_see("Войти")
    await ui_user.should_see("Зарегистрироваться")


async def test_login_success_sets_cookie_and_redirects(
    ui_user, auth_service, valid_token, portfolio_service, bond_service
) -> None:
    """Вход с корректными данными вызывает сервис и открывает портфель."""
    auth_service.login.return_value = valid_token
    portfolio_service.get_all_positions.return_value = []
    bond_service.list_all.return_value = []

    await ui_user.open("/login")
    ui_user.find("Логин").type("alice")
    ui_user.find("Пароль").type("secret123")
    ui_user.find("Войти").click()

    await ui_user.should_see("Портфель")
    auth_service.login.assert_awaited_once_with("alice", "secret123")
    await eventually(lambda: ui_user.http_client.cookies.get("token") == valid_token)


async def test_login_empty_fields_shows_notification(ui_user, auth_service) -> None:
    """Пустая форма: предупреждение, сервис не вызывается."""
    await ui_user.open("/login")
    ui_user.find("Войти").click()

    await ui_user.should_see("Заполните логин и пароль")
    auth_service.login.assert_not_awaited()


async def test_login_wrong_credentials_shows_notification(ui_user, auth_service) -> None:
    """Неверные данные: сообщение из AuthError."""
    auth_service.login.side_effect = AuthError("Неверный логин или пароль")

    await ui_user.open("/login")
    ui_user.find("Логин").type("alice")
    ui_user.find("Пароль").type("wrong")
    ui_user.find("Войти").click()

    await ui_user.should_see("Неверный логин или пароль")


async def test_login_unexpected_error_shows_notification(ui_user, auth_service) -> None:
    """Сбой сервиса: общее сообщение об ошибке."""
    auth_service.login.side_effect = RuntimeError("db down")

    await ui_user.open("/login")
    ui_user.find("Логин").type("alice")
    ui_user.find("Пароль").type("secret123")
    ui_user.find("Войти").click()

    await ui_user.should_see("Ошибка: db down")


async def test_register_page_renders(ui_user) -> None:
    """Страница регистрации содержит форму и ссылку на вход."""
    await ui_user.open("/register")
    await ui_user.should_see("Регистрация")
    await ui_user.should_see("Логин")
    await ui_user.should_see("минимум 8 символов")
    await ui_user.should_see("Зарегистрироваться")
    await ui_user.should_see("Уже есть аккаунт? Войти")


async def test_register_success_creates_user_and_logs_in(
    ui_user, auth_service, valid_token, portfolio_service, bond_service
) -> None:
    """Регистрация: создание пользователя, вход, cookie, переход в портфель."""
    auth_service.login.return_value = valid_token
    portfolio_service.get_all_positions.return_value = []
    bond_service.list_all.return_value = []

    await ui_user.open("/register")
    ui_user.find("Логин").type("alice")
    ui_user.find("Пароль (минимум 8 символов)").type("secret123")
    ui_user.find("Пароль ещё раз").type("secret123")
    ui_user.find("Зарегистрироваться").click()

    await ui_user.should_see("Регистрация успешна")
    await ui_user.should_see("Портфель")
    auth_service.register.assert_awaited_once_with("alice", "secret123")
    auth_service.login.assert_awaited_once_with("alice", "secret123")
    await eventually(lambda: ui_user.http_client.cookies.get("token") == valid_token)


async def test_register_password_mismatch(ui_user, auth_service) -> None:
    """Разные пароли: предупреждение, сервис не вызывается."""
    await ui_user.open("/register")
    ui_user.find("Логин").type("alice")
    ui_user.find("Пароль (минимум 8 символов)").type("secret123")
    ui_user.find("Пароль ещё раз").type("otherpass1")
    ui_user.find("Зарегистрироваться").click()

    await ui_user.should_see("Пароли не совпадают")
    auth_service.register.assert_not_awaited()


async def test_register_empty_fields(ui_user, auth_service) -> None:
    """Пустая форма: предупреждение, сервис не вызывается."""
    await ui_user.open("/register")
    ui_user.find("Зарегистрироваться").click()

    await ui_user.should_see("Заполните логин и пароль")
    auth_service.register.assert_not_awaited()


async def test_register_auth_error(ui_user, auth_service) -> None:
    """Занятое имя пользователя: сообщение из AuthError."""
    auth_service.register.side_effect = AuthError("Пользователь уже существует")

    await ui_user.open("/register")
    ui_user.find("Логин").type("alice")
    ui_user.find("Пароль (минимум 8 символов)").type("secret123")
    ui_user.find("Пароль ещё раз").type("secret123")
    ui_user.find("Зарегистрироваться").click()

    await ui_user.should_see("Пользователь уже существует")
    auth_service.login.assert_not_awaited()


async def test_register_unexpected_error(ui_user, auth_service) -> None:
    """Сбой сервиса: общее сообщение об ошибке."""
    auth_service.register.side_effect = RuntimeError("db down")

    await ui_user.open("/register")
    ui_user.find("Логин").type("alice")
    ui_user.find("Пароль (минимум 8 символов)").type("secret123")
    ui_user.find("Пароль ещё раз").type("secret123")
    ui_user.find("Зарегистрироваться").click()

    await ui_user.should_see("Ошибка: db down")
