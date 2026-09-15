# -*- coding: utf-8 -*-
"""Состояние фильтра «выбранный инфоповод».

Общее между разделами «Инфоповоды» и «Сообщения»: выбор строки в таблице
инфоповодов сохраняется в session_state и сужает ленту сообщений до этого
инфоповода, пока фильтр не сброшен.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st


def selected_event_filter_key(project_id: str | None) -> str:
    return f"selected_event_filter::{project_id or 'global'}"


def set_selected_event_filter(project_id: str | None, selected: pd.Series) -> None:
    event_ids = [
        str(x) for x in (selected.get("event_ids", []) or []) if str(x).strip()
    ]
    if not event_ids and "event_id" in selected.index:
        event_ids = [str(selected.get("event_id"))]
    st.session_state[selected_event_filter_key(project_id)] = {
        "title": str(
            selected.get("title")
            or selected.get("event_title")
            or "Выбранный инфоповод"
        ),
        "event_ids": event_ids,
        "group_key": str(selected.get("group_key") or ""),
    }


def get_selected_event_filter(project_id: str | None) -> dict[str, Any] | None:
    value = st.session_state.get(selected_event_filter_key(project_id))
    return value if isinstance(value, dict) and value.get("event_ids") else None


def clear_selected_event_filter(project_id: str | None) -> None:
    st.session_state.pop(selected_event_filter_key(project_id), None)


def filter_messages_by_selected_event(
    messages: pd.DataFrame, event_filter: dict[str, Any] | None
) -> pd.DataFrame:
    if messages is None or messages.empty or not event_filter:
        return messages
    event_ids = {str(x) for x in event_filter.get("event_ids", []) if str(x).strip()}
    if not event_ids:
        return messages
    mask = pd.Series(False, index=messages.index)
    for col in [
        "event_id",
        "source_event_id",
        "final_event_id",
        "source_final_event_id",
    ]:
        if col in messages.columns:
            mask = mask | messages[col].fillna("").astype(str).isin(event_ids)
    if mask.any():
        return messages[mask].copy()

    # Fallback for imported sources where event links may be reconstructed by title.
    title = str(event_filter.get("title") or "").strip().lower()
    if title:
        for col in ["event_title", "source_main_topic", "Сюжет"]:
            if col in messages.columns:
                fallback_mask = (
                    messages[col]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    .eq(title)
                )
                if fallback_mask.any():
                    return messages[fallback_mask].copy()
    return messages.iloc[0:0].copy()


def event_series_filter(selected: pd.Series) -> dict[str, Any]:
    event_ids = [
        str(x) for x in (selected.get("event_ids", []) or []) if str(x).strip()
    ]
    if not event_ids and "event_id" in selected.index:
        event_ids = [str(selected.get("event_id"))]
    return {
        "title": str(
            selected.get("title")
            or selected.get("event_title")
            or "Выбранный инфоповод"
        ),
        "event_ids": event_ids,
        "group_key": str(
            selected.get("group_key")
            or selected.get("event_id")
            or selected.get("title")
            or "event"
        ),
    }
