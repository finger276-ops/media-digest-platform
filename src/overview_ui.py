# -*- coding: utf-8 -*-
"""Раздел «Обзор»: шапка проекта, метрики периода, последовательное сравнение
периодов (карточки, графики, сравнительная таблица).

render_project_intro — единый верхний блок для всех профилей проекта:
агрегаты за выбранные периоды плюс (если выбрано 2+ периодов) цепочка
последовательного сравнения через render_period_comparison_metrics.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from services.dashboard_config import COMPARISON_CHART_BLOCKS, DEFAULT_DASHBOARD_VIEW_SETTINGS
from services.metrics_compute import format_int, overview_metrics, percent_text
from services.period_comparison import (
    COMPARISON_TABLE_VIEWS,
    build_comparison_metrics,
    build_comparison_table,
    chart_number_label,
    comparison_visual_rows,
    metric_delta,
    pp_delta,
    selected_period_label,
)
from services.project_settings import (
    chart_label_settings_from_project_settings,
    chart_label_text_kwargs,
)
from services.chart_style import (
    LINE_INTERPOLATE,
    PERIOD_AXIS,
    fixed_color_scale,
)

SENTIMENT_COLOR_DOMAIN = ["Позитив", "Нейтрал", "Негатив"]
SENTIMENT_COLOR_RANGE = ["#2ca02c", "#9e9e9e", "#d62728"]

# Порядок метрик закреплён здесь же, где строится их цветовая шкала — тот же
# порядок, что в metrics_cols ниже, чтобы цвет метрики не зависел от того, в
# каком графике она сейчас нарисована.
MAIN_METRICS_COLOR_SCALE = fixed_color_scale(
    ["Сообщения", "Аудитория", "Охват", "Вовлеченность"]
)


def _render_sentiment_donut(
    period_label: str,
    sentiment: dict[str, Any],
    *,
    key: str,
    label_settings: dict[str, Any] | None = None,
) -> None:
    total = int((sentiment or {}).get("total", 0) or 0)
    if total <= 0:
        st.caption(f"{period_label}: нет данных для круговой диаграммы")
        return
    pie = pd.DataFrame(
        [
            {
                "Тональность": "Позитив",
                "Сообщений": int((sentiment or {}).get("positive", 0) or 0),
            },
            {
                "Тональность": "Нейтрал",
                "Сообщений": int((sentiment or {}).get("neutral", 0) or 0),
            },
            {
                "Тональность": "Негатив",
                "Сообщений": int((sentiment or {}).get("negative", 0) or 0),
            },
        ]
    )
    pie = pie[pie["Сообщений"] > 0]
    if pie.empty:
        st.caption(f"{period_label}: нет данных для круговой диаграммы")
        return
    pie = pie.copy()
    pie["Доля"] = pie["Сообщений"] / total * 100
    pie["Подпись"] = pie.apply(
        lambda r: f"{r['Тональность']}: {chart_number_label(r['Доля'], percent=True)}",
        axis=1,
    )
    color_map = dict(zip(SENTIMENT_COLOR_DOMAIN, SENTIMENT_COLOR_RANGE))
    pie["Цвет"] = pie["Тональность"].map(color_map).fillna("#999999")

    # Чтобы подписи не накладывались друг на друга, для круговой диаграммы показываем
    # сам donut отдельно, а значения — списком рядом с диаграммой.
    # Легенда по умолчанию скрыта, потому что справа уже есть блок значений.
    donut_cfg = chart_label_settings_from_project_settings(
        {"chart_label_settings": label_settings or {}}
    )
    donut_legend = (
        alt.Legend(title="Тональность") if donut_cfg.get("show_donut_legend") else None
    )
    base = alt.Chart(pie).encode(
        theta=alt.Theta(field="Сообщений", type="quantitative"),
        color=alt.Color(
            field="Тональность",
            type="nominal",
            scale=alt.Scale(domain=SENTIMENT_COLOR_DOMAIN, range=SENTIMENT_COLOR_RANGE),
            legend=donut_legend,
        ),
        tooltip=[
            "Тональность",
            alt.Tooltip("Сообщений:Q", format=","),
            alt.Tooltip("Доля:Q", format=".1f", title="Доля, %"),
        ],
    )
    arcs = base.mark_arc(innerRadius=50)

    left, right = st.columns([3, 2])
    with left:
        st.altair_chart(
            arcs.properties(height=260, title=period_label), width="stretch"
        )
    with right:
        st.markdown("**Значения**")
        for _, row in pie.sort_values("Сообщений", ascending=False).iterrows():
            tone = str(row.get("Тональность", ""))
            color = str(row.get("Цвет", "#999999"))
            count = format_int(row.get("Сообщений", 0))
            share = chart_number_label(row.get("Доля", 0), percent=True)
            st.markdown(
                f"<div style='margin: 0 0 8px 0; line-height:1.35'>"
                f"<span style='color:{color}; font-size:18px;'>●</span> "
                f"<span style='font-weight:600'>{tone}</span><br>"
                f"<span style='color:#666'>Сообщений:</span> {count}<br>"
                f"<span style='color:#666'>Доля:</span> {share}"
                f"</div>",
                unsafe_allow_html=True,
            )


def _render_value_distribution_donut(
    df: pd.DataFrame, label_col: str, value_col: str, title: str
) -> None:
    if (
        df is None
        or df.empty
        or label_col not in df.columns
        or value_col not in df.columns
    ):
        st.info("Нет данных для круговой диаграммы.")
        return
    pie_df = df[[label_col, value_col]].copy()
    pie_df[value_col] = pd.to_numeric(pie_df[value_col], errors="coerce").fillna(0)
    pie_df = pie_df[pie_df[value_col] > 0]
    total_value = float(pie_df[value_col].sum() or 0)
    if pie_df.empty or total_value <= 0:
        st.info("Нет данных для круговой диаграммы.")
        return
    pie_df["Доля"] = pie_df[value_col] / total_value * 100
    donut = (
        alt.Chart(pie_df)
        .mark_arc(innerRadius=50)
        .encode(
            theta=alt.Theta(field=value_col, type="quantitative"),
            color=alt.Color(f"{label_col}:N", legend=None),
            tooltip=[
                label_col,
                alt.Tooltip(f"{value_col}:Q", format=","),
                alt.Tooltip("Доля:Q", format=".1f", title="Доля, %"),
            ],
        )
    )
    left, right = st.columns([3, 2])
    with left:
        st.altair_chart(
            donut.properties(height=300, title=title), width="stretch"
        )
    with right:
        st.markdown("**Значения**")
        for _, row in pie_df.sort_values(value_col, ascending=False).iterrows():
            st.markdown(
                f"**{row[label_col]}**  \n"
                f"{chart_number_label(row[value_col])} · {chart_number_label(row['Доля'], percent=True)}"
            )


def render_period_comparison_charts(
    comparison: list[dict[str, Any]],
    *,
    label_settings: dict[str, Any] | None = None,
    visible_blocks_default: list[str] | None = None,
) -> None:
    if not comparison:
        return
    chart_df = comparison_visual_rows(comparison)
    if chart_df.empty:
        return

    chart_blocks = list(COMPARISON_CHART_BLOCKS)
    default_blocks = [
        x
        for x in (
            visible_blocks_default
            or DEFAULT_DASHBOARD_VIEW_SETTINGS["comparison_visible_charts"]
        )
        if x in chart_blocks
    ]
    chart_key = abs(hash(tuple(chart_df["Период"].astype(str).tolist())))

    # Настройки графиков живут в одной панели, а не тремя контролами в потоке
    # страницы: сначала данные, управление — по требованию.
    head_left, head_right = st.columns([5, 1])
    with head_left:
        st.markdown("**Графики динамики**")
    with head_right:
        settings_box = (
            st.popover("⚙️ Графики", width="stretch")
            if hasattr(st, "popover")
            else st.expander("⚙️ Графики")
        )
    with settings_box:
        selected_blocks = st.multiselect(
            "Показывать",
            chart_blocks,
            default=default_blocks
            or DEFAULT_DASHBOARD_VIEW_SETTINGS["comparison_visible_charts"],
            key=f"comparison_visible_charts_{chart_key}",
            help="Скрытые графики не рендерятся и не нагружают страницу.",
        )
        chart_type = st.selectbox(
            "Вид основных метрик",
            ["График", "Столбчатая", "Круговая диаграмма"],
            index=0,
            key=f"main_metrics_chart_type_{chart_key}",
        )
        sentiment_chart_type = st.selectbox(
            "Вид тональности",
            ["График", "Столбчатая", "Круговая диаграмма"],
            index=0,
            key=f"sentiment_chart_type_{chart_key}",
        )

    if not selected_blocks:
        st.info("Все графики скрыты. Включите нужные в панели «Графики».")
        return

    metrics_cols = ["Сообщения", "Аудитория", "Охват", "Вовлеченность"]

    if "Динамика основных метрик" in selected_blocks:
        st.markdown("**Динамика основных метрик**")
        metrics_long = chart_df[["Период"] + metrics_cols].melt(
            id_vars="Период",
            var_name="Метрика",
            value_name="Значение",
        )
        metrics_long["Подпись"] = metrics_long["Значение"].apply(chart_number_label)
        base_metrics = alt.Chart(metrics_long).encode(
            x=alt.X(
                "Период:N",
                sort=None,
                title="Период",
                axis=PERIOD_AXIS,
            ),
            y=alt.Y("Значение:Q", title="Значение"),
            color=alt.Color(
                "Метрика:N",
                scale=MAIN_METRICS_COLOR_SCALE,
                legend=alt.Legend(title="Метрика"),
            ),
            tooltip=[
                alt.Tooltip("Полный период:N", title="Период"),
                "Метрика",
                alt.Tooltip("Значение:Q", format=","),
            ],
        )
        if chart_type == "Столбчатая":
            bars = (
                alt.Chart(metrics_long)
                .mark_bar(size=18)
                .encode(
                    x=alt.X(
                        "Период:N",
                        sort=None,
                        title="Период",
                        axis=PERIOD_AXIS,
                    ),
                    xOffset=alt.XOffset("Метрика:N"),
                    y=alt.Y("Значение:Q", title="Значение"),
                    color=alt.Color(
                        "Метрика:N",
                        scale=MAIN_METRICS_COLOR_SCALE,
                        legend=alt.Legend(title="Метрика"),
                    ),
                    tooltip=[
                        alt.Tooltip("Полный период:N", title="Период"),
                        "Метрика",
                        alt.Tooltip("Значение:Q", format=","),
                    ],
                )
            )
            st.altair_chart(bars.properties(height=320), width="stretch")
        elif chart_type == "Круговая диаграмма":
            pie_metric = st.selectbox(
                "Метрика для круговой диаграммы",
                metrics_cols,
                index=0,
                key=f"main_metrics_pie_metric_{abs(hash(tuple(chart_df['Период'].tolist())))}",
            )
            pie_df = (
                chart_df[["Период", "Полный период", pie_metric]]
                .rename(columns={pie_metric: "Значение"})
                .copy()
            )
            pie_df["Значение"] = pd.to_numeric(
                pie_df["Значение"], errors="coerce"
            ).fillna(0)
            total_value = float(pie_df["Значение"].sum() or 0)
            if total_value <= 0:
                st.info("Нет данных для круговой диаграммы по выбранной метрике.")
            else:
                pie_df["Доля"] = pie_df["Значение"] / total_value * 100
                pie_df["Подпись"] = pie_df.apply(
                    lambda r: f"{chart_number_label(r['Значение'])} · {chart_number_label(r['Доля'], percent=True)}",
                    axis=1,
                )
                donut = (
                    alt.Chart(pie_df)
                    .mark_arc(innerRadius=50)
                    .encode(
                        theta=alt.Theta(field="Значение", type="quantitative"),
                        color=alt.Color("Период:N", legend=None),
                        tooltip=[
                            alt.Tooltip("Полный период:N", title="Период"),
                            alt.Tooltip("Значение:Q", format=","),
                            alt.Tooltip("Доля:Q", format=".1f", title="Доля, %"),
                        ],
                    )
                )
                left_pie, right_pie = st.columns([3, 2])
                with left_pie:
                    st.altair_chart(
                        donut.properties(height=300, title=pie_metric),
                        width="stretch",
                    )
                with right_pie:
                    st.markdown("**Значения**")
                    for _, row in pie_df.sort_values(
                        "Значение", ascending=False
                    ).iterrows():
                        st.markdown(
                            f"**{row['Период']}**  \n{chart_number_label(row['Значение'])} · {chart_number_label(row['Доля'], percent=True)}"
                        )
        else:
            # Аудитория измеряется миллионами, сообщения — сотнями. На общей оси
            # видна только самая крупная метрика, поэтому каждая получает
            # собственную шкалу и собственную панель.
            st.caption(
                "У каждой метрики своя шкала: на общей оси аудитория в миллионах "
                "полностью скрывала бы сообщения. Значения — при наведении на точки."
            )
            metrics_line = (
                alt.Chart(metrics_long)
                .mark_line(point=True, interpolate=LINE_INTERPOLATE)
                .encode(
                    x=alt.X(
                        "Период:N",
                        sort=None,
                        title=None,
                        axis=PERIOD_AXIS,
                    ),
                    y=alt.Y("Значение:Q", title=None),
                    color=alt.Color(
                        "Метрика:N", scale=MAIN_METRICS_COLOR_SCALE, legend=None
                    ),
                    tooltip=[
                        alt.Tooltip("Полный период:N", title="Период"),
                        "Метрика",
                        alt.Tooltip("Значение:Q", format=","),
                    ],
                )
                .properties(height=190, width=200)
                .facet(
                    facet=alt.Facet("Метрика:N", title=None, sort=metrics_cols),
                    columns=4,
                )
                .resolve_scale(y="independent")
            )
            st.altair_chart(metrics_line, width="stretch")

    if "Динамика тональности" in selected_blocks:
        st.markdown("**Динамика долей тональности, %**")
        sentiment_long = chart_df[
            ["Период", "Позитив, %", "Нейтрал, %", "Негатив, %"]
        ].melt(
            id_vars="Период",
            var_name="Тональность",
            value_name="Доля, %",
        )
        sentiment_long["Тональность"] = sentiment_long["Тональность"].str.replace(
            ", %", "", regex=False
        )
        sentiment_long["Подпись"] = sentiment_long["Доля, %"].apply(
            lambda x: chart_number_label(x, percent=True)
        )
        base_sentiment = alt.Chart(sentiment_long).encode(
            x=alt.X(
                "Период:N",
                sort=None,
                title="Период",
                axis=PERIOD_AXIS,
            ),
            y=alt.Y("Доля, %:Q", title="Доля, %"),
            color=alt.Color(
                "Тональность:N",
                scale=alt.Scale(
                    domain=SENTIMENT_COLOR_DOMAIN, range=SENTIMENT_COLOR_RANGE
                ),
                legend=alt.Legend(title="Тональность"),
            ),
            tooltip=[
                alt.Tooltip("Полный период:N", title="Период"),
                "Тональность",
                alt.Tooltip("Доля, %:Q", format=".1f"),
            ],
        )
        if sentiment_chart_type == "Столбчатая":
            # 100%-накопленный столбец, а не сгруппированные рядом: три доли
            # одного периода в сумме дают целое, и композицию читают именно
            # так — одной полосой, а не тремя соседними разной высоты.
            # stack="normalize" считает пропорции сам, не полагаясь на то,
            # что округлённые проценты дадут ровно 100.
            sentiment_bars = (
                alt.Chart(sentiment_long)
                .mark_bar(size=28)
                .encode(
                    x=alt.X(
                        "Период:N",
                        sort=None,
                        title="Период",
                        axis=PERIOD_AXIS,
                    ),
                    y=alt.Y(
                        "Доля, %:Q",
                        title="Доля, %",
                        stack="normalize",
                        axis=alt.Axis(format="%"),
                    ),
                    order=alt.Order(
                        "Тональность:N",
                        sort="ascending",
                    ),
                    color=alt.Color(
                        "Тональность:N",
                        scale=alt.Scale(
                            domain=SENTIMENT_COLOR_DOMAIN, range=SENTIMENT_COLOR_RANGE
                        ),
                        legend=alt.Legend(title="Тональность"),
                    ),
                    tooltip=[
                        alt.Tooltip("Полный период:N", title="Период"),
                        "Тональность",
                        alt.Tooltip("Доля, %:Q", format=".1f"),
                    ],
                )
            )
            st.altair_chart(
                sentiment_bars.properties(height=320), width="stretch"
            )
        elif sentiment_chart_type == "Круговая диаграмма":
            period_options = [str(x) for x in chart_df["Период"].tolist()]
            selected_period_for_sentiment = st.selectbox(
                "Период для круговой диаграммы тональности",
                period_options,
                index=len(period_options) - 1 if period_options else 0,
                key=f"sentiment_pie_period_{abs(hash(tuple(period_options)))}",
            )
            sentiment_by_label = {
                str(row["Период"]): item.get("sentiment", {})
                for (_, row), item in zip(chart_df.iterrows(), comparison)
            }
            _render_sentiment_donut(
                selected_period_for_sentiment,
                sentiment_by_label.get(selected_period_for_sentiment, {}),
                key="sentiment_selector",
                label_settings=label_settings,
            )
        else:
            sentiment_line = base_sentiment.mark_line(
                point=True, interpolate=LINE_INTERPOLATE
            )
            st.caption(
                "Подписи процентов скрыты, чтобы линии не накладывались. Значения доступны при наведении на точки."
            )
            st.altair_chart(
                sentiment_line.properties(height=320), width="stretch"
            )
            # Нейтрал обычно занимает 90+ процентов и прижимает позитив с
            # негативом к нулю. Негатив — то, за чем следят, поэтому он
            # получает отдельную панель со своей шкалой.
            negative_only = sentiment_long[sentiment_long["Тональность"] == "Негатив"]
            if not negative_only.empty and float(negative_only["Доля, %"].max()) < 25:
                st.caption("Негатив отдельно — на общей шкале его не видно из-за нейтрала.")
                st.altair_chart(
                    alt.Chart(negative_only)
                    .mark_line(
                        point=True,
                        color=SENTIMENT_COLOR_RANGE[2],
                        interpolate=LINE_INTERPOLATE,
                    )
                    .encode(
                        x=alt.X(
                            "Период:N",
                            sort=None,
                            title=None,
                            axis=PERIOD_AXIS,
                        ),
                        y=alt.Y("Доля, %:Q", title="Негатив, %"),
                        tooltip=[
                            alt.Tooltip("Период:N", title="Период"),
                            alt.Tooltip("Доля, %:Q", format=".1f", title="Негатив, %"),
                        ],
                    )
                    .properties(height=170),
                    width="stretch",
                )

    if "Сравнение выбранной метрики" in selected_blocks:
        st.markdown("**Сравнение выбранной метрики по периодам**")
        metric_map = {
            "Сообщения": "Сообщения",
            "Аудитория": "Аудитория",
            "Охват": "Охват",
            "Вовлеченность": "Вовлеченность",
        }
        selected_metric = st.selectbox(
            "Метрика для сравнения",
            list(metric_map.keys()),
            index=0,
            key=f"comparison_metric_{abs(hash(tuple(chart_df['Период'].tolist())))}",
        )
        metric_col = metric_map[selected_metric]
        bar_df = chart_df[["Период", "Полный период", metric_col]].rename(
            columns={metric_col: "Значение"}
        )
        bar_df["Подпись"] = bar_df["Значение"].apply(chart_number_label)
        comparison_chart_type = st.selectbox(
            "Тип визуализации выбранной метрики",
            ["Столбчатая", "График", "Круговая диаграмма"],
            index=0,
            key=f"single_metric_chart_type_{abs(hash(tuple(chart_df['Период'].tolist())))}",
        )
        if comparison_chart_type == "Круговая диаграмма":
            _render_value_distribution_donut(
                bar_df, "Период", "Значение", selected_metric
            )
        else:
            label_cfg = chart_label_settings_from_project_settings(
                {"chart_label_settings": label_settings or {}}
            )
            if label_cfg.get("position") == "center":
                bar_df["_label_y"] = (
                    pd.to_numeric(bar_df["Значение"], errors="coerce").fillna(0) / 2
                )
            elif label_cfg.get("position") == "bottom":
                max_value = float(
                    pd.to_numeric(bar_df["Значение"], errors="coerce").fillna(0).max()
                    or 0
                )
                bar_df["_label_y"] = max_value * 0.03
            else:
                bar_df["_label_y"] = pd.to_numeric(
                    bar_df["Значение"], errors="coerce"
                ).fillna(0)
            bar_base = alt.Chart(bar_df).encode(
                x=alt.X(
                    "Период:N",
                    sort=None,
                    title="Период",
                    axis=PERIOD_AXIS,
                ),
                y=alt.Y("Значение:Q", title=selected_metric),
                tooltip=[
                    alt.Tooltip("Полный период:N", title="Период"),
                    alt.Tooltip("Значение:Q", format=","),
                ],
            )
            if comparison_chart_type == "График":
                line = bar_base.mark_line(point=True, interpolate=LINE_INTERPOLATE)
                line_labels = bar_base.mark_text(
                    **chart_label_text_kwargs(label_settings, chart_type="line")
                ).encode(text="Подпись:N")
                st.altair_chart(
                    (line + line_labels).properties(height=320),
                    width="stretch",
                )
            else:
                bar = bar_base.mark_bar(size=70)
                bar_label_kwargs = chart_label_text_kwargs(
                    label_settings, chart_type="bar"
                )
                if label_cfg.get("position") in {"center", "bottom"}:
                    bar_label_kwargs["dy"] = 0
                    bar_label_kwargs["baseline"] = (
                        "middle" if label_cfg.get("position") == "center" else "bottom"
                    )
                bar_labels = (
                    alt.Chart(bar_df)
                    .mark_text(**bar_label_kwargs)
                    .encode(
                        x=alt.X("Период:N", sort=None),
                        y=alt.Y("_label_y:Q"),
                        text="Подпись:N",
                    )
                )
                st.altair_chart(
                    (bar + bar_labels).properties(height=320), width="stretch"
                )

    if "Круговые диаграммы тональности" in selected_blocks and len(comparison) >= 2:
        st.markdown("**Круговые диаграммы тональности**")
        unique_periods: list[dict[str, Any]] = []
        seen_periods: set[str] = set()
        for item in comparison:
            key = str(item.get("period_id") or item.get("label") or "").strip()
            if not key:
                key = str(len(unique_periods))
            if key in seen_periods:
                continue
            seen_periods.add(key)
            unique_periods.append(item)

        for row_start in range(0, len(unique_periods), 2):
            cols = st.columns(2)
            for offset, item in enumerate(unique_periods[row_start : row_start + 2]):
                with cols[offset]:
                    donut_key = str(
                        item.get("period_id")
                        or item.get("label")
                        or f"period_{row_start + offset}"
                    )
                    _render_sentiment_donut(
                        str(item.get("label", "Период")),
                        item.get("sentiment", {}),
                        key=f"sentiment_donut_{abs(hash(donut_key))}",
                        label_settings=label_settings,
                    )


def render_period_comparison_metrics(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    *,
    chart_label_settings: dict[str, Any] | None = None,
    comparison_visible_charts: list[str] | None = None,
) -> dict[str, Any] | None:
    """Render sequential comparison when two or more periods are selected."""
    aggregate_metrics = build_comparison_metrics(messages, periods, period_ids)
    if aggregate_metrics is None:
        return None
    comparison = aggregate_metrics["comparison_sequence"]
    previous = aggregate_metrics["comparison"]["previous"]
    current = aggregate_metrics["comparison"]["current"]
    first = aggregate_metrics["comparison"]["first"]
    last = aggregate_metrics["comparison"]["last"]
    st.subheader("Сравнение периодов")
    st.caption(
        "Сравнение идет цепочкой по хронологии: "
        + " → ".join(
            item.get("label", item.get("period_id", "")) for item in comparison
        )
    )

    st.markdown(
        f"**{current['label']}** — к предыдущему периоду: {previous['label']}"
    )
    volume = [
        ("Сообщений", "messages"),
        ("Аудитория", "audience"),
        ("Охват", "reach"),
        ("Вовлеченность", "engagement"),
    ]
    for column, (label, key) in zip(st.columns(4), volume):
        with column, st.container(border=True):
            st.metric(
                label,
                format_int(current[key]),
                delta=metric_delta(current[key], previous[key]),
            )

    tone = [("Позитив", "positive"), ("Нейтрал", "neutral"), ("Негатив", "negative")]
    for column, (label, key) in zip(st.columns(3), tone):
        with column, st.container(border=True):
            st.metric(
                label,
                f"{current[f'{key}_share'] * 100:.0f}%",
                delta=pp_delta(current[f"{key}_share"], previous[f"{key}_share"]),
                help=f"{format_int(current['sentiment'].get(key, 0))} сообщений в последнем периоде",
            )

    render_period_comparison_charts(
        comparison,
        label_settings=chart_label_settings,
        visible_blocks_default=comparison_visible_charts,
    )

    st.markdown("**Сравнительная таблица**")
    view = st.radio(
        "Показатель",
        list(COMPARISON_TABLE_VIEWS.keys()),
        index=0,
        horizontal=True,
        key=f"comparison_table_view_{abs(hash(tuple(item.get('period_id', '') for item in comparison)))}",
        label_visibility="collapsed",
    )
    st.dataframe(
        build_comparison_table(comparison, view),
        hide_index=True,
        width="stretch",
    )
    if view == "Все показатели":
        st.caption(
            "Полная таблица шире экрана — её можно прокрутить вбок или выбрать "
            "отдельный показатель."
        )

    if len(comparison) > 2:
        st.caption(
            f"Итоговая динамика от первого к последнему периоду: "
            f"сообщения — {metric_delta(last['messages'], first['messages'])}; "
            f"аудитория — {metric_delta(last['audience'], first['audience'])}; "
            f"охват — {metric_delta(last['reach'], first['reach'])}; "
            f"вовлеченность — {metric_delta(last['engagement'], first['engagement'])}."
        )

    return aggregate_metrics


def render_period_metrics_line(messages: pd.DataFrame) -> dict[str, Any]:
    """Метрики периода одной строкой — для рабочих разделов.

    Полоса из семи карточек уместна в «Обзоре», где показатели периода и есть
    содержание. В разделах, где аналитик работает с таблицами, она занимает
    треть экрана и отодвигает работу вниз, а те же числа нужны там лишь как
    ориентир: с каким объёмом имеем дело и есть ли негатив.

    Динамика к прошлому периоду сюда не идёт намеренно: со стрелками и
    процентами строка перестаёт читаться с одного взгляда, а за подробностями
    есть «Обзор».
    """
    metrics = overview_metrics(messages)
    sentiment = metrics.get("sentiment") or {}
    total = int(sentiment.get("total", 0))
    parts = [
        f"{format_int(metrics.get('messages', 0))} сообщений",
        f"аудитория {format_int(metrics.get('audience', 0))}",
        f"охват {format_int(metrics.get('reach', 0))}",
        f"вовлечённость {format_int(metrics.get('engagement', 0))}",
    ]
    if total:
        parts.append(f"негатив {percent_text(int(sentiment.get('negative', 0)), total)}")
    st.caption(" · ".join(parts))
    return metrics


def render_project_intro(
    project_name: str,
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    *,
    profile_label: str = "",
    chart_label_settings: dict[str, Any] | None = None,
    comparison_visible_charts: list[str] | None = None,
    show_comparison: bool = True,
    show_title: bool = True,
    previous_metrics: dict[str, Any] | None = None,
    previous_label: str = "",
) -> dict[str, Any]:
    """Unified top block for all project profiles.

    If several periods are selected, the top cards show aggregate values for
    the whole selected range. Sequential comparison is rendered below as a
    separate analytical block and does not replace the aggregate overview.
    """
    period_label = selected_period_label(periods, period_ids)
    selected_ids = [x for x in (period_ids or []) if str(x).strip()]
    metrics = overview_metrics(messages)
    sent = metrics["sentiment"]
    total = int(sent.get("total", 0))

    # Заголовок и подпись периода рисуются здесь только в старых вызовах.
    # На главной странице их берёт на себя компактная шапка проекта.
    if show_title:
        st.header(project_name)
        if profile_label:
            st.caption(f"Профиль проекта: {profile_label}")
        st.subheader("Период и основные метрики")
        if len(selected_ids) >= 2:
            st.caption(
                f"Выбрано периодов: {len(selected_ids)} · общие данные по выбранным периодам: {period_label}"
            )
        else:
            st.caption(f"Период: {period_label}")

    previous = previous_metrics or {}
    prev_sent = (previous.get("sentiment") or {}) if previous else {}
    prev_total = int(prev_sent.get("total", 0) or 0)

    def _delta(key: str) -> str | None:
        if not previous:
            return None
        return metric_delta(metrics.get(key, 0), previous.get(key, 0))

    def _share_delta(key: str) -> str | None:
        if not previous or not prev_total or not total:
            return None
        return pp_delta(
            sent.get(key, 0) / total, prev_sent.get(key, 0) / prev_total
        )

    volume_cards = [
        ("Сообщений", "messages"),
        ("Аудитория", "audience"),
        ("Охват", "reach"),
        ("Вовлеченность", "engagement"),
    ]
    for column, (label, key) in zip(st.columns(4), volume_cards):
        with column, st.container(border=True):
            st.metric(label, format_int(metrics.get(key, 0)), delta=_delta(key))

    tone_cards = [("Позитив", "positive"), ("Нейтрал", "neutral"), ("Негатив", "negative")]
    for column, (label, key) in zip(st.columns(3), tone_cards):
        with column, st.container(border=True):
            st.metric(
                label,
                percent_text(sent.get(key, 0), total),
                delta=_share_delta(key),
                help=f"{format_int(sent.get(key, 0))} сообщений",
            )

    if previous_label:
        st.caption(f"Изменения — к предыдущему периоду: {previous_label}")

    metrics["period_label"] = period_label
    metrics["project_name"] = project_name

    if (
        show_comparison
        and len(selected_ids) >= 2
        and isinstance(messages, pd.DataFrame)
        and "period_id" in messages.columns
    ):
        st.divider()
        comparison_metrics = render_period_comparison_metrics(
            messages,
            periods,
            period_ids,
            chart_label_settings=chart_label_settings,
            comparison_visible_charts=comparison_visible_charts,
        )
        if comparison_metrics is not None:
            metrics["comparison_sequence"] = comparison_metrics.get(
                "comparison_sequence"
            )
            metrics["comparison"] = comparison_metrics.get("comparison")

    return metrics
