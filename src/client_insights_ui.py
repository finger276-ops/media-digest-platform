# -*- coding: utf-8 -*-
"""Раздел «Клиентский обзор»: риски, сигналы и изменения для презентации заказчику.

Не пересчитывает теги/инфоповоды/метрики заново — берёт их из уже готовых
таблиц (services.tag_compute, services.period_comparison) и собирает сводный
слой поверх них. build_client_insights_summary даёт текстовую версию того же
блока для автоматического саммари периода.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.metrics_compute import format_int, overview_metrics
from services.period_comparison import ordered_period_ids, period_metrics_for_comparison
from services.report_highlights import is_technical_event_title
from services.report_highlights import event_title_column as event_title_col
from services.report_highlights import top_report_events as top_client_events
from services.report_highlights import top_report_tags as top_client_tags
from services.tag_compute import build_tag_statistics


def build_period_change_insights(
    messages: pd.DataFrame, periods: pd.DataFrame, selected_period_ids: list[str]
) -> list[str]:
    comparison = period_metrics_for_comparison(messages, periods, selected_period_ids)
    if len(comparison) < 2:
        return []
    prev, cur = comparison[-2], comparison[-1]
    insights: list[str] = []
    checks = [
        ("сообщений", "messages"),
        ("аудитории", "audience"),
        ("охвата", "reach"),
        ("вовлеченности", "engagement"),
    ]
    for label, key in checks:
        old = float(prev.get(key, 0) or 0)
        new = float(cur.get(key, 0) or 0)
        if old == 0 and new == 0:
            continue
        delta = new - old
        if abs(delta) < 1:
            continue
        direction = "выросла" if delta > 0 else "снизилась"
        if label == "сообщений":
            direction = "выросло" if delta > 0 else "снизилось"
        percent = f" ({delta / old * 100:+.0f}%)" if old else ""
        insights.append(
            f"Количество {label} {direction}: {format_int(delta)}{percent} к предыдущему периоду."
        )

    neg_delta = float(cur.get("negative_share", 0) or 0) - float(
        prev.get("negative_share", 0) or 0
    )
    if abs(neg_delta) >= 0.001:
        insights.append(
            f"Доля негатива изменилась на {neg_delta * 100:+.1f} п.п. к предыдущему периоду."
        )
    return insights


def build_tag_change_table(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    selected_period_ids: list[str],
    limit: int = 10,
) -> pd.DataFrame:
    ordered_ids = ordered_period_ids(periods, selected_period_ids)
    if (
        len(ordered_ids) < 2
        or messages is None
        or messages.empty
        or "period_id" not in messages.columns
    ):
        return pd.DataFrame()
    prev_id, cur_id = str(ordered_ids[-2]), str(ordered_ids[-1])
    prev_stats = build_tag_statistics(
        messages[messages["period_id"].astype(str) == prev_id].copy()
    )
    cur_stats = build_tag_statistics(
        messages[messages["period_id"].astype(str) == cur_id].copy()
    )
    if prev_stats.empty and cur_stats.empty:
        return pd.DataFrame()
    prev = (
        prev_stats[["Тег", "Сообщений", "Охват", "Вовлеченность", "Негатив"]].copy()
        if not prev_stats.empty
        else pd.DataFrame(
            columns=["Тег", "Сообщений", "Охват", "Вовлеченность", "Негатив"]
        )
    )
    cur = (
        cur_stats[["Тег", "Сообщений", "Охват", "Вовлеченность", "Негатив"]].copy()
        if not cur_stats.empty
        else pd.DataFrame(
            columns=["Тег", "Сообщений", "Охват", "Вовлеченность", "Негатив"]
        )
    )
    prev = prev.rename(columns={c: f"{c}_prev" for c in prev.columns if c != "Тег"})
    cur = cur.rename(columns={c: f"{c}_cur" for c in cur.columns if c != "Тег"})
    merged = cur.merge(prev, on="Тег", how="outer").fillna(0)
    for metric in ["Сообщений", "Охват", "Вовлеченность", "Негатив"]:
        merged[f"Δ {metric.lower()}"] = pd.to_numeric(
            merged.get(f"{metric}_cur", 0), errors="coerce"
        ).fillna(0) - pd.to_numeric(
            merged.get(f"{metric}_prev", 0), errors="coerce"
        ).fillna(
            0
        )
    merged["abs_delta"] = (
        merged[["Δ сообщений", "Δ охват", "Δ вовлеченность", "Δ негатив"]]
        .abs()
        .sum(axis=1)
    )
    merged = merged.sort_values("abs_delta", ascending=False).head(limit)
    out = pd.DataFrame(
        {
            "Тег": merged["Тег"].astype(str),
            "Сообщений сейчас": merged["Сообщений_cur"].astype(int),
            "Δ сообщений": merged["Δ сообщений"].astype(int),
            "Охват сейчас": merged["Охват_cur"].astype(int),
            "Δ охвата": merged["Δ охват"].astype(int),
            "Вовлеченность сейчас": merged["Вовлеченность_cur"].astype(int),
            "Δ вовлеченности": merged["Δ вовлеченность"].astype(int),
            "Негатив сейчас": merged["Негатив_cur"].astype(int),
            "Δ негатива": merged["Δ негатив"].astype(int),
        }
    )
    return out


def build_client_insights_summary(
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    periods: pd.DataFrame,
    selected_period_ids: list[str],
    *,
    profile: str = "",
) -> str:
    """Return a text version of the client-insights block for automatic summaries."""
    metrics = overview_metrics(messages)
    sent = metrics.get("sentiment", {}) or {}
    total = int(sent.get("total", 0) or 0)
    negative = int(sent.get("negative", 0) or 0)
    negative_share = negative / total if total else 0.0
    engagement = int(metrics.get("engagement", 0) or 0)
    risk_level = (
        "низкий"
        if negative_share < 0.01
        else "средний" if negative_share < 0.05 else "высокий"
    )

    lines: list[str] = []
    lines.append("Клиентский обзор")
    lines.append(
        f"Риск негатива: {risk_level}; негативных сообщений — {format_int(negative)} ({negative_share * 100:.1f}%)."
    )
    lines.append(f"Суммарная вовлеченность: {format_int(engagement)}.")

    if len(selected_period_ids or []) >= 2:
        change_lines = build_period_change_insights(
            messages, periods, selected_period_ids
        )
        if change_lines:
            lines.append("Что изменилось к предыдущему периоду:")
            for item in change_lines[:5]:
                lines.append(f"• {item}")

        tag_changes = build_tag_change_table(
            messages, periods, selected_period_ids, limit=5
        )
        if tag_changes is not None and not tag_changes.empty:
            lines.append("Теги с заметными изменениями:")
            for _, row in tag_changes.head(5).iterrows():
                parts = [str(row.get("Тег") or "")]
                try:
                    delta_messages = int(row.get("Δ сообщений", 0) or 0)
                except Exception:
                    delta_messages = 0
                try:
                    delta_reach = int(row.get("Δ охвата", 0) or 0)
                except Exception:
                    delta_reach = 0
                try:
                    delta_eng = int(row.get("Δ вовлеченности", 0) or 0)
                except Exception:
                    delta_eng = 0
                details = []
                if delta_messages:
                    details.append(f"сообщения {format_int(delta_messages)}")
                if delta_reach:
                    details.append(f"охват {format_int(delta_reach)}")
                if delta_eng:
                    details.append(f"вовлеченность {format_int(delta_eng)}")
                if details:
                    parts.append("; ".join(details))
                lines.append("• " + " — ".join([p for p in parts if p]))

    tags = top_client_tags(messages, limit=5)
    if tags is not None and not tags.empty:
        lines.append("Топ тегов для отчета:")
        for _, row in tags.iterrows():
            lines.append(
                f"• {row.get('Тег', '')} — {format_int(row.get('Сообщений', 0))} сообщ.; "
                f"охват {format_int(row.get('Охват', 0))}; вовлеченность {format_int(row.get('Вовлеченность', 0))}."
            )

    top_events = top_client_events(events_agg, limit=5)
    if top_events is not None and not top_events.empty:
        title_col = event_title_col(top_events) or "title"
        lines.append("Топ инфоповодов для отчета:")
        for _, row in top_events.iterrows():
            lines.append(
                f"• {row.get(title_col, '')} — {format_int(row.get('message_count', 0))} сообщ."
            )

    return "\n".join(line for line in lines if str(line).strip())


def render_client_insights(
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    periods: pd.DataFrame,
    selected_period_ids: list[str],
    *,
    profile: str = "",
) -> None:
    st.subheader("Клиентский обзор")
    st.caption(
        "Сводный слой для презентации заказчику: риски, ключевые сигналы и изменения между периодами."
    )

    metrics = overview_metrics(messages)
    sent = metrics.get("sentiment", {}) or {}
    total = int(sent.get("total", 0) or 0)
    negative = int(sent.get("negative", 0) or 0)
    negative_share = negative / total if total else 0.0
    engagement = int(metrics.get("engagement", 0) or 0)

    risk_level = (
        "низкий"
        if negative_share < 0.01
        else "средний" if negative_share < 0.05 else "высокий"
    )
    top_cards = [
        ("Риск негатива", risk_level, f"Негативных сообщений: {format_int(negative)}"),
        ("Доля негатива", f"{negative_share * 100:.1f}%", ""),
        ("Вовлеченность", format_int(engagement), ""),
        (
            "Инфоповодов",
            format_int(len(events_agg) if isinstance(events_agg, pd.DataFrame) else 0),
            "",
        ),
    ]
    for column, (label, value, hint) in zip(st.columns(4), top_cards):
        with column, st.container(border=True):
            st.metric(label, value, help=hint or None)

    signals: list[dict[str, Any]] = []
    if negative > 0:
        signals.append(
            {
                "Сигнал": "Есть негативные сообщения",
                "Что смотреть": "Негативные публикации и темы с высокой вовлеченностью",
                "Данные": f"{format_int(negative)} сообщ. · {negative_share * 100:.1f}%",
                "Приоритет": "Средний" if negative_share < 0.05 else "Высокий",
            }
        )
    else:
        signals.append(
            {
                "Сигнал": "Критичный негатив не выявлен",
                "Что смотреть": "Контролировать всплески по тегам и инфоповодам",
                "Данные": "0 негативных сообщений",
                "Приоритет": "Низкий",
            }
        )

    if (
        isinstance(events_agg, pd.DataFrame)
        and not events_agg.empty
        and "negative_count" in events_agg.columns
    ):
        risky_events = events_agg.copy()
        risky_events["negative_count"] = pd.to_numeric(
            risky_events["negative_count"], errors="coerce"
        ).fillna(0)
        risky_events = risky_events[
            (risky_events["negative_count"] > 0)
            & (
                ~risky_events.get("title", pd.Series(dtype=str)).apply(
                    is_technical_event_title
                )
            )
        ]
        if not risky_events.empty:
            risky_events = risky_events.sort_values(
                ["negative_count", "message_count"], ascending=False
            ).head(3)
            title_col = event_title_col(risky_events) or "title"
            names = "; ".join(risky_events[title_col].astype(str).head(3).tolist())
            signals.append(
                {
                    "Сигнал": "Темы с негативом",
                    "Что смотреть": names,
                    "Данные": f"{format_int(risky_events['negative_count'].sum())} нег. сообщ.",
                    "Приоритет": "Средний",
                }
            )

    tags = build_tag_statistics(messages)
    if tags is not None and not tags.empty and "Негатив" in tags.columns:
        neg_tags = tags.copy()
        neg_tags["Негатив"] = pd.to_numeric(
            neg_tags["Негатив"], errors="coerce"
        ).fillna(0)
        neg_tags = (
            neg_tags[neg_tags["Негатив"] > 0]
            .sort_values(["Негатив", "Сообщений"], ascending=False)
            .head(3)
        )
        if not neg_tags.empty:
            tag_names = "; ".join(neg_tags["Тег"].astype(str).tolist())
            signals.append(
                {
                    "Сигнал": "Теги с негативом",
                    "Что смотреть": tag_names,
                    "Данные": f"{format_int(neg_tags['Негатив'].sum())} нег. сообщ.",
                    "Приоритет": "Средний",
                }
            )

    st.markdown("#### Риски и сигналы")
    # Раньше это была таблица на четыре колонки: читалась как выгрузка, а не как
    # вывод для клиента. Теперь каждый сигнал — отдельная карточка.
    priority_colors = {"Высокий": "red", "Средний": "orange", "Низкий": "green"}
    for row_start in range(0, len(signals), 3):
        chunk = signals[row_start : row_start + 3]
        for column, signal in zip(st.columns(3), chunk):
            with column, st.container(border=True):
                priority = str(signal.get("Приоритет", ""))
                badge = getattr(st, "badge", None)
                if callable(badge):
                    badge(priority, color=priority_colors.get(priority, "gray"))
                else:
                    st.caption(f"Приоритет: {priority}")
                st.markdown(f"**{signal.get('Сигнал', '')}**")
                st.caption(str(signal.get("Что смотреть", "")))
                st.markdown(f"`{signal.get('Данные', '')}`")

    if len(selected_period_ids or []) >= 2:
        st.markdown("#### Что изменилось к предыдущему периоду")
        insights = build_period_change_insights(messages, periods, selected_period_ids)
        if insights:
            for row_start in range(0, min(len(insights), 6), 2):
                for column, item in zip(
                    st.columns(2), insights[row_start : row_start + 2]
                ):
                    with column, st.container(border=True):
                        st.markdown(str(item))
        else:
            st.caption("Значимых изменений по основным метрикам не найдено.")
        tag_changes = build_tag_change_table(
            messages, periods, selected_period_ids, limit=10
        )
        if not tag_changes.empty:
            display = tag_changes.copy()
            for col in [c for c in display.columns if c != "Тег"]:
                display[col] = display[col].apply(format_int)
            with st.expander("Теги с наибольшими изменениями", expanded=True):
                st.dataframe(display, hide_index=True, width="stretch")

    st.markdown("#### Что включить в отчет")
    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st.markdown("**Топ тегов**")
        top_tags = top_client_tags(messages, limit=5)
        if top_tags.empty:
            st.caption("Теги не найдены.")
        else:
            for _, row in top_tags.iterrows():
                st.markdown(f"**{row['Тег']}**")
                st.caption(
                    f"{format_int(row.get('Сообщений', 0))} сообщений · "
                    f"охват {format_int(row.get('Охват', 0))}"
                )
    with c2, st.container(border=True):
        st.markdown("**Топ инфоповодов**")
        top_events = top_client_events(events_agg, limit=5)
        if top_events.empty:
            st.caption("Инфоповоды не найдены.")
        else:
            title_col = event_title_col(top_events) or "title"
            for _, row in top_events.iterrows():
                st.markdown(f"**{row.get(title_col, '')}**")
                st.caption(f"{format_int(row.get('message_count', 0))} сообщений")
