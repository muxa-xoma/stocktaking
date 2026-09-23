# bond-accounting

**Доступно на:** [Русский](README_ru.md) | [English](README.md)

Сервис учёта облигационного портфеля (Python 3.14+).

## Что это

Сервис для ведения облигационного портфеля: инструменты (облигации) и операции
с ними (покупка / продажа / погашение), учёт портфеля и позиций, расчёт
доходностей (YTM, текущая доходность, НКД, график купонов), аналитика,
JWT-аутентификация, а также веб-интерфейс (NiceGUI) и REST API (FastAPI),
обслуживаемые на одном порту.

## Требования

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)

## Установка

```bash
uv sync
```

## Запуск

```bash
uv run bond-accounting
```

Полезное о запуске:

- Конфигурация читается из `config.yaml` в рабочей директории; путь к ней
  можно переопределить флагом `--config <путь>`. Переменные окружения
  `BOND_*` всегда имеют приоритет над файлом.
- При старте миграции применяются автоматически (`alembic upgrade head`
  выполняется in-process) — отдельно запускать их не нужно.
- UI и REST API обслуживаются на одном порту (по умолчанию
  `127.0.0.1:8080`); OpenAPI-документация доступна по адресам `/docs` и
  `/redoc`.
- Остановка по SIGINT/SIGTERM — корректная (graceful shutdown).

## Конфигурация

Конфигурация собирается
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
из нескольких источников. Приоритет, от высшего к низшему:

1. аргументы конструктора (`Settings(auth={...})`),
2. переменные окружения с префиксом `BOND_` (разделитель уровней вложенности —
   `__`),
3. YAML-файл (`config.yaml` в рабочей директории по умолчанию либо путь,
   переданный в `load_settings(config_path)`),
4. значения по умолчанию из моделей.

Полная структура показана в примере `config.yaml` в корне репозитория:

```yaml
app:
  host: "127.0.0.1"
  port: 8080
  debug: false

database:
  driver: "sqlite"                # "sqlite" | "postgresql"
  sqlite_path: "data/bond_accounting.db"
  # connect_args override the default SQLite pragmas (user wins):
  # connect_args:
  #   journal_mode: "delete"
  # PostgreSQL:
  # driver: "postgresql"
  # host: "localhost"
  # port: 5432
  # user: "bond"
  # password: "secret"
  # dbname: "bond_accounting"
  # connect_args:
  #   sslmode: "disable"

auth:
  jwt_secret: "change-me-in-production"
  jwt_algorithm: "HS256"
  jwt_expires_minutes: 1440

logging:
  level: "INFO"
  format: "json"   # "json" | "text"

event_bus:
  max_queue_size: 10000

market_data:
  enabled: true                                  # false отключает интеграцию с MOEX
  base_url: "https://iss.moex.com"               # базовый URL MOEX ISS API
  timeout_s: 5.0                                 # таймаут HTTP-запроса
  cache_ttl_s: 300                               # TTL кэша поиска в памяти
```

Подключение к базе данных задаётся структурно; async SQLAlchemy URL строится
из этих полей (специальные символы в пароле URL-кодируются автоматически):

- SQLite: `sqlite+aiosqlite:///data/bond_accounting.db` (только async-драйвер)
- PostgreSQL: `postgresql+psycopg://user:password@host:port/dbname?param=value`
  (psycopg 3 работает с `create_async_engine`)

Для SQLite на каждое соединение применяется набор прагм по умолчанию
(`auto_vacuum=1`, `journal_mode=wal`, `synchronous=1`, `temp_store=2`,
`cache_size=-64000`, `foreign_keys=1`). Значения из `database.connect_args`
переопределяют эти умолчания — пользовательский выбор побеждает: например,
`connect_args: {journal_mode: delete}` заменит `wal`, остальные прагмы
останутся. Для PostgreSQL `connect_args` попадают в query-строку URL
(например, `sslmode`).

