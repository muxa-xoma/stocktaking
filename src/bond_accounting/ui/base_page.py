"""Базовый класс защищённых страниц NiceGUI: JWT-проверка и регистрация."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from nicegui import ui

from bond_accounting.ui.common import get_current_user

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload

logger = logging.getLogger(__name__)


class BasePage(ABC):
    """Абстрактный базовый класс защищённых страниц NiceGUI.

    Наследники задают :attr:`path` и реализуют :meth:`render`;
    :meth:`register` навешивает декоратор ``ui.page`` и выполняет JWT-проверку,
    поэтому авторизационный код больше не повторяется на каждой странице.
    """

    path: str = ""

    def __init__(self, jwt_service: JwtService) -> None:
        self.jwt_service = jwt_service

    @abstractmethod
    async def render(self, user: TokenPayload) -> None:
        """Отрисовать содержимое страницы для аутентифицированного пользователя.

        Args:
            user: Проверенный payload JWT (``user_id``, ``username``).
        """
        ...

    def register(self) -> None:
        """Зарегистрировать страницу по пути :attr:`path`."""
        ui.page(self.path)(self._handle_page)

    async def _handle_page(self) -> None:
        user = get_current_user(self.jwt_service)
        if user is None:
            return
        await self.render(user)
