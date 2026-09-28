# -*- coding: utf-8 -*-
"""Раздел «Источники»: площадки и авторы, которые пишут о бренде.

Главный вопрос заказчика после «что пишут» — «где и кто»: какие площадки дают
основной объём и негатив, кто пишет чаще всех, какие площадки появились
впервые. Площадка — сайт или соцсеть (vk.com, telegram.org), без названий
сообществ и каналов. Расчёт — services.source_stats; здесь только экран.
"""

from __future__ import annotations

from typing import Callable

import altair as alt
import pandas as pd
import streamlit as st

from messages_ui import open_messages_for_platform, render_message_list
from metric_cards_ui import metric_card, render_metric_row
from services.message_compute import message_link_column, message_text_column
from services.metrics_compute import (
    NO_SENTIMENT_REASON,
    format_int,
    has_sentiment_markup,
)
from services.period_comparison import (
    ordered_period_ids,
    period_row_label,
    previous_period_id,
    selected_period_rows,
)
from services.source_stats import (
    build_author_statistics,
    build_source_statistics,
    display_table,
    messages_of_author,
    messages_of_source,
    new_sources,
)

TABLE_LIMIT = 50
CHART_LIMIT = 10
MESSAGES_SHOWN = 15


def _period_label(periods: pd.DataFrame, period_id: str) -> str:
    rows = selected_period_rows(periods, [period_id])
    if rows.empty:
        return str(period_id)
    return period_row_label(rows.iloc[0], str(period_id))


def comparison_basis(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    load_period_messages: Callable[[str], pd.DataFrame] | None,
) -> tuple[pd.DataFrame, pd.DataFrame | None, str, str]:
    """С чем сравнивать для «новых площадок».

    Выбрано несколько периодов — последний с предыдущим из выбранных. Выбран
    один — с периодом перед ним, если он есть. Возвращает (текущие
    сообщения, прошлые или None, подпись текущего, подпись прошлого).
    """
    ordered = ordered_period_ids(periods, period_ids)
    has_period = isinstance(messages, pd.DataFrame) and "period_id" in messages.columns
    if len(ordered) >= 2 and has_period:
        current_id, previous_id = ordered[-1], ordered[-2]
        by_period = messages["period_id"].astype(str)
        return (
            messages[by_period == str(current_id)],
            messages[by_period == str(previous_id)],
            _period_label(periods, current_id),
            _period_label(periods, previous_id),
        )
    previous_id = previous_period_id(periods, period_ids)
    current_label = _period_label(periods, ordered[-1]) if ordered else ""
    if not previous_id or load_period_messages is None:
        return messages, None, current_label, ""
    try:
        previous = load_period_messages(previous_id)
    except Exception:  # noqa: BLE001 — без прошлого периода раздел работает
        previous = None
    return messages, previous, current_label, _period_label(periods, previous_id)


def _top_chart(stats: pd.DataFrame, marked: bool) -> None:
    top = stats.head(CHART_LIMIT).copy()
    top["Площадка"] = top["label"].astype(str)
    top["Сообщений"] = top["messages"].astype(int)
    top["Доля негатива"] = top["negative_share"].astype(float)
    encoding = {
        "x": alt.X("Сообщений:Q", title="Сообщений"),
        "y": alt.Y(
            "Площадка:N",
            sort=alt.EncodingSortField(field="Сообщений", order="descending"),
            title=None,
            axis=alt.Axis(labelLimit=260),
        ),
        "tooltip": [alt.Tooltip("Площадка:N"), alt.Tooltip("Сообщений:Q", format=",")],
    }
    if marked:
        # Та же шкала, что у графика инфоповодов: доля негатива 0..1 одним
        # оттенком — одинаковая доля в разных периодах одного цвета.
        encoding["color"] = alt.Color(
            "Доля негатива:Q",
            title="Доля негатива",
            scale=alt.Scale(scheme="reds", domain=[0, 1]),
            legend=alt.Legend(format=".0%"),
        )
        encoding["tooltip"].append(alt.Tooltip("Доля негатива:Q", format=".0%"))
    chart = alt.Chart(top).mark_bar().encode(**encoding).properties(height=alt.Step(26))
    st.altair_chart(chart, width="stretch")


def _selected_row(event) -> int | None:
    rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
    return int(rows[0]) if rows else None


def _render_messages(subset: pd.DataFrame) -> None:
    if subset is None or subset.empty:
        st.info("Сообщений нет.")
        return
    order = [c for c in ("_engagement", "_reach", "_audience") if c in subset.columns]
    view = subset.sort_values(order, ascending=False) if order else subset
    st.caption(
        f"Показаны {min(MESSAGES_SHOWN, len(view))} из {format_int(len(view))} — "
        "сначала с наибольшей вовлечённостью."
    )
    render_message_list(
        view.head(MESSAGES_SHOWN),
        text_col=message_text_column(view),
        link_col=message_link_column(view),
    )


