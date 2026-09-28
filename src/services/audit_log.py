# -*- coding: utf-8 -*-
"""Журнал ручных правок: кто, когда и что поменял.

Правки хранятся в platform_manual_rows и перезаписываются — прежнее значение
пропадает, а кто его поменял, неизвестно. Журнал (platform_audit_log,
миграция 0007) дописывает строку на каждую запись и удаление правки: роль и
анонимная сессия автора, время, прежнее и новое значение, сводка словами.

Запись в журнал — лучшее усилие: если таблицы нет (миграция не накачена) или
база не ответила, правка всё равно сохраняется, а в лог уходит
предупреждение. Иначе журнал ломал бы ровно то, что должен описывать.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import pandas as pd

import platform_store as store

LOGGER = logging.getLogger("platform.audit")
AUDIT_TABLE = "platform_audit_log"

ROLE_TITLES = {"owner": "Владелец", "editor": "Аналитик", "viewer": "Пользователь"}

_SAVE_TITLES = {
    "event_edits": "Правка инфоповода",
    "event_merges": "Инфоповоды объединены",
    "message_hidden": "Сообщение скрыто",
    "message_moves": "Сообщение перенесено в другой инфоповод",
    "message_irrelevant": "Сообщение отмечено как не относящееся к инфоповоду",
    "manual_events": "Инфоповод создан вручную",
    "title_merge_blocks": "Заголовок выведен из автосклейки",
    "summaries": "Саммари сохранено",
    "ai_texts": "Текст от ИИ сохранён",
    "rebuild_orphans": "Правка отложена при пересборке периода",
    "saved_views": "Вид сохранён",
}
_DELETE_TITLES = {
    "event_edits": "Правка инфоповода отменена",
    "event_merges": "Объединение инфоповодов отменено",
    "message_hidden": "Сообщение возвращено",
    "message_moves": "Перенос сообщения отменён",
    "message_irrelevant": "Отметка «не относится» снята",
    "manual_events": "Ручной инфоповод удалён",
    "title_merge_blocks": "Заголовок возвращён в автосклейку",
    "summaries": "Саммари удалено",
    "ai_texts": "Текст от ИИ удалён",
    "saved_views": "Вид удалён",
}
# Поля правки инфоповода и как они называются в сводке.
_EVENT_FIELDS = (("title", "название"), ("description", "описание"), ("tags", "теги"), ("hidden", "скрытие"))


_REASON: ContextVar[str] = ContextVar("audit_reason", default="")


@contextmanager
def audit_reason(reason: str) -> Iterator[None]:
    """Пометить правки внутри блока причиной: «Пересборка периода: …».

    Без неё перенос правки на новый ID при пересборке выглядел бы в журнале как
    отмена правки аналитиком.
    """
    token = _REASON.set(reason)
    try:
        yield
    finally:
        _REASON.reset(token)


def _short(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize(action: str, table_name: str, before: dict | None, after: dict | None) -> str:
    """Сводка правки словами — то, что владелец читает в журнале."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    if action == "delete":
        return _DELETE_TITLES.get(table_name, f"Удалена правка ({table_name})")
    title = _SAVE_TITLES.get(table_name, f"Правка ({table_name})")
    if table_name == "event_edits":
        changes = []
        for field, name in _EVENT_FIELDS:
            old, new = before.get(field), after.get(field)
            if field in after and (old or "") != (new or ""):
                if field == "hidden":
                    changes.append("инфоповод скрыт" if new else "инфоповод возвращён")
                elif old:
                    changes.append(f"{name}: «{_short(old, 40)}» → «{_short(new, 40)}»")
                else:
                    changes.append(f"{name}: «{_short(new, 60)}»")
        return f"{title}: {'; '.join(changes)}" if changes else title
    if table_name == "manual_events" and after.get("title"):
        return f"{title}: «{_short(after.get('title'), 60)}»"
    if table_name == "title_merge_blocks" and after.get("title"):
        return f"{title}: «{_short(after.get('title'), 60)}»"
    if table_name == "saved_views" and after.get("name"):
        return f"{title}: «{_short(after.get('name'), 60)}»"
    return title


def current_actor() -> tuple[str, str]:
    """Роль и анонимная сессия того, кто сейчас работает с платформой.

    Вне Streamlit (воркер, скрипты) — «система»: правку сделал не человек.
    """
    try:
        import streamlit as st

        state = st.session_state
        if state.get("platform_is_admin"):
            role = "owner"
        else:
            role = str(state.get("platform_project_role") or "")
        session = str(state.get("_presence_session_id") or "")
        return role or "system", session[:12]
    except Exception:  # noqa: BLE001 — вне приложения сессии нет
        return "system", ""


def record(
    project_id: str,
    action: str,
    table_name: str,
    row_key: str,
    *,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """Дописать строку журнала. Сбой не прерывает правку — только в лог."""
    role, session = current_actor()
    entry = {
        "project_id": str(project_id),
        "created_at": store.now_iso(),
        "actor_role": role,
        "actor_session": session,
        "action": action,
        "table_name": str(table_name or ""),
        "row_key": str(row_key or ""),
        "summary": (f"{_REASON.get()}: " if _REASON.get() else "")
        + summarize(action, table_name, before, after),
        "before": before if isinstance(before, dict) else None,
        "after": after if isinstance(after, dict) else None,
    }
    try:
        store.get_supabase_client().table(AUDIT_TABLE).insert(entry).execute()
    except Exception as exc:  # noqa: BLE001 — журнал не важнее самой правки
        LOGGER.warning("Журнал правок: запись не удалась (%s): %s", row_key, exc)


def load_log(project_id: str | None = None, *, limit: int = 500) -> pd.DataFrame:
    """Последние записи журнала, новые первыми."""
    client = store.get_supabase_client()
    query = client.table(AUDIT_TABLE).select("*")
    if project_id:
        query = query.eq("project_id", project_id)
    rows = query.order("created_at", desc=True).limit(limit).execute().data or []
    return pd.DataFrame(rows)


def actor_title(role: Any, session: Any) -> str:
    title = ROLE_TITLES.get(str(role or ""), "Система" if str(role) == "system" else str(role or ""))
    session = str(session or "")
    return f"{title} · сессия {session[:6]}" if session else title
