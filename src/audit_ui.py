# -*- coding: utf-8 -*-
"""Страница «Платформа → Журнал»: кто, когда и что поменял в правках.

Только владельцу платформы. «Кто» — роль и анонимная сессия: у кодов доступа
нет личностей, но две правки с одной сессией сделаны в одной вкладке, и по
«Платформа → Сессии» видно, в каком проекте эта вкладка работала.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from error_ui import error_details_allowed, show_error
from services.audit_log import actor_title, load_log

ALL_PROJECTS = "__all__"
LIMIT = 500


def _moscow(value) -> str:
    stamp = pd.to_datetime(value, errors="coerce", utc=True)
    return "" if pd.isna(stamp) else stamp.tz_convert("Europe/Moscow").strftime("%d.%m.%Y %H:%M:%S")


def render_audit_page(projects: pd.DataFrame, *, is_admin: bool) -> None:
    st.header("Журнал правок")
    if not is_admin:
        st.info("Журнал правок доступен владельцу платформы.")
        return
    st.caption(
        "Каждая ручная правка — название и описание инфоповода, объединения, "
        "скрытые и перенесённые сообщения, саммари — с автором и временем. "
        "Автор — роль и анонимная сессия вкладки: у кодов доступа нет имён. "
        "Выберите строку, чтобы увидеть, что было и что стало."
    )
    names = {}
    if isinstance(projects, pd.DataFrame) and not projects.empty:
        names = {
            str(row["project_id"]): str(row.get("project_name") or row["project_id"])
            for _, row in projects.iterrows()
        }
    choice = st.selectbox(
        "Проект",
        [ALL_PROJECTS] + list(names),
        format_func=lambda pid: "Все проекты" if pid == ALL_PROJECTS else names.get(pid, pid),
        key="audit_project",
    )
    try:
        log = load_log(None if choice == ALL_PROJECTS else choice, limit=LIMIT)
    except Exception as exc:  # noqa: BLE001 — нет таблицы или связи
        show_error("Журнал правок сейчас недоступен.", exc, warning=True)
        if error_details_allowed():
            st.caption(
                "Если таблицы ещё нет — выполните в Supabase миграцию "
                "sql/migrations/0007_platform_audit_log.sql."
            )
        return
    if log.empty:
        st.info("Правок пока нет.")
        return

    view = pd.DataFrame(
        {
            "Когда": log["created_at"].map(_moscow),
            "Проект": log["project_id"].astype(str).map(lambda pid: names.get(pid, pid)),
            "Кто": [actor_title(r, s) for r, s in zip(log.get("actor_role", ""), log.get("actor_session", ""))],
            "Что": log.get("summary", pd.Series([""] * len(log))).astype(str),
        }
    )
    st.caption(f"Показаны последние {len(view)} правок." if len(view) >= LIMIT else f"Правок: {len(view)}.")
    event = st.dataframe(
        view,
        hide_index=True,
        width="stretch",
        selection_mode="single-row",
        on_select="rerun",
        key="audit_table",
    )
    rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
    if not rows:
        return
    entry = log.iloc[rows[0]]
    st.markdown(f"**{entry.get('summary')}**")
    left, right = st.columns(2)
    for column, title, key in ((left, "Было", "before"), (right, "Стало", "after")):
        with column:
            st.caption(title)
            value = entry.get(key)
            if isinstance(value, dict) and value:
                st.code(json.dumps(value, ensure_ascii=False, indent=2), language="json")
            else:
                st.caption("—")
