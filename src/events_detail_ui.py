# -*- coding: utf-8 -*-
"""«Инфоповоды»: карточка выбранного инфоповода — метрики, теги,
авто-описание и сообщения.

Вынесено из events_ui; events_ui реэкспортирует отсюда прежние имена.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from metric_cards_ui import metric_card, render_metric_row

from services.event_filter_state import (
    event_series_filter,
    filter_messages_by_selected_event,
)
from services.message_compute import message_link_column, message_text_column
from services.metrics_compute import (
    NO_METRIC_REASON,
    NO_SENTIMENT_REASON,
    format_int,
    metric_missing,
    metric_text,
    numeric_series,
    overview_metrics,
    percent_text,
    sentiment_counts,
    sentiment_unmarked,
)
from services.tag_compute import split_pipe_values
from services.tag_parsing import PLATFORM_RUBRIC_TAG_KEYS
from messages_ui import render_message_list


def _event_tags_text(selected: pd.Series, event_messages: pd.DataFrame) -> str:
    values: list[str] = []
    for col in ["tags", "main_tags", "display_tags", "source_topics"]:
        if col in selected.index:
            values.extend(split_pipe_values(str(selected.get(col) or "")))
    if (
        not values
        and isinstance(event_messages, pd.DataFrame)
        and not event_messages.empty
        and "tags" in event_messages.columns
    ):
        for item in event_messages["tags"].fillna("").astype(str).head(500).tolist():
            values.extend(split_pipe_values(item))
    seen: set[str] = set()
    clean: list[str] = []
    for value in values:
        key = value.strip().lower().replace("ё", "е")
        # main_tags инфоповода хранят и рубрики платформы — клиенту их не
        # показываем, как и в тегах сообщений.
        if key in PLATFORM_RUBRIC_TAG_KEYS:
            continue
        if value.strip() and key not in seen:
            seen.add(key)
            clean.append(value.strip())
    return ", ".join(clean[:12])


def _event_auto_summary(selected: pd.Series, event_messages: pd.DataFrame) -> str:
    description = str(
        selected.get("description")
        or selected.get("display_description")
        or selected.get("summary")
        or ""
    ).strip()
    if description:
        return description
    title = str(
        selected.get("title") or selected.get("event_title") or "инфоповод"
    ).strip()
    count = (
        int(len(event_messages))
        if isinstance(event_messages, pd.DataFrame)
        else int(selected.get("message_count", 0) or 0)
    )
    negative_count = 0
    if isinstance(event_messages, pd.DataFrame) and not event_messages.empty:
        sent = sentiment_counts(event_messages)
        if sentiment_unmarked(sent):
            return f"В теме «{title}» собрано {format_int(count)} сообщений."
        negative_count = int(sent.get("negative", 0) or 0)
    return f"В теме «{title}» собрано {format_int(count)} сообщений. Негативных сообщений: {format_int(negative_count)}."


def render_selected_event_detail(
    project_id: str, selected: pd.Series, messages: pd.DataFrame
) -> None:
    """Render a unified selected-infopoint card across all project profiles."""
    event_filter = event_series_filter(selected)
    event_messages = filter_messages_by_selected_event(messages, event_filter)
    title = str(
        selected.get("title") or selected.get("event_title") or "Выбранный инфоповод"
    )
    summary = _event_auto_summary(selected, event_messages)
    tags_text = _event_tags_text(selected, event_messages)

    st.markdown(f"## {title}")
    if summary:
        st.info(summary)

    metrics = overview_metrics(
        event_messages if isinstance(event_messages, pd.DataFrame) else pd.DataFrame()
    )
    sent = metrics.get("sentiment", {}) or {}
    total = int(sent.get("total", metrics.get("messages", 0)) or 0)
    chat_count = int(selected.get("chat_count", 0) or 0)
    if (
        not chat_count
        and isinstance(event_messages, pd.DataFrame)
        and not event_messages.empty
    ):
        for col in ["chat_title", "platform", "source", "Источник", "Место публикации"]:
            if col in event_messages.columns:
                chat_count = int(
                    event_messages[col]
                    .fillna("")
                    .astype(str)
                    .replace("", pd.NA)
                    .dropna()
                    .nunique()
                )
                break
    author_count = 0
    if isinstance(event_messages, pd.DataFrame) and not event_messages.empty:
        for col in ["author", "Автор"]:
            if col in event_messages.columns:
                author_count = int(
                    event_messages[col]
                    .fillna("")
                    .astype(str)
                    .replace("", pd.NA)
                    .dropna()
                    .nunique()
                )
                break

    # Карточки в рамках, как в шапке «Обзора»: одинаковые числа должны и
    # выглядеть одинаково, иначе показатели инфоповода читаются как подпись к
    # заголовку, а не как самостоятельная сводка.
    render_metric_row(
        [
            metric_card("Сообщений", format_int(metrics.get("messages", 0))),
            metric_card("Источников/чатов", format_int(chat_count)),
            metric_card("Авторов", format_int(author_count)),
            (
                metric_card("Негатив", "—", help_text=NO_SENTIMENT_REASON)
                if sentiment_unmarked(sent)
                else metric_card(
                    "Негатив", percent_text(int(sent.get("negative", 0) or 0), total)
                )
            ),
            metric_card(
                "Важность",
                str(round(float(selected.get("importance_score", 0) or 0), 2)),
            ),
            *[
                metric_card(
                    label,
                    metric_text(metrics, key),
                    help_text=NO_METRIC_REASON[key] if metric_missing(metrics, key) else "",
                )
                for label, key in (
                    ("Аудитория", "audience"),
                    ("Охват", "reach"),
                    ("Вовлеченность", "engagement"),
                )
            ],
        ]
    )

    if tags_text:
        st.caption(f"Теги: {tags_text}")
    if isinstance(event_messages, pd.DataFrame):
        st.caption(
            f"В выбранном инфоповоде найдено сообщений: {format_int(len(event_messages))}."
        )

    mode = st.radio(
        "Сообщения инфоповода",
        ["Ключевые сообщения", "Вся лента"],
        horizontal=True,
        key=f"selected_event_messages_mode_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}",
    )

    if event_messages is None or event_messages.empty:
        st.info("Сообщений по выбранному инфоповоду не найдено.")
        return

    work = event_messages.copy()
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
            "Показаны топ-15 сообщений выбранного инфоповода по вовлеченности. Если вовлеченность равна 0, учитываются охват и аудитория."
        )
        view = (
            work.sort_values(["_engagement", "_reach", "_audience"], ascending=False)
            .head(15)
            .copy()
        )
    else:
        search_key = f"selected_event_feed_search_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}"
        search = st.text_input(
            "Поиск по ленте инфоповода",
            placeholder="Введите слово или фразу",
            key=search_key,
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
                key=f"selected_event_feed_page_size_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}",
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
                key=f"selected_event_feed_page_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}",
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
