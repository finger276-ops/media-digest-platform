# -*- coding: utf-8 -*-
"""Heartbeat живых сессий + раздел «Платформа → Сессии» (только владелец).

Presence анонимна: у платформы нет логинов, поэтому счётчик показывает
браузерные вкладки (роль + проект + с какого момента активна), а не имена
людей. См. sql/platform_sessions_schema.sql и services/session_presence.py.

Обновление сознательно НЕ использует st.fragment(run_every=...): этот
параметр приводит к зависанию при импорте модуля вне полноценной сессии
Streamlit-рантайма (воспроизводится детерминированно даже на голом `python -c
"import app"`, без какого-либо тестового окружения) — то есть риск не
ограничен тестами, а мог бы проявиться и в проде. Поэтому heartbeat
отмечается на каждый обычный rerun (клик где угодно в приложении), а
панель «Сессии» обновляется по кнопке.
"""

from __future__ import annotations

import time
import uuid

import pandas as pd
import streamlit as st

from services.cached_store import list_projects
from services.perf import perf_block
from services.session_presence import (
    cleanup_stale_sessions,
    list_recent_sessions,
    touch_session,
)

SESSION_ID_KEY = "_presence_session_id"
LAST_TOUCH_KEY = "_presence_last_touch"
# Streamlit перезапускает скрипт на каждый клик, поэтому без этого окна
# presence стоил бы одного запроса в Supabase на каждое действие
# пользователя — заметная задержка перед отрисовкой любой страницы.
HEARTBEAT_MIN_INTERVAL_SECONDS = 30
# Больше интервала heartbeat, чтобы вкладка не мигала "недавно ушёл"
# между двумя отметками.
ONLINE_WINDOW_SECONDS = 100
RECENT_WINDOW_SECONDS = 30 * 60

ROLE_LABELS = {"owner": "Владелец", "editor": "Редактор", "viewer": "Просмотр"}


def _session_id() -> str:
    if SESSION_ID_KEY not in st.session_state:
        st.session_state[SESSION_ID_KEY] = uuid.uuid4().hex
    return st.session_state[SESSION_ID_KEY]


def render_presence_heartbeat(project_id: str | None, role: str) -> None:
    """Отметить текущую вкладку активной в platform_sessions.

    Вызывается на каждом rerun main(), но реально пишет в Supabase не чаще
    HEARTBEAT_MIN_INTERVAL_SECONDS: presence — фоновая справочная функция и
    не должна добавлять сетевой запрос к каждому клику пользователя. Смена
    проекта или роли пишется сразу, не дожидаясь окна, иначе панель
    показывала бы устаревший проект до полуминуты.
    """
    signature = (str(project_id or ""), role)
    last = st.session_state.get(LAST_TOUCH_KEY)
    if (
        isinstance(last, tuple)
        and last[1] == signature
        and time.monotonic() - last[0] < HEARTBEAT_MIN_INTERVAL_SECONDS
    ):
        return
    # Отметку времени ставим до запроса: если Supabase недоступен, повтор
    # будет по тому же расписанию, а не на каждый клик.
    st.session_state[LAST_TOUCH_KEY] = (time.monotonic(), signature)
    try:
        with perf_block("presence.heartbeat"):
            touch_session(_session_id(), project_id=project_id, role=role)
    except Exception:
        pass  # presence — не критичная функция, сеть/Supabase не должны ронять страницу


def _format_duration(seconds: float) -> str:
    if seconds is None or pd.isna(seconds):
        return "—"
    seconds = max(0, int(seconds))
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def _render_sessions_table(
    df: pd.DataFrame,
    project_names: dict[str, str],
    *,
    now: pd.Timestamp,
    duration_from: str,
) -> None:
    view = df.copy()
    view["Проект"] = view["project_id"].map(project_names).fillna(
        view["project_id"].fillna("— без проекта —")
    )
    view["Роль"] = view["role"].map(ROLE_LABELS).fillna(view["role"])
    view["С какого момента"] = view["started_at"].dt.strftime("%d.%m.%Y %H:%M").fillna("—")
    view["Длительность"] = (now - view[duration_from]).dt.total_seconds().apply(_format_duration)
    st.dataframe(
        view[["Проект", "Роль", "С какого момента", "Длительность"]],
        hide_index=True,
        width="stretch",
    )


def render_session_presence_page() -> None:
    st.header("Сессии")
    if st.button("Обновить", key="presence_refresh"):
        st.rerun()

    try:
        cleanup_stale_sessions()
    except Exception:
        pass  # очистка — best-effort, не должна мешать показать текущие данные
    try:
        sessions = list_recent_sessions(RECENT_WINDOW_SECONDS)
    except Exception as exc:
        st.error("Не удалось получить список сессий.")
        st.exception(exc)
        return

    now = pd.Timestamp.now(tz="UTC")
    if sessions.empty:
        online = sessions
        recently_left = sessions
    else:
        online_cutoff = now - pd.Timedelta(seconds=ONLINE_WINDOW_SECONDS)
        is_online = sessions["last_seen_at"] >= online_cutoff
        online = sessions[is_online]
        recently_left = sessions[~is_online]

    try:
        projects = list_projects(include_inactive=True)
    except Exception:
        projects = pd.DataFrame()
    project_names = (
        dict(zip(projects["project_id"].astype(str), projects["project_name"].astype(str)))
        if not projects.empty
        else {}
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Сейчас онлайн", len(online))
    c2.metric(
        "Проектов с активностью",
        online["project_id"].nunique() if not online.empty else 0,
    )
    c3.metric("Недавно ушли", len(recently_left))
    st.caption(
        f"«Онлайн» — heartbeat вкладки за последние {ONLINE_WINDOW_SECONDS} сек. "
        f"«Недавно ушли» — были активны за последние {RECENT_WINDOW_SECONDS // 60} мин, но сейчас не онлайн. "
        "Список не обновляется сам — нажмите «Обновить». "
        "Без логинов на платформе сессия — это браузерная вкладка, а не человек: "
        "два зрителя с одним кодом просмотра будут видны как две отдельные сессии."
    )

    if online.empty:
        st.info("Сейчас никто не онлайн.")
    else:
        st.markdown("#### По проектам")
        breakdown = (
            online.assign(
                Проект=online["project_id"]
                .map(project_names)
                .fillna(online["project_id"].fillna("— без проекта —"))
            )
            .groupby("Проект")
            .agg(Онлайн=("session_id", "count"))
            .reset_index()
            .sort_values("Онлайн", ascending=False)
        )
        st.dataframe(breakdown, hide_index=True, width="stretch")

        st.markdown("#### Онлайн сейчас")
        _render_sessions_table(online, project_names, now=now, duration_from="started_at")

    if not recently_left.empty:
        st.markdown("#### Недавно ушли")
        _render_sessions_table(recently_left, project_names, now=now, duration_from="last_seen_at")
