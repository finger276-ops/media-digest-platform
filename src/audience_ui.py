# -*- coding: utf-8 -*-
"""Раздел «Аудитория»: пол, возраст и география авторов.

Расчёт — services.audience; здесь только экран. Каждая цифра идёт с
покрытием: пол и возраст в выгрузках известны далеко не у всех авторов, и
«70 % мужчин» без «пол известен у 22 % авторов» вводило бы заказчика в
заблуждение.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from metric_cards_ui import metric_card, render_metric_row
from services.audience import audience_summary, mentions_text, percent, places_count_text
from services.chart_style import CATEGORICAL_PALETTE
from services.metrics_compute import format_int

NO_DATA_TEXT = (
    "В выгрузке нет данных об авторах. Пол, возраст и география приходят в "
    "выгрузках Brand Analytics — колонки «Пол», «Возраст», «Регион» и «Город»."
)
BAR_COLOR = CATEGORICAL_PALETTE[0]


def _bars(rows: list[dict], *, label: str, value: str, value_title: str, text: str, sort: bool) -> None:
    frame = pd.DataFrame(rows)
    y = alt.Y(
        f"{label}:N",
        title=None,
        sort=alt.EncodingSortField(field=value, order="descending") if sort else None,
        axis=alt.Axis(labelLimit=220),
    )
    base = alt.Chart(frame).encode(
        y=y,
        x=alt.X(f"{value}:Q", title=value_title),
        tooltip=[alt.Tooltip(f"{label}:N"), alt.Tooltip(f"{value}:Q", title=value_title, format=","),
                 alt.Tooltip(f"{text}:N", title="Доля")],
    )
    chart = base.mark_bar(color=BAR_COLOR) + base.mark_text(align="left", dx=4).encode(text=f"{text}:N")
    st.altair_chart(chart.properties(height=alt.Step(30)), width="stretch")


def _group_chart(block: dict, *, title: str, what: str) -> None:
    st.markdown(f"**{title}**")
    if not block.get("known"):
        st.caption(f"{what} авторов в выгрузке не указан.")
        return
    rows = [
        {"Группа": item["name"], "Авторов": item["authors"], "Доля": percent(item["share"])}
        for item in block["groups"]
    ]
    _bars(rows, label="Группа", value="Авторов", value_title="Авторов", text="Доля", sort=False)
    st.caption(
        f"Среди {format_int(block['known'])} авторов, у кого {what.lower()} указан "
        f"({percent(block['share_known'])} всех авторов)."
    )


def _place_chart(places: list[dict], *, title: str, what: str) -> None:
    st.markdown(f"**{title}**")
    if not places:
        st.caption(f"{what} авторов в выгрузке не указаны.")
        return
    rows = [{"Место": item["name"], "Упоминаний": item["messages"], "Доля": percent(item["share"])} for item in places]
    _bars(rows, label="Место", value="Упоминаний", value_title="Упоминаний", text="Доля", sort=True)


def render_audience_page(messages: pd.DataFrame) -> None:
    st.subheader("Аудитория: кто пишет")
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        st.info("За выбранный период сообщений нет.")
        return
    summary = audience_summary(messages)
    if not summary["has_data"]:
        st.info(NO_DATA_TEXT)
        return
    gender, age, geo = summary["gender"], summary["age"], summary["geo"]
    render_metric_row(
        [
            metric_card("Авторов", format_int(summary["authors"]),
                        help_text="Уникальные авторы выборки; сообщение без автора считается отдельным автором."),
            metric_card("Пол известен", percent(gender["share_known"]),
                        help_text=f"У {format_int(gender['known'])} из {format_int(summary['authors'])} авторов."),
            metric_card("Возраст известен", percent(age["share_known"]),
                        help_text=f"У {format_int(age['known'])} из {format_int(summary['authors'])} авторов."),
            metric_card("Место известно", percent(geo["share_known"]),
                        help_text=f"Регион или город указан у {format_int(geo['known'])} из "
                        f"{format_int(summary['authors'])} авторов."),
        ],
        columns=4,
    )
    st.caption(
        "Пол и возраст — доли среди авторов, у кого они указаны; каждый автор учтён "
        "один раз, сколько бы он ни написал. Данные — из выгрузки, платформа их не "
        "угадывает."
    )
    left, right = st.columns(2)
    with left:
        _group_chart(gender, title="Пол", what="Пол")
    with right:
        _group_chart(age, title="Возраст", what="Возраст")

    st.markdown("#### География")
    if geo["known"]:
        st.caption(
            f"Упоминания от авторов из {places_count_text(geo)}. Показано, сколько раз о "
            f"бренде написали из места; учтено {mentions_text(geo['messages_known'])}, где "
            "место автора известно."
        )
    left, right = st.columns(2)
    with left:
        _place_chart(geo["regions"], title="Регионы", what="Регионы")
    with right:
        _place_chart(geo["cities"], title="Города", what="Города")
