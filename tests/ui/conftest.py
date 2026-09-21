"""Fixtures for UI tests: NiceGUI user simulation with mocked services.

General fixtures (``_clean_bond_env``, ``auth_config``, ``jwt_service``) live
in ``tests/conftest.py`` and are inherited here.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
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
from bond_accounting.auth.service import AuthService
from bond_accounting.bonds.service import BondService
from bond_accounting.brokers.service import BrokerService
from bond_accounting.portfolio.service import PortfolioService
from bond_accounting.ui import create_ui_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from bond_accounting.auth.jwt_service import JwtService


# --------------------------------------------------------------------------- #
# NiceGUI user-simulation fixtures (see https://nicegui.io/documentation/user_simulation)
# --------------------------------------------------------------------------- #

#: Шаблоны JS-команд установки/сброса cookie из ``ui/common.py``.
_SET_COOKIE_JS = re.compile(r'document\.cookie = "token=([^;"]+)')
_CLEAR_COOKIE_JS = re.compile(r'document\.cookie = "token=;')


@contextlib.contextmanager
def _safe_nicegui_reset_globals():
    """Сбросить глобальное состояние NiceGUI, не выкидывая ``bond_accounting.*``.

    WHY this wrapper exists: pytest собирает ``tests/ui`` раньше ``tests/unit``
    (алфавитный порядок), и все тесты должны запускаться одной командой
    (``uv run pytest -q``). Однако ``nicegui_reset_globals`` на выходе удаляет
    из ``sys.modules`` модули всех функций из ``Client.page_routes``, чей
    ``__module__`` не начинается с ``tests.`` — то есть ``bond_accounting.*``
    (страницы регистрируются через ``create_ui_app``). Без восстановления эти
    модули пропадают из ``sys.modules`` до конца pytest-сессии, и позже
    запускаемые unit-тесты падают с ``ModuleNotFoundError``.

    Снимок объектов модулей делается ДО входа в сброс, а после выхода
    восстанавливается через ``sys.modules.update``. Модули возвращаются
    по ссылке (те же объекты), поэтому их код не исполняется повторно и
    mid-context импортов с побочными эффектами не возникает: выкидывание
    происходит в ``finally`` самой ``nicegui_reset_globals``, и наш
    ``finally`` сразу возвращает всё на место. Обёртка идемпотентна —
    повторный вызов просто делает новый снимок и снова восстанавливает его.

    (Это НЕ старый обходной путь с ``saved_bond_modules`` из до-task-20
    времён: тот глобально снапшотил/восстанавливал состояние вокруг каждого
    теста, здесь же сохраняются только ссылки на модули.)
    """
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "bond_accounting" or name.startswith("bond_accounting.")
    }
    try:
        with nicegui_reset_globals():
            yield
    finally:
        sys.modules.update(saved)


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
def broker_service() -> BrokerService:
    """Мок CRUD-сервиса брокеров и брокерских счетов."""
    return create_autospec(BrokerService, instance=True)


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

        self._client.outbox._emit = simulated_emit  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]


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

    Client.run_javascript = run_javascript_with_rules  # type: ignore[method-assign]  # monkeypatch a bound class method for the test simulation

    def restore_run_javascript() -> None:
        Client.run_javascript = original  # type: ignore[method-assign]  # restore the original method

    return restore_run_javascript


@pytest.fixture
async def ui_user(
    jwt_service: JwtService,
    auth_service: AuthService,
    bond_service: BondService,
    broker_service: BrokerService,
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
    with _safe_nicegui_reset_globals():
        os.environ["NICEGUI_USER_SIMULATION"] = "true"
        try:
            prepare_simulation()
            ui.run(storage_secret="simulated secret")
            create_ui_app(
                auth_service=auth_service,
                jwt_service=jwt_service,
                bond_service=bond_service,
                broker_service=broker_service,
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
