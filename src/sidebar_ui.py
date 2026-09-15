# -*- coding: utf-8 -*-
"""Общие элементы боковой панели: меню разделов (кнопки), переключатель
клиентского/аналитического вида, порог склейки похожих заголовков и порог
скрытия малых инфоповодов.

Эти контролы используются из нескольких разделов дашборда, поэтому вынесены
в отдельный модуль, а не дублируются.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.cached_store import cache_version
from services.dashboard_config import SECTION_ALIASES
from services.event_titles import DEFAULT_SIMILARITY, merge_similar_events
from services.metrics_compute import format_int
from services.perf import perf_block
from services.project_settings import default_min_event_messages
from services.roles import role_rank

NAV_STATE_KEY = "platform_nav_page"


def dashboard_view_mode_for_session(
    role: str, settings: dict[str, Any], *, key: str = "dashboard_view_mode"
) -> str:
    default_mode = str(settings.get("default_view_mode") or "client")
    if role_rank(role) < role_rank("editor"):
        st.sidebar.caption("Вид дашборда: клиентский")
        return "client"
    mode_labels = {"client": "Клиентский", "analyst": "Аналитический"}
    options = ["client", "analyst"]
    selected = st.sidebar.radio(
        "Вид дашборда",
        options,
        index=options.index(default_mode) if default_mode in options else 0,
        format_func=lambda x: mode_labels.get(x, x),
        horizontal=False,
        key=key,
        help="Клиентский вид скрывает технические настройки и оставляет чистый презентационный интерфейс. Аналитический вид показывает рабочие настройки и расширенные элементы.",
    )
    return selected


def render_min_event_messages_control(
    profile: str,
    events: pd.DataFrame | None = None,
    *,
    key: str = "min_event_messages",
    container: Any = None,
) -> int:
    default_value = int(default_min_event_messages(profile, events))
    max_value = 50
    help_text = (
        "Инфоповоды с меньшим числом сообщений скрываются из таблицы и саммари. "
        "Сообщения при этом остаются в общей статистике и полной ленте."
    )
    target = container if container is not None else st.sidebar
    return int(
        target.number_input(
            "Мин. сообщений в инфоповоде",
            min_value=1,
            max_value=max_value,
            value=default_value,
            step=1,
            help=help_text,
            key=key,
        )
    )


@st.cache_data(show_spinner=False, max_entries=6, ttl=900)
def _cached_merge_similar_events(
    _events_agg: pd.DataFrame,
    project_id: str,
    period_ids_key: tuple[str, ...],
    threshold: float,
    blocked_key: tuple[str, ...],
    data_version: int,
    manual_version: int,
):
    with perf_block("dashboard.merge_titles", project_id=project_id):
        return merge_similar_events(
            _events_agg, threshold=threshold, blocked=set(blocked_key)
        )


def cached_merge_similar_events(
    events_agg: pd.DataFrame,
    project_id: str,
    period_ids: tuple[str, ...],
    threshold: float,
    blocked: tuple[str, ...],
):
    """Склейка заголовков с кэшем по дешёвым ключам.

    Сам кадр в ключ кэша не попадает (аргумент с подчёркиванием) — вместо него
    версии данных проекта. Хеширование DataFrame однажды уже стоило платформе
    секунд на каждом перерисовывании.
    """
    return _cached_merge_similar_events(
        events_agg,
        project_id,
        tuple(period_ids),
        float(threshold),
        tuple(blocked),
        cache_version(project_id, "data"),
        cache_version(project_id, "manual"),
    )


TITLE_MERGE_OPTIONS: dict[str, float] = {
    "Выключено": 0.0,
    "Осторожно": 0.75,
    "Обычно": DEFAULT_SIMILARITY,
    "Агрессивно": 0.5,
}


def render_title_merge_control(
    saved: float, *, key: str = "title_merge", container: Any = None
) -> float:
    """Переключатель силы склейки инфоповодов с близкими заголовками."""
    labels = list(TITLE_MERGE_OPTIONS)
    values = [TITLE_MERGE_OPTIONS[label] for label in labels]
    current = float(saved or 0.0)
    # Ближайший пресет к сохранённому значению.
    index = min(range(len(values)), key=lambda i: abs(values[i] - current))
    if current <= 0:
        index = labels.index("Выключено")
    target = container if container is not None else st.sidebar
    choice = target.selectbox(
        "Склейка похожих заголовков",
        labels,
        index=index,
        key=key,
        help=(
            "Brand Analytics переформулирует один и тот же сюжет от периода к "
            "периоду, поэтому инфоповоды с почти одинаковыми заголовками "
            "объединяются. Что именно склеилось, видно в разделе «Инфоповоды»."
        ),
    )
    return float(TITLE_MERGE_OPTIONS[choice])


def filter_small_events(
    events_agg: pd.DataFrame, min_messages: int
) -> tuple[pd.DataFrame, int, int]:
    """Hide tiny information events from dashboard-level analytics.

    This does not delete events or messages from storage; it only filters the
    analytical view. It is intended to suppress one-off algorithmic clusters
    such as `Обсуждение: ...` with 1-3 messages.
    """
    if (
        events_agg is None
        or events_agg.empty
        or min_messages <= 1
        or "message_count" not in events_agg.columns
    ):
        return events_agg, 0, 0
    work = events_agg.copy()
    counts = pd.to_numeric(work["message_count"], errors="coerce").fillna(0).astype(int)
    keep_mask = counts >= int(min_messages)
    hidden_events = int((~keep_mask).sum())
    hidden_messages = int(counts[~keep_mask].sum())
    return work[keep_mask].copy(), hidden_events, hidden_messages


def render_small_events_notice(
    hidden_events: int, hidden_messages: int, min_messages: int
) -> None:
    if hidden_events <= 0:
        return
    st.caption(
        f"Скрыто малых инфоповодов: {format_int(hidden_events)} "
        f"(< {format_int(min_messages)} сообщений). "
        f"Сообщений в них: {format_int(hidden_messages)}. "
        "Они не удалены и остаются в общей статистике/ленте."
    )


def normalize_section(name: Any, options: list[str]) -> str:
    """Привести название раздела к актуальному, с учётом старых настроек."""
    value = str(name or "").strip()
    value = SECTION_ALIASES.get(value, value)
    return value if value in options else (options[0] if options else "")


def _nav_button_type(active: bool) -> str:
    """Активный пункт меню выделен заливкой, остальные — плоские."""
    if active:
        return "primary"
    return "tertiary" if _supports_tertiary_buttons() else "secondary"


def _supports_tertiary_buttons() -> bool:
    if "_tertiary_ok" not in st.session_state:
        try:
            import inspect

            source = inspect.signature(st.button)
            st.session_state["_tertiary_ok"] = "type" in source.parameters
        except Exception:
            st.session_state["_tertiary_ok"] = False
    return bool(st.session_state.get("_tertiary_ok"))


def _select_nav_page(item: str) -> None:
    """Колбэк кнопки меню: выполняется до перезапуска скрипта."""
    st.session_state[NAV_STATE_KEY] = item


def render_sidebar_nav(
    groups: list[tuple[str, list[str]]],
    default: str,
    after_group: dict[str, Any] | None = None,
) -> str:
    """Единое меню разделов в боковой панели.

    Кнопки вместо радио: группы получают заголовки, активный пункт видно сразу,
    а переход не требует прокрутки страницы. `after_group` позволяет вставить
    свой блок сразу после нужной группы — так выбор периодов оказывается рядом
    с разделами аналитики, а не в самом низу панели.
    """
    available = [item for _, items in groups for item in items]
    if not available:
        return ""

    current = st.session_state.get(NAV_STATE_KEY)
    if current not in available:
        current = default if default in available else available[0]
        st.session_state[NAV_STATE_KEY] = current

    for title, items in groups:
        if not items:
            continue
        st.sidebar.caption(title)
        for item in items:
            st.sidebar.button(
                item,
                key=f"nav_btn_{item}",
                width="stretch",
                type=_nav_button_type(item == current),
                on_click=_select_nav_page,
                args=(item,),
            )
        hook = (after_group or {}).get(title)
        if callable(hook):
            hook()
    return current
