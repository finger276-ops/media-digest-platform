# -*- coding: utf-8 -*-
"""Кнопки выгрузки саммари: Word / PDF / PNG-инфографика с выбором шаблона отчёта.

Сама генерация документов — в services/report_export.py (без Streamlit);
здесь только форма выбора шаблона и три download_button.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.dashboard_config import REPORT_TEMPLATE_OPTIONS
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
) -> None:
    report_template = st.selectbox(
        "Шаблон отчета",
        list(REPORT_TEMPLATE_OPTIONS.keys()),
        index=0,
        format_func=lambda x: REPORT_TEMPLATE_OPTIONS.get(x, x),
        key=f"{key_prefix}_template",
        help="Шаблон меняет структуру выгрузки и набор аналитических блоков в Word/PDF/PNG.",
    )
    payload = summary_export_payload(
        project_name,
        period_label,
        summary_text,
        metrics,
        messages=messages,
        events_agg=events_agg,
        report_template=report_template,
        branding=branding,
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