def render_sources_page(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    *,
    load_period_messages: Callable[[str], pd.DataFrame] | None = None,
    project_id: str | None = None,
) -> None:
    st.subheader("Источники и авторы")
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        st.info("За выбранный период сообщений нет.")
        return
    sources = build_source_statistics(messages)
    authors = build_author_statistics(messages)
    marked = has_sentiment_markup(messages)
    current, previous, current_label, previous_label = comparison_basis(
        messages, periods, period_ids, load_period_messages
    )
    fresh = new_sources(current, previous) if isinstance(previous, pd.DataFrame) and not previous.empty else None

    total = int(len(messages))
    leader = sources.iloc[0] if not sources.empty else None
    leader_share = int(leader["messages"]) / total if leader is not None and total else 0.0
    render_metric_row(
        [
            metric_card("Площадок", format_int(len(sources))),
            metric_card("Авторов", format_int(len(authors))),
            (
                metric_card(
                    "Новых площадок",
                    format_int(len(fresh)),
                    help_text=f"Есть в «{current_label}», не было в «{previous_label}».",
                )
                if fresh is not None
                else metric_card("Новых площадок", "—", help_text="Нет прошлого периода для сравнения.")
            ),
            (
                metric_card(
                    "Главная площадка",
                    str(leader["label"]),
                    help_text=f"{leader_share * 100:.0f}% сообщений выборки "
                    f"({format_int(int(leader['messages']))} из {format_int(total)}).",
                )
                if leader is not None
                else metric_card("Главная площадка", "—")
            ),
        ],
        columns=4,
    )
    if not marked:
        st.caption(f"Негатив по площадкам и авторам не показан. {NO_SENTIMENT_REASON}")

    tab_sources, tab_authors, tab_new = st.tabs(["Площадки", "Авторы", "Новые площадки"])
    with tab_sources:
        _top_chart(sources, marked)
        shown = sources.head(TABLE_LIMIT)
        st.caption(
            "Площадка — сайт или соцсеть, где опубликовано сообщение: все сообщества "
            "и каналы ВКонтакте — это vk.com, Telegram — telegram.org. Аудитория — "
            "сумма подписчиков сообществ площадки, каждое учтено один раз; охват и "
            "вовлечённость — сумма по сообщениям. Выберите строку, чтобы открыть "
            "сообщения площадки."
        )
        event = st.dataframe(
            display_table(shown, messages, kind="sources"),
            hide_index=True,
            width="stretch",
            selection_mode="single-row",
            on_select="rerun",
            key="sources_table",
        )
        position = _selected_row(event)
        if position is not None and position < len(shown):
            row = shown.iloc[position]
            head, action = st.columns([3, 2], vertical_alignment="center")
            with head:
                st.markdown(f"#### {row['label']}")
            with action:
                # Здесь — 15 самых заметных сообщений; вся лента площадки с
                # поиском, фильтрами и выгрузкой в Excel — в «Сообщениях».
                if st.button(
                    f"Все {format_int(int(row['messages']))} сообщ. в «Сообщениях»",
                    key=f"open_platform_messages_{row['_key']}",
                    width="stretch",
                ):
                    open_messages_for_platform(project_id, str(row["label"]))
                    st.rerun()
            _render_messages(messages_of_source(messages, row["_key"]))
    with tab_authors:
        shown_authors = authors.head(TABLE_LIMIT)
        st.caption("Выберите строку, чтобы открыть сообщения автора.")
        event = st.dataframe(
            display_table(shown_authors, messages, kind="authors"),
            hide_index=True,
            width="stretch",
            selection_mode="single-row",
            on_select="rerun",
            key="authors_table",
        )
        position = _selected_row(event)
        if position is not None and position < len(shown_authors):
            row = shown_authors.iloc[position]
            st.markdown(f"#### {row['label']}")
            _render_messages(messages_of_author(messages, row["_key"]))
    with tab_new:
        if fresh is None:
            st.info(
                "Сравнивать не с чем: нет прошлого периода. Выберите два периода или "
                "загрузите предыдущую выгрузку."
            )
        elif fresh.empty:
            st.info(f"Все площадки «{current_label}» уже были в «{previous_label}».")
        else:
            st.caption(f"Площадки «{current_label}», которых не было в «{previous_label}».")
            st.dataframe(
                display_table(fresh.head(TABLE_LIMIT), current, kind="sources"),
                hide_index=True,
                width="stretch",
            )