Движок создаётся функцией `create_engine_from_settings(db_config)` из
`bond_accounting.db.engine` (см. [База данных](#база-данных)). Для SQLite
прагмы применяются на каждом новом соединении слушателем события `connect`,
который выполняет инструкции `PRAGMA key = value`, — а не через
`connect_args` движка: диалект aiosqlite передаёт `connect_args` без изменений
в `sqlite3.connect`, который не принимает ключи прагм.

Логирование настраивается функцией `setup_logging(settings.logging)` из
`bond_accounting.config.logging_setup`: `format: "json"` выдаёт
машиночитаемые JSON-логи (через msgspec-бэкенд `python-json-logger`) со всеми
стандартными полями и любыми `extra=`-аргументами; `format: "text"` —
читаемые строки вида
`2026-09-20 12:00:00 | INFO | bond_accounting.bonds | message | key=value`.
Известные сторонние логгеры (uvicorn server + access, nicegui, sqlalchemy,
alembic, fastapi) принудительно сбрасываются, чтобы их записи шли через единый
root-handler в заданном формате и уровне (включая JSON access-логи с
`client_addr` / `request_line` / `status_code`); предупреждение PyJWT
`InsecureKeyLengthWarning` перехватывается в логгер
`bond_accounting.pyjwt_warnings`, все остальные warnings сохраняют
поведение по умолчанию.

Переменные окружения используют префикс `BOND_` и разделитель `__` для уровней
вложенности:

```bash
BOND_AUTH__JWT_SECRET="super-secret"
BOND_APP__PORT="9000"
BOND_DATABASE__DRIVER="postgresql"
BOND_DATABASE__HOST="db.example.com"
BOND_DATABASE__SQLITE_PATH="/var/lib/bonds.db"
BOND_EVENT_BUS__MAX_QUEUE_SIZE="5000"
BOND_LOGGING__LEVEL="DEBUG"
BOND_LOGGING__FORMAT="text"
BOND_MARKET_DATA__ENABLED="false"
```

> Примечание: старые «плоские» имена `BOND_DATABASE_URL` и
> `BOND_AUTH_JWT_SECRET` больше не поддерживаются — используйте вложенный
> синтаксис из примера выше.

`auth.jwt_secret` обязателен: задайте его в файле конфигурации или экспортируйте
`BOND_AUTH__JWT_SECRET`. `load_settings()` возбуждает `ConfigError`, если
конфигурация невалидна или YAML-файл повреждён.

## Справочный поиск облигаций (MOEX)

Модуль `market_data` (`bond_accounting.market_data`) интегрируется со
справочным API [MOEX ISS](https://www.moex.com/) через `httpx`
(runtime-зависимость), чтобы облигации можно было находить по ISIN или
названию вместо ручного ввода:

- `GET /api/bonds/reference/search?q=<запрос>&limit=10` — поиск
  справочных данных облигаций по ISIN или названию (`q` обязателен,
  1–100 символов; `limit` по умолчанию 10, максимум 50); возвращает
  `200 {"results": [...]}` (возможно, пустой), `422` при невалидном запросе
  или `503`, если провайдер недоступен или интеграция отключена.
- `GET /api/bonds/{isin}/coupons` — чтение сохранённого графика купонов
  (`bond_coupons`) облигации, упорядоченного по дате; возвращает `200`
  `[{"coupon_date": "<ISO date>", "coupon_amount": <float>}]` (пустой список —
  валидный результат), `404`, если ISIN не найден, или `422` при невалидном
  ISIN. Читает только базу — без обращения к провайдеру.
- `POST /api/bonds/{isin}/sync-coupons` — пересоздание графика купонов
  облигации из сервиса MOEX bondization в фоновой задаче; возвращает
  `202 {"task_id": "<uuid4().hex>"}` (статус задачи не сохраняется), `404`
  для неизвестной облигации, `422` при невалидном ISIN или `503`, если
  интеграция отключена.

Все эндпоинты защищены JWT, как и все остальные. При отключённой
интеграции (`market_data.enabled: false`) REST-эндпоинты возвращают `503`,
а UI показывает статическую подсказку; при недоступности MOEX UI показывает
ненавязчивую подсказку — ручной ввод облигаций работает в обоих случаях.

## База данных

Слой работы с БД живёт в `bond_accounting.db` (`src/bond_accounting/db/`):

- `Base`, `User`, `Bond`, `BondCoupon`, `Transaction`, `Broker`,
  `BrokerAccount`, `AccountOperation` — модели SQLAlchemy 2.0
  (`Mapped`/`mapped_column`) с CHECK-ограничениями на `transactions.type`
  (`BUY`/`SELL`/`MATURE`), `account_operations.type`
  (`DEPOSIT`/`WITHDRAWAL`/`TAX`) и `bonds.coupon_period_days` (Integer,
  NOT NULL, по умолчанию 182, `0` — бескупонная облигация; календарные дни
  между выплатами купона).
  `Broker.commission` хранится в **процентах** (5.0 = 5%);
  `Broker.min_commission` интерпретируется в зависимости от
  `Broker.min_commission_type` (`PERCENT` или `RUBLES`);
  `BrokerAccount.broker_id` — обязательное поле (NOT NULL).
- `BondCoupon` — одна запись на фактическую купонную выплату облигации
  в таблице `bond_coupons` (`bond_id` FK → `bonds.id` ON DELETE CASCADE,
  индексируется; `coupon_date`, `coupon_amount`,
  `UNIQUE(bond_id, coupon_date)`). Таблица опциональна: пока пуста, расчёт
  доходностей выводит график купонов из `Bond.coupon_period_days`; после
  заполнения (например, из MOEX `bondization`) фактические даты и суммы
  имеют приоритет.
- `create_engine_from_settings(db_config)` — фабрика async-движка
  (`create_async_engine`); прагмы SQLite применяются через слушатель события
  `connect` (см. выше).
- `create_session_factory(engine)` — фабрика `AsyncSession` с
  `expire_on_commit=False`.

Позиция по облигации нигде не хранится: она всегда вычисляется как
`sum(BUY.quantity) - sum(SELL.quantity) - sum(MATURE.quantity)`.

Приложение никогда не создаёт таблицы само (`Base.metadata.create_all` не
используется): схема управляется исключительно миграциями
[Alembic](https://alembic.sqlalchemy.org/) в `alembic/versions/`. URL миграций
**не** хранится в `alembic.ini` — `alembic/env.py` строит async-движок из
конфигурации приложения (`config.yaml` или переменных `BOND_*`), поэтому
миграции применяются ровно к той базе, с которой работает приложение:

```bash
uv run alembic revision --autogenerate -m "<description>"  # создать миграцию
uv run alembic upgrade head                              # применить все миграции
uv run alembic downgrade -1                              # откатить одну ревизию
uv run alembic downgrade base                            # откатить всё
```

Вся схема живёт в единственной «схлопнутой» миграции
(`alembic/versions/0001_initial_schema.py`): один `upgrade()` создаёт полную
целевую схему, `downgrade()` удаляет все таблицы. Локальные базы, созданные
старой цепочкой миграций, нужно пересоздать (приложение ещё не в продакшене;
схлопывание — осознанное решение).

Пример — применить миграции к конкретному SQLite-файлу, не меняя конфиг
(запускать из корня проекта, чтобы `config.yaml` предоставил `auth.jwt_secret`,
либо экспортировать `BOND_AUTH__JWT_SECRET`):

```bash
BOND_DATABASE__SQLITE_PATH=/tmp/new.db uv run alembic upgrade head
```

## Структура проекта

```
src/bond_accounting/
  main.py         # точка входа: композиция, запуск, graceful shutdown
  config/         # настройки: Pydantic + YAML + env, логирование
  db/             # модели SQLAlchemy 2.0, engine/фабрика сессий
  event_bus/      # внутренняя событийная шина (pub/sub, request/response)
  auth/           # JWT-аутентификация, хеширование паролей (bcrypt)
  bonds/          # облигации: CRUD-сервис с публикацией в event bus
  brokers/        # брокеры и брокерские счета: CRUD-сервис
  market_data/    # справочный поиск облигаций в MOEX ISS (httpx, TTL-кэш)
  account_operations/ # операции по счёту: CRUD (DEPOSIT/WITHDRAWAL/TAX)
  portfolio/      # портфель: сделки, позиции, неттинг
  yield_calc/     # доходности: YTM, текущая, НКД, график купонов
  analytics/      # метрики портфеля и отчёты (подписана на event bus)
  ui/             # веб-интерфейс на NiceGUI (drawer-навигация, тёмная тема,
                  #  модальные CRUD-формы; страницы: логин, облигации, сделки,
                  #  портфель, аналитика, брокеры, счета, операции)
  api/            # публичный REST API (FastAPI)
  external_bus/   # интеграция с внешней шиной событий (зарезервировано)
alembic/          # миграции Alembic (async env.py)
alembic.ini       # конфигурация Alembic (без URL базы данных)
tests/            # тесты (слои: unit, integration, api, functional, ui)
config.yaml       # пример конфигурации
data/             # файлы SQLite (runtime)
```

Проект использует стандартный `src/`-layout для uv: код приложения лежит в
`src/bond_accounting/`.

## Разработка

```bash
uv sync --all-extras --group dev                # runtime- и dev-зависимости
uv run pytest -q                                # все тесты, одной командой
uv run ruff check .                             # линтер
uv run mypy src                                 # проверка типов
```

Тесты запускаются одной командой `uv run pytest -q` — все слои сразу:
unit, integration, api, functional и ui. Структура тестов слоистая:

- `tests/unit/` — юнит-тесты (изолированные компоненты),
- `tests/integration/` — интеграционные тесты (компоненты вместе, БД),
- `tests/api/` — тесты REST API,
- `tests/functional/` — функциональные тесты (сценарии целиком),
- `tests/ui/` — тесты веб-интерфейса (симуляция пользователя NiceGUI),
- `tests/conftest.py` — общий стек фикстур для всех слоёв,
- `tests/rest_utils.py` — общие константы и хелперы REST-тестов
  (используются слоями `api` и `functional`).

## Покрытие тестами

```bash
uv run pytest -q --cov=bond_accounting --cov-report=term-missing
```

Команда запускает все тесты с замером покрытия (`pytest-cov` входит в
dev-зависимости `pyproject.toml`). Добавьте `--cov-report=html`, чтобы
получить открываемый в браузере HTML-отчёт в `htmlcov/`. Текущее покрытие
по строкам — около 91% (557 тестов, 90.60% по строкам).
(точка входа `main.py` не покрывается тестами: она проверяется фактическим
запуском приложения; без неё покрытие, соответственно, выше).

## Качество кода

В проекте используются четыре линтера/тайпчекера, все запускаются через `uv`.
Все четыре — обязательные гейты: CI должен проходить каждый из них.

| Инструмент | Назначение | Команда |
| --- | --- | --- |
| [Ruff](https://docs.astral.sh/ruff/) | Линтер + форматтер | `uv run ruff check .` |
| | | `uv run ruff format --check .` |
| [mypy](https://mypy-lang.org/) | Тайпчекер (гейт) | `uv run mypy src` |
| [ty](https://docs.astral.sh/ty/) | Тайпчекер (Astral, гейт) | `uv run ty check src tests` |
| [Pyrefly](https://pyrefly.org/) | Тайпчекер (Meta, гейт) | `uv run pyrefly check --min-severity warn` |

> **Примечание о счётчике «suppressed» в pyrefly:** в сводке строка
> `N suppressed` не равна числу реально скрытых диагностик — директивы
> подавления могут давать «фантомные» единицы. Чтобы увидеть, что реально
> подавлено, выполните
> `uv run pyrefly check --enabled-ignores pyre --min-severity ignore --output-format json`.

Конфигурация живёт в `pyproject.toml`: `[tool.ruff]`, `[tool.mypy]`,
`[tool.ty]` и `[tool.pyrefly]`. `ty` проверяет `src/` и `tests/`
(`[tool.ty.src] include`); `pyrefly` работает в режиме проекта и подхватывает
`[tool.pyrefly]` (включает `src/**` и `tests/**`).

> Примечание: по умолчанию `pyrefly check` скрывает предупреждения — чтобы их
> увидеть, используйте `--min-severity warn`.

Запустить всё сразу:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run ty check src tests
uv run pyrefly check --min-severity warn
uv run pytest -q --cov=bond_accounting
```
