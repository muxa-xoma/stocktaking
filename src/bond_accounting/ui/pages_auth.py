"""Страницы входа и регистрации (``/login``, ``/register``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from nicegui import ui

from bond_accounting.auth.service import AuthError
from bond_accounting.ui.common import notify_error, set_token_cookie

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService
    from bond_accounting.auth.service import AuthService

logger = logging.getLogger(__name__)


@dataclass
class _LoginState:
    """Мутируемое состояние формы входа."""

    username: ui.input
    password: ui.input


@dataclass
class _RegisterState:
    """Мутируемое состояние формы регистрации."""

    username: ui.input
    password: ui.input
    password_repeat: ui.input


class LoginPage:
    """Страница входа (``/login``)."""

    path = "/login"

    def __init__(self, auth_service: AuthService, jwt_service: JwtService) -> None:
        self.auth_service = auth_service
        self.jwt_service = jwt_service

    def register(self) -> None:
        """Зарегистрировать страницу ``/login``."""
        ui.page(self.path)(self._handle_page)

    async def _handle_page(self) -> None:
        await self.render()

    async def _handle_login(self, state: _LoginState) -> None:
        """Проверить учётные данные, сохранить cookie и открыть портфель."""
        if not state.username.value or not state.password.value:
            ui.notify("Заполните логин и пароль", type="negative")
            return
        try:
            token = await self.auth_service.login(state.username.value, state.password.value)
        except AuthError as exc:
            ui.notify(str(exc), type="negative")
            return
        except Exception as exc:
            notify_error(exc)
            return
        set_token_cookie(token)
        ui.navigate.to("/")

    async def render(self) -> None:
        """Отрисовать форму входа: логин, пароль, переход в портфель."""
        with ui.card().classes("mx-auto mt-16 w-80"):
            ui.label("Вход").classes("text-h5")
            username = ui.input("Логин").classes("w-full")
            password = ui.input("Пароль", password=True).classes("w-full")
            login_button = ui.button("Войти").classes("w-full")
            ui.link("Нет аккаунта? Зарегистрироваться", "/register").classes("text-sm")

        state = _LoginState(username=username, password=password)
        login_button.on_click(partial(self._handle_login, state))


class RegisterPage:
    """Страница регистрации (``/register``)."""

    path = "/register"

    def __init__(self, auth_service: AuthService, jwt_service: JwtService) -> None:
        self.auth_service = auth_service
        self.jwt_service = jwt_service

    def register(self) -> None:
        """Зарегистрировать страницу ``/register``."""
        ui.page(self.path)(self._handle_page)

    async def _handle_page(self) -> None:
        await self.render()

    async def _handle_register(self, state: _RegisterState) -> None:
        """Создать пользователя, войти и сохранить cookie сессии."""
        if not state.username.value or not state.password.value:
            ui.notify("Заполните логин и пароль", type="negative")
            return
        if state.password.value != state.password_repeat.value:
            ui.notify("Пароли не совпадают", type="negative")
            return
        try:
            await self.auth_service.register(state.username.value, state.password.value)
            token = await self.auth_service.login(state.username.value, state.password.value)
        except AuthError as exc:
            ui.notify(str(exc), type="negative")
            return
        except Exception as exc:
            notify_error(exc)
            return
        logger.info("UI: зарегистрирован пользователь %r", state.username.value)
        ui.notify("Регистрация успешна", type="positive")
        set_token_cookie(token)
        ui.navigate.to("/")

    async def render(self) -> None:
        """Отрисовать форму регистрации: логин, пароль и подтверждение пароля."""
        with ui.card().classes("mx-auto mt-16 w-80"):
            ui.label("Регистрация").classes("text-h5")
            username = ui.input("Логин").classes("w-full")
            password = ui.input("Пароль (минимум 8 символов)", password=True).classes("w-full")
            password_repeat = ui.input("Пароль ещё раз", password=True).classes("w-full")
            register_button = ui.button("Зарегистрироваться").classes("w-full")
            ui.link("Уже есть аккаунт? Войти", "/login").classes("text-sm")

        state = _RegisterState(
            username=username,
            password=password,
            password_repeat=password_repeat,
        )
        register_button.on_click(partial(self._handle_register, state))
