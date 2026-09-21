"""Shared pytest fixtures."""

from __future__ import annotations

import os
import re
import uuid
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import httpx
import pytest
from nicegui import Client, core, ui
from nicegui.functions.download import download
from nicegui.functions.navigate import Navigate
from nicegui.functions.notify import notify
from nicegui.testing import User
from nicegui.testing.general import nicegui_reset_globals, prepare_simulation

from bond_accounting.analytics.service import AnalyticsService
from bond_accounting.auth.jwt_service import JwtService
from bond_accounting.auth.service import AuthService
from bond_accounting.bonds.service import BondService
from bond_accounting.config.settings import ENV_PREFIX, AuthConfig
from bond_accounting.portfolio.service import PortfolioService
from bond_accounting.ui import create_ui_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable


@pytest.fixture(autouse=True)
def _clean_bond_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient ``BOND_*`` env vars so tests do not depend on the host.

    Individual tests re-set the variables they need via ``monkeypatch``.
    """
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


# --------------------------------------------------------------------------- #
# NiceGUI user-simulation fixtures (see https://nicegui.io/documentation/user_simulation)
# --------------------------------------------------------------------------- #

#: JWT-секрет, используемый только UI-тестами.
UI_JWT_SECRET = "test-secret"

#: Шаблоны JS-команд установки/сброса cookie из ``ui/common.py``.
_SET_COOKIE_JS = re.compile(r'document\.cookie = "token=([^;"]+)')
_CLEAR_COOKIE_JS = re.compile(r'document\.cookie = "token=;')


@pytest.fixture
def auth_config() -> AuthConfig:
    """Auth-конфиг с тестовым секретом (без env-переменных)."""
    return AuthConfig(jwt_secret=UI_JWT_SECRET)


@pytest.fixture
def jwt_service(auth_config: AuthConfig) -> JwtService:
    """Реальный JwtService: страницы проверяют подпись настоящего токена."""
    return JwtService(auth_config)


@pytest.fixture
def valid_token(jwt_service: JwtService) -> str:
    """Валидный JWT пользователя alice (user_id=1)."""
    return jwt_service.create_token(user_id=1, username="alice")


@pytest.fixture
def auth_service() -> AuthService:
    """Мок сервиса аутентификации (register/login — AsyncMock)."""
    return create_autospec(AuthService, instance=True)


@pytest.fixture
def bond_service() -> BondService:
    """Мок CRUD-сервиса облигаций."""
    return create_autospec(BondService, instance=True)


@pytest.fixture
def portfolio_service() -> PortfolioService:
    """Мок сервиса сделок и позиций."""
    return create_autospec(PortfolioService, instance=True)


@pytest.fixture
def analytics_service() -> AnalyticsService:
    """Мок сервиса аналитики."""
    return create_autospec(AnalyticsService, instance=True)


class SimUser(User):
    """``User`` с фиксой для fire-and-forget ``run_javascript``.

    В production-коде cookie выставляются через ``ui.run_javascript(...)`` без
    await; такие сообщения не содержат ``request_id``, и стандартный
    ``simulated_emit`` из nicegui падает с ``KeyError`` *до* применения
    ``javascript_rules`` (порядок вычисления аргументов словаря). Здесь
    отсутствующий ``request_id`` заменяется случайным — ``JavaScriptRequest.resolve``
    просто игнорирует неизвестный id.
    """

    def _patch_outbox_emit_function(self) -> None:
        original_emit = self._client.outbox._emit

        async def simulated_emit(message: tuple) -> None:
            await original_emit(message)
            _, type_, data = message
            if type_ == "run_javascript":
                for rule, result in self.javascript_rules.items():
                    match = rule.match(data["code"])
                    if match:
                        self._client.handle_javascript_response(
                            {
                                "request_id": data.get("request_id", str(uuid.uuid4())),
                                "result": result(match),
                            }
                        )

        self._client.outbox._emit = simulated_emit  # type: ignore[method-assign]


def _install_cookie_rules(user: SimUser) -> None:
    """Перехватить ``document.cookie`` из симуляции в cookies HTTP-клиента.

    ``set_token_cookie``/``clear_token_cookie`` выставляют cookie через
    ``ui.run_javascript``; в user simulation эти команды перехватываются
    через ``user.javascript_rules`` и применяются к httpx-клиенту, чтобы
    последующая навигация видела cookie как настоящий браузер.
    """

    def capture_set(match: re.Match) -> bool:
        user.http_client.cookies.set("token", match.group(1))
        return True

    def capture_clear(match: re.Match) -> bool:
        user.http_client.cookies.delete("token")
        return True

    user.javascript_rules[_SET_COOKIE_JS] = capture_set
    user.javascript_rules[_CLEAR_COOKIE_JS] = capture_clear


def _patch_run_javascript(user: SimUser) -> Callable[[], None]:
    """Применять ``javascript_rules`` синхронно в ``Client.run_javascript``.

    В симуляции ``ui.navigate.to`` открывает страницу через HTTP-запрос из
    отдельной фоновой задачи, которая может опередить цикл outbox. В реальном
    браузере сообщения websocket обрабатываются по порядку, поэтому cookie
    всегда виден навигации. Здесь это поведение воспроизводится: правила
    (установка/сброс cookie) применяются сразу в момент вызова.
    """
    original = Client.run_javascript

    def run_javascript_with_rules(self: Client, code: str, *, timeout: float = 1.0):
        response = original(self, code, timeout=timeout)
        for rule, result in user.javascript_rules.items():
            match = rule.match(code)
            if match:
                result(match)
        return response

    Client.run_javascript = run_javascript_with_rules  # type: ignore[method-assign]

    def restore_run_javascript() -> None:
        Client.run_javascript = original  # type: ignore[method-assign]  # restore the original method

    return restore_run_javascript


@pytest.fixture
async def ui_user(
    jwt_service: JwtService,
    auth_service: AuthService,
    bond_service: BondService,
    portfolio_service: PortfolioService,
    analytics_service: AnalyticsService,
) -> AsyncGenerator[SimUser]:
    """Пользователь NiceGUI user simulation: реальный UI + моки сервисов.

    Точно повторяет ``nicegui.testing.user_simulation`` (сброс глобального
    состояния, ``prepare_simulation``, lifespan), но вместо main-файла
    регистрирует страницы через ``create_ui_app`` с мок-сервисами.
    """
    user: SimUser | None = None
    restore_run_javascript: Callable[[], None] | None = None
    with nicegui_reset_globals():
        os.environ["NICEGUI_USER_SIMULATION"] = "true"
        try:
            prepare_simulation()
            ui.run(storage_secret="simulated secret")
            create_ui_app(
                auth_service=auth_service,
                jwt_service=jwt_service,
                bond_service=bond_service,
                portfolio_service=portfolio_service,
                analytics_service=analytics_service,
            )
            async with (
                core.app.router.lifespan_context(core.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=core.app), base_url="http://test"
                ) as client,
            ):
                user = SimUser(client)
                _install_cookie_rules(user)
                restore_run_javascript = _patch_run_javascript(user)
                yield user
        finally:
            if restore_run_javascript is not None:
                restore_run_javascript()
            os.environ.pop("NICEGUI_USER_SIMULATION", None)
            ui.navigate = Navigate()
            ui.notify = notify
            ui.download = download
