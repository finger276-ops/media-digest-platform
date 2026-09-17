# -*- coding: utf-8 -*-
"""Кнопки выгрузки саммари: Word / PDF / PNG-инфографика с конструктором разделов.

Сама генерация документов — в services/report_export.py (без Streamlit);
здесь только форма выбора разделов и три download_button. Раньше рядом был
ещё выбор «шаблона отчёта» (Краткое саммари/Клиентский обзор/Сравнительный/
Полный) — все варианты, кроме подписи в шапке, вели себя одинаково
(реальный набор блоков всегда определял этот же конструктор), поэтому
шаблон убрали — аналитик сразу собирает то, что нужно.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.dashboard_config import REPORT_SECTION_OPTIONS
from services.project_settings import report_sections_from_project_settings
from services.report_export import (
    generate_summary_docx,
    generate_summary_infographic_png,
    generate_summary_pdf,
    safe_export_filename,
    summary_export_payload,
)


def render_summary_export_buttons(
    project_name: str,
    period_label: str,
    summary_text: str,
    metrics: dict[str, Any],
    *,
    key_prefix: str,
    messages: pd.DataFrame | None = None,
    events_agg: pd.DataFrame | None = None,
    branding: dict[str, Any] | None = None,
    project_settings: dict[str, Any] | None = None,
    project_id: str = "",
    role_can_edit: bool = False,
) -> None:
    default_sections = report_sections_from_project_settings(project_settings)
    selected_sections = st.multiselect(
        "Разделы отчёта",
        list(REPORT_SECTION_OPTIONS.keys()),
        default=default_sections,
        format_func=lambda s: REPORT_SECTION_OPTIONS.get(s, s),
        key=f"{key_prefix}_sections",
        help=(
            "Какие блоки собрать в Word/PDF/PNG — можно оставить только то, "
            "что нужно для этой выгрузки. PNG-инфографика сама перестраивается "
            "под выбранный набор, без пустых мест."
        ),
    )
    if role_can_edit and project_id:
        if st.button(
            "Сохранить как выбор по умолчанию для проекта",
            key=f"{key_prefix}_save_sections",
            help="Следующие выгрузки будут открываться с этим набором разделов.",
        ):
            from services.cached_store import clear_platform_caches, update_project

            updated = dict(project_settings or {})
            updated["report_sections"] = list(selected_sections) or list(
                REPORT_SECTION_OPTIONS.keys()
            )
            try:
                update_project(project_id, settings=updated)
                clear_platform_caches(project_id)
                st.success("Сохранено как выбор по умолчанию для проекта.")
            except Exception as exc:  # noqa: BLE001 — сохранение не должно ронять выгрузку
                st.warning(f"Не удалось сохранить: {exc}")

    payload = summary_export_payload(
        project_name,
        period_label,
        summary_text,
        metrics,
        messages=messages,
        events_agg=events_agg,
        branding=branding,
        sections=selected_sections,
    )
    st.caption(
        f"Брендирование: {payload.get('client_name') or project_name}; акцентный цвет {payload.get('accent_color')}."
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        try:
            st.download_button(
                "Скачать Word",
                data=generate_summary_docx(payload),
                file_name=safe_export_filename(project_name, period_label, "docx"),
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                width="stretch",
                key=f"{key_prefix}_docx",
            )
        except Exception as exc:
            st.warning(str(exc))
    with c2:
        try:
            st.download_button(
                "Скачать PDF",
                data=generate_summary_pdf(payload),
                file_name=safe_export_filename(project_name, period_label, "pdf"),
                mime="application/pdf",
                width="stretch",
                key=f"{key_prefix}_pdf",
            )
        except Exception as exc:
            st.warning(str(exc))
    with c3:
        try:
            st.download_button(
                "Скачать инфографику PNG",
                data=generate_summary_infographic_png(payload),
                file_name=safe_export_filename(project_name, period_label, "png"),
                mime="image/png",
                width="stretch",
                key=f"{key_prefix}_png",
            )
        except Exception as exc:
            st.warning(str(exc))
