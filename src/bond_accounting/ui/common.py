"""Общие помощники UI: авторизация по cookie, навигация, форматирование."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from nicegui import context, ui

from bond_accounting.auth.jwt_service import JwtError

if TYPE_CHECKING:
    from bond_accounting.auth.jwt_service import JwtService, TokenPayload
    from bond_accounting.brokers.service import BrokerService

logger = logging.getLogger(__name__)

#: Имя cookie, в которой браузер хранит JWT-токен сессии.
TOKEN_COOKIE = "token"  # nosec B105

#: Запасной срок жизни cookie, если из токена не удалось прочитать ``exp``;
#: совпадает с дефолтом ``auth.jwt_expires_minutes`` (1440 минут).
FALLBACK_MAX_AGE = 1440 * 60

#: Пункты навигации защищённых страниц: путь и заголовок.
_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("/", "Портфель"),
    ("/bonds", "Облигации"),
    ("/brokers", "Брокеры"),
    ("/accounts", "Счета"),
    ("/transactions", "Сделки"),
    ("/analytics", "Аналитика"),
)


def get_current_user(jwt_service: JwtService) -> TokenPayload | None:
    """Извлечь аутентифицированного пользователя из cookie запроса.

    Читает JWT из cookie ``token`` и проверяет его. При отсутствии или
    ошибке проверки браузер перенаправляется на страницу входа.

    Args:
        jwt_service: Сервис проверки JWT.

    Returns:
        Payload проверенного токена или ``None``, если пользователь не
        аутентифицирован (редирект на ``/login`` уже отправлен).
    """
    token = context.client.request.cookies.get(TOKEN_COOKIE)
    if not token:
        ui.navigate.to("/login")
        return None
    try:
        return jwt_service.verify_token(token)
    except JwtError as exc:
        logger.info("UI: доступ запрещён, токен невалиден (%s)", exc)
        ui.navigate.to("/login")
        return None


def token_max_age(token: str) -> int:
    """Остаток срока жизни JWT в секундах (значение для ``Max-Age``).

    Величина выводится из claim ``exp`` самого токена, поэтому cookie живёт
    ровно столько, сколько токен: TTL токена задаётся настройкой
    ``auth.jwt_expires_minutes``, и cookie его не переживает. Payload
    разбирается без проверки подписи — значение используется только как
    срок жизни cookie, сам токен проверяется при каждом запросе.

    Args:
        token: JWT-токен, только что выданный сервисом аутентификации.

    Returns:
        Секунды до ``exp`` (не меньше 0); :data:`FALLBACK_MAX_AGE`,
        если claims не удалось разобрать.
    """
    try:
        payload_b64 = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        remaining = int(claims["exp"]) - int(datetime.now(UTC).timestamp())
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "UI: не удалось прочитать exp из токена (%s); cookie получит запасной TTL", exc
        )
        return FALLBACK_MAX_AGE
    return max(0, remaining)


def _request_is_https() -> bool:
    """Открыта ли страница по HTTPS.

    Механизм (выбран вместо флага в конфигурации): схема берётся из
    ``context.client.request`` — HTTP-запроса, которым страница была
    открыта; NiceGUI хранит его на клиенте (он же читается для cookie в
    :func:`get_current_user`), поэтому схема доступна и внутри
    websocket-обработчиков. Схема отражает фактический транспорт: при
    терминировании TLS в самом uvicorn она ``https``, а за reverse-proxy с
    включёнными ``proxy-headers`` uvicorn уже переписывает её на ``https``.
    Если запроса у клиента нет (клиент без страницы), считаем HTTP.
    """
    try:
        return context.client.request.url.scheme == "https"
    except RuntimeError:
        return False


def set_token_cookie(token: str) -> None:
    """Сохранить JWT в cookie ``token`` на стороне браузера.

    NiceGUI не предоставляет серверного API для установки cookie из
    websocket-обработчика, поэтому cookie выставляется через
    ``document.cookie`` в браузере; сообщения обрабатываются по порядку,
    так что последующая навигация уже увидит cookie.

    Атрибуты: ``Max-Age`` равен остатку срока жизни токена (см.
    :func:`token_max_age`), ``Secure`` добавляется, когда страница открыта
    по HTTPS (см. :func:`_request_is_https`).

    Args:
        token: JWT-токен; живёт ровно столько, сколько его claim ``exp``.
    """
    secure = "; Secure" if _request_is_https() else ""
    cookie = f"{TOKEN_COOKIE}={token}; Path=/; Max-Age={token_max_age(token)}; SameSite=Lax{secure}"
    ui.run_javascript(f"document.cookie = {json.dumps(cookie)};")


def clear_token_cookie() -> None:
    """Удалить cookie ``token`` (выход из аккаунта).

    Атрибуты Path и Secure совпадают с :func:`set_token_cookie`, чтобы
    браузер удалил именно эту cookie при любой схеме (http и https).
    """
    secure = "; Secure" if _request_is_https() else ""
    cookie = f"{TOKEN_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax{secure}"
    ui.run_javascript(f"document.cookie = {json.dumps(cookie)};")


async def build_account_selector(
    broker_service: BrokerService,
    user_id: int,
    *,
    mandatory: bool = False,
) -> ui.select:
    """Построить выпадающий список брокерских счетов пользователя.

    Args:
        broker_service: Сервис для загрузки счетов пользователя.
        user_id: Пользователь, чьи счета показываются.
        mandatory: Когда ``False`` (по умолчанию), первым пунктом идёт
            «Все счета» (значение ``0``); когда ``True`` — показываются
            только реальные счета и первый предвыбирается.

    Returns:
        Заполненный ``ui.select``. Если у пользователя нет счетов и
        ``mandatory=True``, список пуст (вызывающий код отвечает за
        валидацию при добавлении сделки).
    """
    accounts = await broker_service.list_accounts_for_user(user_id)
    options: dict[int | str, str] = {}
    if not mandatory:
        options[0] = "Все счета"
    for acc in accounts:
        options[acc.id] = f"{acc.broker_name} / {acc.name}"
    select = ui.select(options, label="Брокер / Счёт")
    if options:
        select.value = next(iter(options))
    return select


def resolve_account_id(value: int | str | None) -> int | None:
    """Преобразовать значение селектора в ``broker_account_id``.

    ``0`` или ``None`` означает «все счета» → возвращается ``None``;
    любое другое значение возвращается как ``int``.
    """
    if value is None or value == 0:
        return None
    return int(value)


def notify_error(exc: BaseException) -> None:
    """Показать ошибку операции тостом, не раскрывая стектрейс в UI.

    Полная информация (включая трейсбек) пишется в серверный лог.

    Args:
        exc: Исключение, вызванное операцией; выводится пользователю
            в виде уведомления.
    """
    logger.exception("UI: операция завершилась ошибкой")
    ui.notify(f"Ошибка: {exc}", type="negative")


def page_header(active_path: str) -> None:
    """Отрисовать шапку с навигацией для защищённых страниц.

    Args:
        active_path: Путь текущей страницы; соответствующий пункт
            навигации выделяется.
    """
    with ui.header().classes("items-center gap-6"):
        ui.label("Bond Accounting").classes("text-h6")
        for path, title in _NAV_ITEMS:
            link = ui.link(title, path).classes("no-underline")
            if path == active_path:
                link.classes("text-bold")
        ui.space()
        ui.button("Выйти", on_click=_logout).props("flat")


def _logout() -> None:
    """Сбросить cookie сессии и вернуться на страницу входа."""
    clear_token_cookie()
    ui.navigate.to("/login")


def parse_date(value: str | None, field: str) -> date:
    """Разобрать значение формы ``YYYY-MM-DD`` в дату.

    Args:
        value: Строка из date-поля формы.
        field: Человекочитаемое имя поля для сообщения об ошибке.

    Returns:
        Разобранная дата.

    Raises:
        ValueError: Если значение пустое или не соответствует формату.
    """
    if not value:
        raise ValueError(f"{field}: укажите дату")
    return date.fromisoformat(value)


def fmt_money(value: float | None) -> str:
    """Отформатировать денежное значение с двумя знаками после запятой."""
    return "—" if value is None else f"{value:.2f}"


def fmt_percent(value: float | None) -> str:
    """Отформатировать долю (например, ``0.05``) как проценты."""
    return "—" if value is None else f"{value:.2%}"


def fmt_raw_percent(value: float | None) -> str:
    """Отформатировать процентное значение (например, ``5.0``) с двумя знаками после запятой."""
    return "—" if value is None else f"{value:.2f}%"


def fmt_date(value: date | None) -> str:
    """Отформатировать дату в виде ``ДД.ММ.ГГГГ``."""
    return "—" if value is None else value.strftime("%d.%m.%Y")


def fmt_datetime(value: datetime) -> str:
    """Отформатировать момент времени в виде ``ДД.ММ.ГГГГ ЧЧ:ММ``."""
    return value.strftime("%d.%m.%Y %H:%M")
