"""Страницы входа и регистрации (``/login``, ``/register``)."""

from __future__ import annotations

import logging

from nicegui import ui

from bond_accounting.auth.service import AuthError, AuthService
from bond_accounting.ui.common import notify_error, set_token_cookie

logger = logging.getLogger(__name__)


def register_auth_pages(auth_service: AuthService) -> None:
    """Зарегистрировать страницы ``/login`` и ``/register``.

    Args:
        auth_service: Сервис аутентификации (вход и регистрация).
    """

    @ui.page("/login")
    async def login_page() -> None:
        """Страница входа: логин, пароль, переход в портфель."""

        async def handle_login() -> None:
            """Проверить учётные данные, сохранить cookie и открыть портфель."""
            if not username.value or not password.value:
                ui.notify("Заполните логин и пароль", type="negative")
                return
            try:
                token = await auth_service.login(username.value, password.value)
            except AuthError as exc:
                ui.notify(str(exc), type="negative")
                return
            except Exception as exc:
                notify_error(exc)
                return
            set_token_cookie(token)
            ui.navigate.to("/")

        with ui.card().classes("mx-auto mt-16 w-80"):
            ui.label("Вход").classes("text-h5")
            username = ui.input("Логин").classes("w-full")
            password = ui.input("Пароль", password=True).classes("w-full")
            ui.button("Войти", on_click=handle_login).classes("w-full")
            ui.link("Нет аккаунта? Зарегистрироваться", "/register").classes("text-sm")

    @ui.page("/register")
    async def register_page() -> None:
        """Страница регистрации: логин, пароль и подтверждение пароля."""

        async def handle_register() -> None:
            """Создать пользователя, войти и сохранить cookie сессии."""
            if not username.value or not password.value:
                ui.notify("Заполните логин и пароль", type="negative")
                return
            if password.value != password_repeat.value:
                ui.notify("Пароли не совпадают", type="negative")
                return
            try:
                await auth_service.register(username.value, password.value)
                token = await auth_service.login(username.value, password.value)
            except AuthError as exc:
                ui.notify(str(exc), type="negative")
                return
            except Exception as exc:
                notify_error(exc)
                return
            logger.info("UI: зарегистрирован пользователь %r", username.value)
            ui.notify("Регистрация успешна", type="positive")
            set_token_cookie(token)
            ui.navigate.to("/")

        with ui.card().classes("mx-auto mt-16 w-80"):
            ui.label("Регистрация").classes("text-h5")
            username = ui.input("Логин").classes("w-full")
            password = ui.input("Пароль (минимум 8 символов)", password=True).classes("w-full")
            password_repeat = ui.input("Пароль ещё раз", password=True).classes("w-full")
            ui.button("Зарегистрироваться", on_click=handle_register).classes("w-full")
            ui.link("Уже есть аккаунт? Войти", "/login").classes("text-sm")
