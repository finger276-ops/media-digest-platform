# -*- coding: utf-8 -*-
"""Живые сессии платформы: таблица platform_sessions (см. sql/platform_sessions_schema.sql).

У платформы нет системы логинов — сессия анонимна (случайный ID браузерной
вкладки). Модуль не зависит от Streamlit и не участвует в кешировании
services/cached_store.py: presence-данные читаются и пишутся напрямую, по
образцу services/ingest_queue.py — кешировать здесь нечего, данные и так
живут минуты.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from platform_store import get_supabase_client, now_iso

TABLE = "platform_sessions"
ROLES = ("owner", "editor", "viewer")
COLUMNS = ("session_id", "project_id", "role", "started_at", "last_seen_at")


def _rows_to_frame(rows: list[dict[str, Any]] | None) -> pd.DataFrame:
    """Всегда вернуть таблицу одной и той же формы.

    Форма гарантируется даже для строк, где колонки не оказалось: presence —
    вспомогательная панель, и отсутствие поля не должно превращаться в
    KeyError посреди отрисовки страницы.
    """
    if not rows:
        return pd.DataFrame(columns=list(COLUMNS))
    df = pd.DataFrame(rows)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    for col in ["started_at", "last_seen_at"]:
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    return df


def touch_session(session_id: str, *, project_id: str | None, role: str) -> None:
    """Отметить вкладку активной сейчас.

    started_at сознательно не передаётся: при первой вставке его проставит
    default now() в БД, а при повторном upsert он не затрагивается — вкладка
    не "теряет" момент, когда зашла, на каждом heartbeat.
    """
    session_id = str(session_id or "").strip()
    if not session_id or role not in ROLES:
        return
    client = get_supabase_client()
    client.table(TABLE).upsert(
        {
            "session_id": session_id,
            "project_id": str(project_id) if project_id else None,
            "role": role,
            "last_seen_at": now_iso(),
        },
        on_conflict="session_id",
    ).execute()


def list_recent_sessions(window_seconds: int = 1800) -> pd.DataFrame:
    """Сессии с активностью не раньше, чем window_seconds назад.

    Один запрос вместо отдельных "онлайн"/"недавно ушли": разбивка на
    корзины по last_seen_at делается на стороне вызывающего (UI), чтобы не
    ходить в Supabase дважды ради одного и того же среза данных.
    """
    threshold = (datetime.now(timezone.utc) - timedelta(seconds=int(window_seconds))).isoformat()
    resp = (
        get_supabase_client()
        .table(TABLE)
        .select("*")
        .gte("last_seen_at", threshold)
        .order("last_seen_at", desc=True)
        .execute()
    )
    return _rows_to_frame(getattr(resp, "data", None))


def cleanup_stale_sessions(older_than_seconds: int = 86400) -> None:
    """Удалить сессии, от которых давно не было heartbeat.

    Нет отдельного воркера/крона для presence, поэтому очистка — best-effort
    побочный эффект при открытии панели «Сессии» (owner-only, открывается
    нечасто, лишним запросом не нагружает обычную работу платформы).
    """
    threshold = (
        datetime.now(timezone.utc) - timedelta(seconds=int(older_than_seconds))
    ).isoformat()
    get_supabase_client().table(TABLE).delete().lt("last_seen_at", threshold).execute()
