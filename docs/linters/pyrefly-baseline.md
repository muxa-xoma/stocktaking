# pyrefly — baseline подавленных диагностик (Q32)

- **pyrefly version:** 1.3.1 (dev-dependency, `uv run pyrefly check`)
- **Дата фиксации baseline:** 2026-09-21 (файлы проекта менялись параллельными задачами; номера строк актуальны на момент фиксации)
- **Снимок:** `uv run pyrefly check` → `8 errors (15 suppressed, 1 warning not shown)`

## Почему счётчик показывает «15 suppressed», а реально подавлено 8

Число `15` в сводке — это **счётчик pyrefly**, а не количество реально скрытых диагностик.
В pyrefly 1.3.1 счётчик завышается директивами подавления (воспроизведено
экспериментально, см. «Метод верификации» ниже):

| Источник подавления | Реально скрыто диагностик | Вклад в счётчик |
|---|---|---|
| `tests/test_bonds.py:150` — `# type: ignore[arg-type]` | 7 | +7 (корректно) |
| `tests/test_settings.py:249` — `# type: ignore[arg-type]` | 1 | +2 (+1 фантомная единица) |
| `tests/test_settings.py:1` — `# pyrefly: ignore-errors` (file-level) | 0 дополнительно (дублирует подавление на 249) | +6 (фантомные единицы) |
| **Итого** | **8** | **15** |

Эксперименты в изолированной копии проекта показали, что file-level директива
`# pyrefly: ignore-errors` увеличивает счётчик `suppressed` непропорционально числу
реально скрытых диагностик: добавление file-level директивы в файл с одной видимой
ошибкой повышало счётчик на +6…+32 в зависимости от файла, при этом в выводе
скрывалась ровно одна диагностика. Это выглядит как дефект подсчёта в pyrefly 1.3.1.

## Полный список реально подавленных диагностик (8)

Все записи **verified**: подтверждены diff между `uv run pyrefly check --min-severity ignore --output-format json`
и тем же прогоном с `--enabled-ignores pyre` (все ignore-директивы отключены).

### 1–7. `tests/test_bonds.py:150:23` — `bad-argument-type` ×7 — verified

Код:

```python
def _create_data(isin: str, **overrides: object) -> BondCreate:
    fields: dict[str, object] = {...}
    fields.update(overrides)
    return BondCreate(**fields)  # type: ignore[arg-type]
```

Причина подавления: значения `dict[str, object]` не совпадают с типами полей
`BondCreate` (pydantic сузит их рантайм-валидацией). Одна директива подавляет по
одной диагностике на каждый keyword-аргумент конструктора:

| # | Параметр `BondCreate.__init__` | Ожидаемый тип |
|---|---|---|
| 1 | `isin` | `bytearray \| bytes \| str` |
| 2 | `name` | `bytearray \| bytes \| str` |
| 3 | `nominal` | `Decimal \| bool \| bytes \| float \| int \| str` |
| 4 | `coupon_rate` | `Decimal \| bool \| bytes \| float \| int \| str` |
| 5 | `coupon_frequency` | `Literal['ANNUAL', 'QUARTERLY', 'SEMI_ANNUAL']` |
| 6 | `maturity_date` | `Decimal \| bytes \| date \| datetime \| float \| int \| str` |
| 7 | `issuer` | `bytearray \| bytes \| str \| None` |

### 8. `tests/test_settings.py:249:30` — `bad-argument-type` — verified

Код:

```python
def test_logging_config_rejects_invalid_format() -> None:
    """Only "json" and "text" are valid formats."""
    with pytest.raises(ValidationError, match="format"):
        LoggingConfig(format="xml")  # type: ignore[arg-type]  # invalid on purpose; ty: ignore[invalid-argument-type]
```

Причина подавления: тест намеренно передаёт невалидное значение `format="xml"`
(`Literal['json', 'text']`) — это само поведение под тестом.
Ошибка дополнительно накрывается file-level директивой `# pyrefly: ignore-errors`
в первой строке `tests/test_settings.py`.

## Директивы подавления, актуальные на момент baseline

- `tests/test_settings.py:1` — `# pyrefly: ignore-errors` (file-level; весь файл)
- `tests/test_settings.py:249` — `# type: ignore[arg-type]` (+ `# ty: ignore[...]`, не активен для pyrefly)
- `tests/test_bonds.py:150` — `# type: ignore[arg-type]`
- `tests/conftest.py:125` — `# type: ignore[method-assign]` (пустая: ошибок на строке нет)
- `tests/conftest.py:168` — `# type: ignore[method-assign]` (пустая)
- `tests/conftest.py:171` — `# type: ignore[method-assign]` (пустая)
- `tests/test_ui_import.py:38` — `# type: ignore[func-returns-value]` (пустая)
- `tests/test_smoke.py:14` — `# type: ignore[arg-type]` (пустая)
- `src/bond_accounting/config/settings.py:334` — `# type: ignore[call-arg]` (пустая)

«Пустые» директивы в счётчик `suppressed` не дают вклада (проверено удалением
каждой в изолированной копии — счётчик не менялся).

Конфигурация `[tool.pyrefly]` в `pyproject.toml` не содержит ignore-списков
(только `project-includes`, `project-excludes`, `python-version`); baseline-файл
pyrefly (`.pyrefly/baseline.json`) в проекте отсутствует.

## Историческая заметка: quirk с `jose` (Task 41)

В ранних прогонах (Task 13) подавление untyped-import для `python-jose`
настраивалось через `replace-imports-with-any = ['jose', 'jose.*']`; при этом
документированный quirk pyrefly 1.3.1: опция `replace-untyped-imports-with-any`
для `jose` **не работала**. Сейчас это только исторический контекст:
`python-jose` заменён на `PyJWT` (Task 41), импорта `jose` в кодовой базе
больше нет, и этот quirk на текущий baseline не влияет.

## Метод верификации (воспроизведение)

```bash
# 1. Текущее состояние (default): 8 errors (15 suppressed)
uv run pyrefly check

# 2. Полный список диагностик с отключёнными ignore-директивами
#    (pyre-тег не используется в кодовой базе → ни одна директива не активна):
uv run pyrefly check --enabled-ignores pyre --min-severity ignore --output-format json

# 3. Diff выводов (1) и (2) даёт полный список реально подавленных диагностик
#    (см. скрипт в истории Task 40).
```

Проверено также: `--min-severity ignore` НЕ раскрывает подавленные директивами
диагностики (сводка: `9 diagnostics (15 suppressed)`), `--verbose` и JSON-формат
также их не показывают — единственный надёжный способ — отключение ignore-директив
через `--enabled-ignores pyre`.
