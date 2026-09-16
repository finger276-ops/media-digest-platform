# -*- coding: utf-8 -*-
"""Раздел «Саммари периода»: автоматический текст, ручное редактирование,
панель генерации ИИ и кнопки выгрузки (Word/PDF/PNG).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from ai_summary_ui import render_ai_summary_panel
from client_insights_ui import build_client_insights_summary
from report_export_ui import render_summary_export_buttons
from services.cached_store import (
    ManualEditConflict,
    clear_platform_caches,
    delete_manual,
    get_manual,
    get_manual_version,
    save_manual,
)
from services.metrics_compute import overview_metrics
from services.period_comparison import selected_period_label
from services.roles import role_rank


def build_auto_summary(
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    periods: pd.DataFrame,
    selected_period_ids: list[str],
) -> str:
    total = len(messages)
    chats = messages["chat_title"].nunique() if "chat_title" in messages.columns else 0
    authors = messages["author"].nunique() if "author" in messages.columns else 0
    neg = 0
    if "sentiment" in messages.columns:
        neg = int(
            messages["sentiment"]
            .fillna("")
            .astype(str)
            .str.lower()
            .str.contains("нег")
            .sum()
        )
    neg_share = (neg / total * 100) if total else 0
    period_names = []
    if not periods.empty:
        subset = periods[
            periods["period_id"].astype(str).isin([str(x) for x in selected_period_ids])
        ]
        period_names = [
            str(x) for x in subset.get("period_name", pd.Series(dtype=str)).tolist()
        ]
    top_events = events_agg.head(5)["title"].tolist() if not events_agg.empty else []
    top_chats = []
    if "chat_title" in messages.columns:
        top_chats = (
            messages["chat_title"]
            .fillna("")
            .astype(str)
            .replace("", pd.NA)
            .dropna()
            .value_counts()
            .head(5)
            .index.tolist()
        )

    lines = []
    lines.append(
        f"За выбранный период обработано {total:,} сообщений из {chats:,} чатов; уникальных авторов — {authors:,}.".replace(
            ",", " "
        )
    )
    lines.append(f"Негативных сообщений: {neg:,} ({neg_share:.1f}%).".replace(",", " "))
    if period_names:
        lines.append(
            "Периоды: "
            + "; ".join(period_names[:6])
            + ("…" if len(period_names) > 6 else "")
        )
    if top_events:
        lines.append("Основные инфоповоды: " + "; ".join(top_events) + ".")
    if top_chats:
        lines.append("Наиболее активные чаты: " + "; ".join(top_chats) + ".")

    client_overview = build_client_insights_summary(
        messages, events_agg, periods, selected_period_ids
    )
    if client_overview:
        lines.append(client_overview)
    return "\n\n".join(lines)


def summary_storage_key(period_ids: list[str], profile: str = "") -> str:
    return (
        "summary::"
        + "__".join(sorted(str(x) for x in (period_ids or []) if str(x).strip()))
    )


def render_period_summary(
    project_id: str,
    project_name: str,
    period_ids: list[str],
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    periods: pd.DataFrame,
    role: str,
    *,
    profile: str = "",
    metrics: dict[str, Any] | None = None,
    branding: dict[str, Any] | None = None,
    project_settings: dict[str, Any] | None = None,
) -> None:
    """Unified editable/exportable period summary for all project profiles."""
    st.subheader("Саммари периода")
    key = summary_storage_key(period_ids, profile)
    manual = get_manual(project_id, key)
    auto_summary = build_auto_summary(messages, events_agg, periods, period_ids)
    summary_text = str((manual or {}).get("summary") or "").strip() or auto_summary
    st.markdown(summary_text.replace("\n", "  \n"))
    if str((manual or {}).get("source") or "") == "ai":
        st.caption("Текст сгенерирован моделью и сохранён владельцем платформы.")

    # По умолчанию генерация доступна только владельцу платформы: она тратит
    # деньги и отправляет данные проекта внешнему сервису. Владелец может
    # открыть её редакторам конкретного проекта — настройка внутри панели.
    render_ai_summary_panel(
        project_id,
        project_name,
        period_ids,
        messages,
        events_agg,
        periods,
        role=role,
        metrics=metrics,
        project_settings=project_settings,
    )

    metrics = metrics or overview_metrics(messages)
    metrics.setdefault("period_label", selected_period_label(periods, period_ids))
    metrics.setdefault("project_name", project_name)
    period_label = str(
        metrics.get("period_label") or selected_period_label(periods, period_ids)
    )

    with st.expander("Выгрузить саммари", expanded=False):
        st.caption(
            "Можно скачать Word, PDF или отдельную PNG-инфографику. В инфографику попадут метрики, тональность, топ-теги, топ-инфоповоды и ключевые тезисы саммари."
        )
        render_summary_export_buttons(
            project_name,
            period_label,
            summary_text,
            metrics,
            key_prefix=f"summary_export_{abs(hash(key))}",
            messages=messages,
            events_agg=events_agg,
            branding=branding,
        )

    if role_rank(role) >= role_rank("editor"):
        with st.expander("Редактировать саммари", expanded=False):
            # Версия замораживается при первом показе поля: пока редактор
            # пишет, кеш с TTL может подтянуть чужую правку, и сохранение
            # затёрло бы её без предупреждения.
            widget_key = f"summary_{key}"
            versions_key = f"manual_versions::{widget_key}"
            if (
                widget_key not in st.session_state
                or versions_key not in st.session_state
            ):
                st.session_state[versions_key] = get_manual_version(
                    project_id, key, "summaries"
                )
            edited = st.text_area(
                "Текст саммари", value=summary_text, height=220, key=widget_key
            )
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Сохранить саммари", key=f"save_{key}"):
                    try:
                        save_manual(
                            project_id,
                            "summaries",
                            key,
                            {
                                "summary": edited,
                                "period_ids": period_ids,
                                "profile": profile,
                            },
                            expected_updated_at=st.session_state.get(versions_key),
                        )
                    except ManualEditConflict:
                        st.error(
                            "Саммари только что изменил другой редактор — "
                            "сохранение отменено, чтобы не затереть его текст. "
                            "Ваш текст остался в поле; повторное сохранение "
                            "запишет его поверх."
                        )
                        clear_platform_caches(project_id)
                        st.session_state.pop(versions_key, None)
                    else:
                        st.session_state.pop(versions_key, None)
                        st.success("Саммари сохранено.")
                        st.rerun()
            with c2:
                if st.button("Вернуть автоматическое", key=f"auto_{key}"):
                    delete_manual(project_id, key)
                    st.success("Вернули автоматическое саммари.")
                    st.rerun()
