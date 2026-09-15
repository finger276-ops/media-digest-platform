# -*- coding: utf-8 -*-
"""Раздел «Сообщения»: топ-15 по вовлеченности и вся лента с пагинацией.

Если в разделе «Инфоповоды» выбран инфоповод, обе вкладки сужаются до его
сообщений (фильтр из services.event_filter_state); сбросить его можно
кнопкой прямо в этом блоке.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.event_filter_state import (
    clear_selected_event_filter,
    filter_messages_by_selected_event,
    get_selected_event_filter,
)
from services.formatting import fmt_date
from services.message_compute import message_link_column, message_text_column
from services.metrics_compute import format_int, numeric_series


def _value_from_row(row: pd.Series, *columns: str) -> str:
    """Return the first non-empty value from a message row."""
    for col in columns:
        if col in row.index:
            value = str(row.get(col) or "").strip()
            if value and value.lower() not in {"nan", "none", "nat", "null"}:
                return value
    return ""


def render_message_list(
    view: pd.DataFrame, *, text_col: str | None, link_col: str | None
) -> None:
    """Render messages as readable cards instead of a dataframe."""
    if view is None or view.empty:
        st.info("Сообщений для показа нет.")
        return

    for _, row in view.iterrows():
        date_text = fmt_date(row.get("datetime")) if "datetime" in row.index else ""
        source = _value_from_row(
            row, "chat_title", "platform", "source", "Источник", "Место публикации"
        )
        author = _value_from_row(row, "author", "Автор")
        sentiment = _value_from_row(row, "sentiment", "Тональность")
        event_title = _value_from_row(row, "event_title", "source_main_topic", "Сюжет")
        tags = _value_from_row(row, "tags", "Теги").replace("|", ", ")
        audience = int(row.get("_audience", 0) or 0)
        reach = int(row.get("_reach", 0) or 0)
        engagement = int(row.get("_engagement", 0) or 0)
        text = str(row.get(text_col, "") or "").strip() if text_col else ""
        link = str(row.get(link_col, "") or "").strip() if link_col else ""

        meta_parts = [part for part in [date_text, source, author, sentiment] if part]
        metrics_parts = [
            f"аудитория: {format_int(audience)}",
            f"охват: {format_int(reach)}",
            f"вовлеченность: {format_int(engagement)}",
        ]

        st.markdown("---")
        if meta_parts:
            st.caption(" · ".join(meta_parts))
        if event_title:
            st.markdown(f"**Инфоповод:** {event_title}")
        if tags:
            st.caption(f"Теги: {tags}")
        st.markdown(f"*{' · '.join(metrics_parts)}*")
        st.write(text[:1800] if text else "—")
        if link.startswith("http"):
            st.markdown(f"[Открыть сообщение]({link})")


def render_messages_block(
    messages: pd.DataFrame, *, project_id: str | None = None
) -> None:
    """Render key messages and full feed as a readable list.

    If an event was selected in the «Инфоповоды» section, both modes are
    filtered by that event: top messages and the full feed show only messages
    from the selected infopoint.
    """
    st.subheader("Ключевые сообщения")
    if messages is None or messages.empty:
        st.info("Сообщения не найдены.")
        return

    event_filter = get_selected_event_filter(project_id)
    if event_filter:
        c1, c2 = st.columns([4, 1])
        with c1:
            st.info(
                f"Выбран инфоповод: {event_filter.get('title')}. В топе и общей ленте показаны только сообщения этого инфоповода."
            )
        with c2:
            if st.button(
                "Сбросить фильтр",
                key=f"clear_event_message_filter_{project_id or 'global'}",
                use_container_width=True,
            ):
                clear_selected_event_filter(project_id)
                st.rerun()

    mode = st.radio(
        "Режим просмотра сообщений",
        ["Ключевые сообщения", "Вся лента"],
        horizontal=True,
        key="messages_block_mode",
    )

    work = messages.copy()
    if event_filter:
        work = filter_messages_by_selected_event(work, event_filter)
        if work.empty:
            st.warning(
                "По выбранному инфоповоду сообщения не найдены. Возможно, данные были пересобраны или связи инфоповодов изменились."
            )
            return
    text_col = message_text_column(work)
    link_col = message_link_column(work)
    work["_audience"] = numeric_series(work, ["audience", "Аудитория"]).astype(int)
    work["_reach"] = numeric_series(
        work, ["views", "Просмотры", "Просмотров", "reach", "Охват"]
    ).astype(int)
    work["_engagement"] = numeric_series(
        work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]
    ).astype(int)

    if mode == "Ключевые сообщения":
        scope = "выбранного инфоповода" if event_filter else "всей выборки"
        st.caption(
            f"Показаны 15 сообщений с максимальной вовлеченностью для {scope}. Если вовлеченность равна 0, дополнительными критериями выступают охват и аудитория."
        )
        view = (
            work.sort_values(["_engagement", "_reach", "_audience"], ascending=False)
            .head(15)
            .copy()
        )
    else:
        search = st.text_input(
            "Поиск по всей ленте",
            placeholder="Введите слово или фразу",
            key="full_feed_search",
        )
        view = work.copy()
        if search.strip() and text_col:
            view = view[
                view[text_col]
                .fillna("")
                .astype(str)
                .str.contains(search.strip(), case=False, regex=False)
            ]
        view = (
            view.sort_values("datetime", ascending=False)
            if "datetime" in view.columns
            else view
        )

        total_found = int(len(view))
        page_size = int(
            st.selectbox(
                "Сообщений на странице",
                [25, 50, 100, 200],
                index=1,
                key="full_feed_page_size",
            )
        )
        total_pages = max(1, (total_found + page_size - 1) // page_size)
        page = int(
            st.number_input(
                "Страница",
                min_value=1,
                max_value=total_pages,
                value=min(int(st.session_state.get("full_feed_page", 1)), total_pages),
                step=1,
                key="full_feed_page",
            )
        )
        start = (page - 1) * page_size
        end = start + page_size
        st.caption(
            f"Найдено сообщений: {format_int(total_found)}. "
            f"Показано: {format_int(start + 1 if total_found else 0)}–{format_int(min(end, total_found))} из {format_int(total_found)}."
        )
        view = view.iloc[start:end].copy()

    render_message_list(view, text_col=text_col, link_col=link_col)
