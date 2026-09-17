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
from services.ai_summary import comparison_block, metrics_block
from services.cached_store import (
    ManualEditConflict,
    clear_platform_caches,
    delete_manual,
    get_manual,
    get_manual_version,
    save_manual,
)
from services.metrics_compute import format_int, overview_metrics
from services.period_comparison import selected_period_label
from services.report_highlights import event_title_column, top_report_events
from services.roles import role_rank


def _as_subheading(block: str) -> str:
    """Помечает первую строку блока как подзаголовок ("## ") - тот же
    маркер, что Streamlit и так рисует как заголовок в live-превью
    (st.markdown), а при экспорте в PDF/DOCX превращается в оформленный
    подзаголовок вместо обычного абзаца (см. report_export._render_summary_text_line)."""
    if not block:
        return block
    head, _, rest = block.partition("\n")
    return f"## {head}\n{rest}" if rest else f"## {head}"


def build_auto_summary(
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    periods: pd.DataFrame,
    selected_period_ids: list[str],
    *,
    metrics: dict | None = None,
) -> str:
    """Читаемый текст саммари без ИИ - пока никто не сгенерировал и не \
сохранил версию от модели, аналитик и экспорт видят именно это.

    Раньше это была россыпь предложений без структуры и без самих метрик/
    динамики (только число сообщений и доля негатива). metrics_block/
    comparison_block - те же функции, что собирают карточку данных для ИИ
    (services/ai_summary.py) - дают готовые, проверенные блоки "Метрики
    периода"/"Динамика", здесь их не пересчитывают заново.
    """
    metrics = metrics or overview_metrics(messages)
    total = len(messages)
    chats = messages["chat_title"].nunique() if "chat_title" in messages.columns else 0
    authors = messages["author"].nunique() if "author" in messages.columns else 0
    neg = int((metrics.get("sentiment") or {}).get("negative", 0) or 0)
    neg_share = (neg / total * 100) if total else 0
    period_names = []
    if not periods.empty:
        subset = periods[
            periods["period_id"].astype(str).isin([str(x) for x in selected_period_ids])
        ]
        period_names = [
            str(x) for x in subset.get("period_name", pd.Series(dtype=str)).tolist()
        ]
    # top_report_events - та же выборка (без служебных "Без сюжета" и
    # похожих, отсортирована по числу сообщений), что и остальной отчёт -
    # раньше здесь был третий по счёту способ выбрать топ (events_agg.head(5)
    # без фильтра и без гарантированной сортировки).
    top_events_rows = top_report_events(events_agg, limit=5)
    top_events = []
    if not top_events_rows.empty:
        title_col = event_title_column(top_events_rows) or "title"
        top_events = top_events_rows[title_col].astype(str).tolist()
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

    intro = [
        f"За выбранный период обработано {format_int(total)} сообщений из "
        f"{format_int(chats)} чатов; уникальных авторов — {format_int(authors)}.",
        f"Негативных сообщений: {format_int(neg)} ({neg_share:.1f}%).",
    ]
    if period_names:
        intro.append(
            "Периоды: "
            + "; ".join(period_names[:6])
            + ("…" if len(period_names) > 6 else "")
        )
    if top_events:
        intro.append("Основные инфоповоды: " + "; ".join(top_events) + ".")
    if top_chats:
        intro.append("Наиболее активные чаты: " + "; ".join(top_chats) + ".")

    blocks = ["\n".join(intro), _as_subheading(metrics_block(messages, metrics))]

    comparison = (metrics or {}).get("comparison") or {}
    if comparison.get("previous") and comparison.get("current"):
        blocks.append(_as_subheading(comparison_block(metrics)))

    client_overview = build_client_insights_summary(
        messages, events_agg, periods, selected_period_ids
    )
    if client_overview:
        blocks.append(client_overview)

    return "\n\n".join(block for block in blocks if block)


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
    auto_summary = build_auto_summary(messages, events_agg, periods, period_ids, metrics=metrics)
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
            "Можно скачать Word, PDF или отдельную PNG-инфографику — набор блоков "
            "настраивается ниже, PNG сам перестраивается под выбор."
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
            project_settings=project_settings,
            project_id=project_id,
            role_can_edit=role_rank(role) >= role_rank("editor"),
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
