# -*- coding: utf-8 -*-
"""Раздел «Теги»: статистика по тегам и карточка выбранного тега.

Теги берутся из системных колонок Brand Analytics после «Обработано»
(services.tag_compute). Выбор строки в таблице статистики открывает карточку
тега с метриками, ключевыми сообщениями и полной лентой — по аналогии с
карточкой выбранного инфоповода.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from metric_cards_ui import metric_card, render_metric_row

from messages_ui import render_message_list
from services.message_compute import message_link_column, message_text_column
from services.metrics_compute import format_int, numeric_series, overview_metrics, percent_text
from services.tag_compute import build_tag_statistics, split_pipe_values


def messages_with_tag(messages: pd.DataFrame, tag: str) -> pd.DataFrame:
    if (
        messages is None
        or messages.empty
        or "tags" not in messages.columns
        or not str(tag).strip()
    ):
        return pd.DataFrame()
    key = str(tag).strip().lower().replace("ё", "е")
    mask = (
        messages["tags"]
        .fillna("")
        .astype(str)
        .apply(
            lambda value: key
            in {item.lower().replace("ё", "е") for item in split_pipe_values(value)}
        )
    )
    return messages[mask].copy()


def _tag_auto_summary(tag: str, tag_messages: pd.DataFrame) -> str:
    if tag_messages is None or tag_messages.empty:
        return f"По тегу «{tag}» сообщения не найдены."
    metrics = overview_metrics(tag_messages)
    sent = metrics.get("sentiment", {}) or {}
    total = int(sent.get("total", metrics.get("messages", 0)) or 0)
    negative = int(sent.get("negative", 0) or 0)
    event_col = next(
        (
            col
            for col in ["event_title", "source_main_topic", "Сюжет"]
            if col in tag_messages.columns
        ),
        None,
    )
    event_part = ""
    if event_col:
        top_events = (
            tag_messages[event_col]
            .fillna("")
            .astype(str)
            .str.strip()
            .replace("", pd.NA)
            .dropna()
            .value_counts()
            .head(5)
            .index.tolist()
        )
        if top_events:
            event_part = (
                " Основные связанные инфоповоды: "
                + "; ".join(map(str, top_events))
                + "."
            )
    return (
        f"По тегу «{tag}» найдено {format_int(total)} сообщений. "
        f"Негативных сообщений: {format_int(negative)} ({percent_text(negative, total)}). "
        f"Суммарный охват — {format_int(metrics.get('reach', 0))}, "
        f"вовлеченность — {format_int(metrics.get('engagement', 0))}."
        f"{event_part}"
    )


def render_selected_tag_detail(
    project_id: str, selected_tag: str, messages: pd.DataFrame
) -> None:
    """Render selected-tag card with metrics and messages, like selected event card."""
    tag_messages = messages_with_tag(messages, selected_tag)
    st.markdown(f"## Тег: {selected_tag}")
    st.info(_tag_auto_summary(selected_tag, tag_messages))

    if tag_messages is None or tag_messages.empty:
        st.info("Сообщений по выбранному тегу не найдено.")
        return

    metrics = overview_metrics(tag_messages)
    sent = metrics.get("sentiment", {}) or {}
    total = int(sent.get("total", metrics.get("messages", 0)) or 0)

    source_count = 0
    for col in ["chat_title", "platform", "source", "Источник", "Место публикации"]:
        if col in tag_messages.columns:
            source_count = int(
                tag_messages[col]
                .fillna("")
                .astype(str)
                .replace("", pd.NA)
                .dropna()
                .nunique()
            )
            break
    author_count = 0
    for col in ["author", "Автор"]:
        if col in tag_messages.columns:
            author_count = int(
                tag_messages[col]
                .fillna("")
                .astype(str)
                .replace("", pd.NA)
                .dropna()
                .nunique()
            )
            break

    render_metric_row(
        [
            metric_card("Сообщений", format_int(metrics.get("messages", 0))),
            metric_card("Источников/чатов", format_int(source_count)),
            metric_card("Авторов", format_int(author_count)),
            metric_card(
                "Негатив", percent_text(int(sent.get("negative", 0) or 0), total)
            ),
            metric_card("Аудитория", format_int(metrics.get("audience", 0))),
            metric_card("Охват", format_int(metrics.get("reach", 0))),
            metric_card("Вовлеченность", format_int(metrics.get("engagement", 0))),
        ]
    )

    mode = st.radio(
        "Сообщения тега",
        ["Ключевые сообщения", "Вся лента"],
        horizontal=True,
        key=f"selected_tag_messages_mode_{project_id}_{abs(hash(selected_tag))}",
    )

    work = tag_messages.copy()
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
        st.caption(
            "Показаны топ-15 сообщений выбранного тега по вовлеченности. Если вовлеченность равна 0, учитываются охват и аудитория."
        )
        view = (
            work.sort_values(["_engagement", "_reach", "_audience"], ascending=False)
            .head(15)
            .copy()
        )
    else:
        search_key = f"selected_tag_feed_search_{project_id}_{abs(hash(selected_tag))}"
        search = st.text_input(
            "Поиск по ленте тега", placeholder="Введите слово или фразу", key=search_key
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
                key=f"selected_tag_feed_page_size_{project_id}_{abs(hash(selected_tag))}",
            )
        )
        total_pages = max(1, (total_found + page_size - 1) // page_size)
        page = int(
            st.number_input(
                "Страница",
                min_value=1,
                max_value=total_pages,
                value=1,
                step=1,
                key=f"selected_tag_feed_page_{project_id}_{abs(hash(selected_tag))}",
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


def render_tag_statistics(
    messages: pd.DataFrame, *, project_id: str | None = None
) -> None:
    stats = build_tag_statistics(messages)
    if stats.empty:
        st.info("Теги не найдены.")
        return

    st.subheader("Статистика тегов")
    top = stats.head(30).copy()
    display = top.copy()
    for col in ["Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"]:
        if col in display.columns:
            display[col] = display[col].apply(format_int)
    display["Доля негатива"] = display["Доля негатива"].astype(str) + "%"
    st.caption(
        "Теги берутся из системных колонок Brand Analytics после «Обработано». Аудитория, охват и вовлеченность суммируются по сообщениям с выбранным тегом."
    )
    tag_selection = st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        selection_mode="single-row",
        on_select="rerun",
        key=f"tag_statistics_table_{project_id or 'global'}",
    )
    rows = (
        getattr(tag_selection, "selection", {}).get("rows", [])
        if tag_selection is not None
        else []
    )
    if not rows:
        st.caption(
            "Выберите строку тега, чтобы открыть метрики, ключевые сообщения и всю ленту сообщений по этому тегу."
        )
        return
    selected_tag = str(top.iloc[rows[0]].get("Тег") or "").strip()
    if selected_tag:
        render_selected_tag_detail(project_id or "global", selected_tag, messages)
