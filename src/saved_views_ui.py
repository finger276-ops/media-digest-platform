# -*- coding: utf-8 -*-
"""Сохранённые виды: срез по тегам и фильтры ленты под именем.

Аналитик каждую неделю открывает одно и то же: «Технониколь · негатив ·
vk.com» — срез по тегу, фильтры в «Сообщениях», нужный раздел. Вид
сохраняет это под именем; открыть его можно выбором в шапке или ссылкой
вида ?view=… — её можно переслать коллеге или заказчику с кодом доступа.

Периоды в вид не входят: он применяется к тому, что выбрано в боковой
панели, — «Технониколь · негатив» одинаково нужен и в мае, и в июне.
Хранится в ручных правках проекта (таблица saved_views): попадает в журнал
правок и в резервные копии.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import streamlit as st

from messages_ui import PLATFORM_FILTER_KEY, TONE_FILTER_KEY
from services.cached_store import clear_platform_caches, delete_manual, list_manual, save_manual
from sidebar_ui import NAV_STATE_KEY
from tag_slice_ui import slice_state_key

VIEW_TABLE = "saved_views"
VIEW_PARAM = "view"
NO_VIEW = ""


def view_keys(project_id: str) -> dict[str, str]:
    """Что входит в вид: короткое имя → ключ состояния страницы."""
    suffix = project_id or "global"
    return {
        "slice": slice_state_key(project_id),
        "tags": f"messages_tag_filter_{suffix}",
        "tag_match": f"messages_tag_match_{suffix}",
        "platforms": PLATFORM_FILTER_KEY.format(suffix),
        "tones": TONE_FILTER_KEY.format(suffix),
        "types": f"messages_type_filter_{suffix}",
        "mode": "messages_block_mode",
        "search": "full_feed_search",
    }


def view_id(name: str) -> str:
    """Короткий постоянный ID из имени: то же имя — тот же вид (перезапись)."""
    return hashlib.sha1(" ".join(str(name).lower().split()).encode("utf-8")).hexdigest()[:10]


def capture_view(project_id: str, name: str) -> dict[str, Any]:
    """Снимок текущего среза, фильтров ленты и раздела."""
    state = {}
    for short, key in view_keys(project_id).items():
        value = st.session_state.get(key)
        if value not in (None, "", []):
            state[short] = list(value) if isinstance(value, (list, tuple)) else value
    return {
        "name": " ".join(str(name).split()),
        "page": st.session_state.get(NAV_STATE_KEY) or "",
        "state": state,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_view(project_id: str, view: dict[str, Any]) -> None:
    """Выставить срез, фильтры и раздел вида. Чего в виде нет — сбрасывается.

    Вызывать до того, как нарисованы виджеты: из обработчика выбора или в
    начале страницы.
    """
    state = view.get("state") or {}
    for short, key in view_keys(project_id).items():
        if short in state:
            st.session_state[key] = state[short]
        else:
            st.session_state.pop(key, None)
    # Номер страницы ленты — от прошлого отбора, у вида он свой: первая.
    st.session_state.pop("full_feed_page", None)
    if view.get("page"):
        st.session_state[NAV_STATE_KEY] = view["page"]


def list_views(project_id: str) -> dict[str, dict[str, Any]]:
    """Виды проекта: ID → вид, по имени."""
    try:
        rows = list_manual(project_id, VIEW_TABLE)
    except Exception:  # noqa: BLE001 — без видов дашборд работает как раньше
        return {}
    views = {}
    for _, row in rows.iterrows() if rows is not None and not rows.empty else []:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if payload.get("name"):
            views[str(row.get("row_key", "")).split("::", 1)[-1]] = payload
    return dict(sorted(views.items(), key=lambda item: str(item[1]["name"]).lower()))


def describe_view(view: dict[str, Any]) -> str:
    """«Сообщения · срез: Технониколь · площадка: vk.com · тональность: негатив»."""
    state = view.get("state") or {}
    parts = [str(view.get("page") or "")]
    labels = (("slice", "срез"), ("tags", "теги"), ("platforms", "площадка"), ("tones", "тональность"),
              ("types", "тип"), ("search", "поиск"))
    for short, title in labels:
        value = state.get(short)
        if value:
            text = ", ".join(str(v) for v in value) if isinstance(value, list) else f"«{value}»"
            parts.append(f"{title}: {text.lower() if short == 'tones' else text}")
    return " · ".join(part for part in parts if part)


def apply_view_from_link(project_id: str) -> None:
    """Открыть вид по ссылке ?view=… — один раз, до отрисовки меню и фильтров."""
    wanted = str(st.query_params.get(VIEW_PARAM) or "")
    marker = f"_applied_view::{project_id}"
    if not wanted or st.session_state.get(marker) == wanted:
        return
    view = list_views(project_id).get(wanted)
    st.session_state[marker] = wanted
    if view is None:
        return
    apply_view(project_id, view)
    st.session_state[f"saved_view_select::{project_id}"] = wanted


def _on_select(project_id: str) -> None:
    chosen = st.session_state.get(f"saved_view_select::{project_id}") or NO_VIEW
    if chosen == NO_VIEW:
        st.query_params.pop(VIEW_PARAM, None)
        return
    view = list_views(project_id).get(chosen)
    if view is None:
        return
    apply_view(project_id, view)
    st.session_state[f"_applied_view::{project_id}"] = chosen
    # Ссылка в адресной строке — сразу на этот вид: её можно скопировать.
    st.query_params[VIEW_PARAM] = chosen


def render_saved_views(project_id: str, *, can_edit: bool) -> None:
    """Выбор вида под срезом; редактор может сохранить текущий или удалить."""
    views = list_views(project_id)
    if not views and not can_edit:
        return
    select_key = f"saved_view_select::{project_id}"
    pending_key = f"_pending_view_select::{project_id}"
    # Выбор, заказанный кнопками прошлой перерисовки: значение виджета можно
    # менять только до того, как он нарисован.
    if pending_key in st.session_state:
        st.session_state[select_key] = st.session_state.pop(pending_key)
    if st.session_state.get(select_key) not in ({NO_VIEW} | set(views)):
        st.session_state[select_key] = NO_VIEW
    choice_col, action_col = st.columns([4, 1], vertical_alignment="bottom")
    with choice_col:
        chosen = st.selectbox(
            "Сохранённый вид",
            [NO_VIEW, *views],
            format_func=lambda vid: views[vid]["name"] if vid in views else "— не выбран —",
            key=select_key,
            on_change=_on_select,
            args=(project_id,),
            help="Срез по тегам, фильтры «Сообщений» и раздел под одним именем. "
            "Периоды не входят: вид применяется к выбранным в боковой панели.",
        )
    if chosen in views:
        base = str(getattr(st.context, "url", "") or "").split("?", 1)[0]
        st.caption(
            describe_view(views[chosen])
            + (f" · ссылка: {base}?{VIEW_PARAM}={chosen}" if base else f" · ссылка: ?{VIEW_PARAM}={chosen}")
        )
    if not can_edit:
        return
    with action_col:
        box = st.popover("Сохранить вид", width="stretch") if hasattr(st, "popover") else st.expander("Сохранить вид")
    with box:
        st.caption("Сохранит текущий срез по тегам, фильтры «Сообщений» и раздел. "
                   "То же имя — перезаписать вид.")
        name = st.text_input("Название вида", key=f"saved_view_name::{project_id}",
                             placeholder="Например: Технониколь · негатив · vk.com")
        if st.button("Сохранить этот вид", key=f"saved_view_save::{project_id}", disabled=not name.strip()):
            vid = view_id(name)
            save_manual(project_id, VIEW_TABLE, f"saved_view::{vid}", capture_view(project_id, name))
            clear_platform_caches(project_id)
            st.session_state[f"_applied_view::{project_id}"] = vid
            st.session_state[pending_key] = vid
            st.query_params[VIEW_PARAM] = vid
            st.rerun()
        if chosen in views and st.button(
            f"Удалить вид «{views[chosen]['name']}»", key=f"saved_view_delete::{project_id}"
        ):
            delete_manual(project_id, f"saved_view::{chosen}")
            clear_platform_caches(project_id)
            st.session_state[pending_key] = NO_VIEW
            st.query_params.pop(VIEW_PARAM, None)
            st.rerun()
