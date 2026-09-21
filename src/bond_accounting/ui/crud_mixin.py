"""Generic CRUD mixin for NiceGUI справочник-страниц.

Выносит повторяющийся CRUD-сценарий справочника (таблица + форма создания +
форма правки/удаления выбранной записи) в переиспользуемый
:class:`CrudPageMixin`. Конкретная страница:

* объявляет 11 ``ClassVar``-полей с морфологией сообщений (``entity_title``
  и др., см. список ниже);
* в ``render()`` создаёт state-объект (виджеты + кэш + текущий выбор) в
  локальной области видимости и передаёт его первым аргументом в методы
  миксина;
* реализует абстрактные хуки, которые обращаются к своему сервису и DTO.

Миксин спроектирован как ``ABC`` (`CrudPageMixin`) и напрямую не
инстанцируется. Все методы принимают ``state`` первым аргументом (после
``self``) — это объект дата-класса, созданный в ``render()``, а не атрибут
экземпляра страницы.

Структура state-объекта описана Протоколом :class:`CrudState` (поля ``cache``,
``selected_id``, ``table``, ``selected_label``, ``delete_dialog``), а сам миксин
параметризован типом state-объекта ``StateT``. Конкретная страница не
указывает generic-параметр явно — mypy выводит его из сигнатур абстрактных
хуков, которые принимают конкретный дата-класс (duck-typing, структурно
совместимый с ``CrudState``).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Protocol

from nicegui import ui
from pydantic import ValidationError

from bond_accounting.ui.common import notify_error

logger = logging.getLogger(__name__)


class CrudState(Protocol):
    """Минимальный контракт state-объекта для методов CRUD-миксина."""

    cache: dict[int, Any]
    selected_id: int | None
    table: Any
    selected_label: Any
    delete_dialog: Any


class CrudPageMixin[StateT: CrudState](ABC):
    """CRUD-сценарий для справочников: таблица, создание, правка, удаление.

    Наследник объявляет морфологию сообщений (см. ``ClassVar``-поля) и
    реализует абстрактные хуки. Общие методы оперируют над переданным
    ``state``-объектом, поэтому не зависят от конкретной сущности;
    ``StateT`` выводится mypy из сигнатур хуков наследника.
    """

    #: Имя сущности в именительном падеже, напр. «Облигация».
    entity_title: ClassVar[str] = ""
    #: Имя сущности в винительном падеже, напр. «облигацию» («Сначала выберите…»).
    entity_accusative: ClassVar[str] = ""
    #: Имя сущности в родительном падеже, напр. «облигации» («Некорректные данные…»).
    entity_genitive: ClassVar[str] = ""
    #: Сказуемое, предшествующее caption выбранной записи, напр. «Выбрана облигация:».
    selected_verb: ClassVar[str] = ""
    #: Текст, когда запись не выбрана (сброс выборки).
    no_selection_text: ClassVar[str] = ""
    #: Сообщение, что поле «Название» обязательно (для формы правки).
    name_required_message: ClassVar[str] = ""
    #: Окончание-сказуемое успешного создания, напр. «добавлена».
    created_suffix: ClassVar[str] = ""
    #: Окончание-сказуемое успешного обновления, напр. «обновлена».
    updated_suffix: ClassVar[str] = ""
    #: Сообщение об успешном удалении.
    deleted_message: ClassVar[str] = ""
    #: Сообщение «запись не найдена».
    not_found_message: ClassVar[str] = ""
    #: Исключение (или кортеж), означающее «удаление запрещено» (напр. есть сделки).
    delete_guard_error: ClassVar[type[Exception] | tuple[type[Exception], ...]] = ()

    # -- Абстрактные хуки --------------------------------------------------

    @abstractmethod
    async def _load_rows(self, state: StateT) -> list[dict[str, Any]]:
        """Загрузить записи справочника (строки таблицы) и обновить кэш."""
        raise NotImplementedError

    @abstractmethod
    def _fill_edit_form(self, state: StateT, entity: Any) -> None:
        """Заполнить форму правки значениями выбранной записи."""
        raise NotImplementedError

    @abstractmethod
    def _entity_name(self, entity: Any) -> str:
        """Краткое имя записи (напр. ISIN) для сообщений о создании/обновлении."""
        raise NotImplementedError

    @abstractmethod
    def _entity_caption(self, entity: Any) -> str:
        """Полная подпись записи, выводимая после выбора в таблице."""
        raise NotImplementedError

    @abstractmethod
    def _build_create_dto(self, state: StateT) -> Any:
        """Собрать DTO создания из полей формы создания."""
        raise NotImplementedError

    @abstractmethod
    def _build_update_dto(self, state: StateT) -> Any:
        """Собрать DTO обновления из полей формы правки."""
        raise NotImplementedError

    @abstractmethod
    async def _service_create(self, state: StateT, dto: Any) -> Any:
        """Создать запись через сервис и залогировать факт создания."""
        raise NotImplementedError

    @abstractmethod
    async def _service_update(self, state: StateT, entity_id: int, dto: Any) -> Any:
        """Обновить запись через сервис; вернуть обновлённую или ``None``."""
        raise NotImplementedError

    @abstractmethod
    async def _service_delete(self, state: StateT, entity_id: int) -> bool:
        """Удалить запись через сервис; вернуть ``True``, если запись удалена."""
        raise NotImplementedError

    def _prevalidate_create(self, state: StateT) -> str | None:
        """Дополнительная проверка перед созданием; по умолчанию её нет."""
        return None

    def _prevalidate_update(self, state: StateT) -> str | None:
        """Дополнительная проверка перед обновлением; по умолчанию её нет."""
        return None

    # -- Общие методы CRUD -------------------------------------------------

    async def _reload(self, state: StateT) -> None:
        """Перезагрузить таблицу и сбросить выбор записи."""
        try:
            state.table.rows = await self._load_rows(state)
        except Exception as exc:
            notify_error(exc)
            return
        state.selected_id = None
        state.table.selected = []
        state.selected_label.set_text(self.no_selection_text)

    def _on_table_select(self, state: StateT, e: Any) -> None:
        """Заполнить форму правки при выборе строки таблицы."""
        selection = e.selection
        if not selection:
            state.selected_id = None
            return
        row = selection[0]
        entity = state.cache.get(row["id"])
        if entity is None:
            state.selected_id = None
            return
        state.selected_id = entity.id
        state.selected_label.set_text(f"{self.selected_verb} {self._entity_caption(entity)}")
        self._fill_edit_form(state, entity)

    async def _handle_create(self, state: StateT) -> None:
        """Создать запись справочника из формы создания."""
        pre_error = self._prevalidate_create(state)
        if pre_error:
            ui.notify(pre_error, type="negative")
            return
        try:
            dto = self._build_create_dto(state)
            entity = await self._service_create(state, dto)
        except (ValidationError, ValueError) as exc:
            ui.notify(f"Некорректные данные {self.entity_genitive}: {exc}", type="negative")
            return
        except Exception as exc:
            notify_error(exc)
            return
        ui.notify(
            f"{self.entity_title} {self._entity_name(entity)} {self.created_suffix}",
            type="positive",
        )
        await self._reload(state)

    async def _handle_update(self, state: StateT) -> None:
        """Сохранить изменения выбранной записи."""
        if state.selected_id is None:
            ui.notify(f"Сначала выберите {self.entity_accusative} в таблице", type="warning")
            return
        pre_error = self._prevalidate_update(state)
        if pre_error:
            ui.notify(pre_error, type="negative")
            return
        try:
            dto = self._build_update_dto(state)
            updated = await self._service_update(state, state.selected_id, dto)
        except (ValidationError, ValueError) as exc:
            ui.notify(f"Некорректные данные {self.entity_genitive}: {exc}", type="negative")
            return
        except Exception as exc:
            notify_error(exc)
            return
        if updated is None:
            ui.notify(self.not_found_message, type="warning")
            await self._reload(state)
            return
        ui.notify(
            f"{self.entity_title} {self._entity_name(updated)} {self.updated_suffix}",
            type="positive",
        )
        await self._reload(state)

    def _handle_delete(self, state: StateT) -> None:
        """Открыть подтверждение удаления выбранной записи."""
        if state.selected_id is None:
            ui.notify(f"Сначала выберите {self.entity_accusative} в таблице", type="warning")
            return
        state.delete_dialog.open()

    async def _confirm_delete(self, state: StateT) -> None:
        """Удалить выбранную запись после подтверждения."""
        state.delete_dialog.close()
        if state.selected_id is None:
            return
        try:
            deleted = await self._service_delete(state, state.selected_id)
        except self.delete_guard_error as exc:
            ui.notify(f"Нельзя удалить {self.entity_accusative}: {exc}", type="negative")
            return
        except Exception as exc:
            notify_error(exc)
            return
        if deleted:
            ui.notify(self.deleted_message, type="positive")
        else:
            ui.notify(self.not_found_message, type="warning")
        await self._reload(state)
