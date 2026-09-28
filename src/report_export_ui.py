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

from error_ui import show_error
from services.observability import report_failure
from services.dashboard_config import REPORT_SECTION_OPTIONS
from services.project_settings import (
    report_sections_from_project_settings,
    report_sections_setting,
)
from services.report_export import (
    _export_sentiment,
    generate_summary_docx,
    generate_summary_infographic_png,
    generate_summary_pdf,
    safe_export_filename,
    summary_export_payload,
)
from services.report_pptx import generate_summary_pptx


def _export_failed(format_name: str, exc: BaseException) -> None:
    """Выгрузка не собралась: владельцу — событие и подробности, остальным — фраза.

    Раньше здесь показывался текст исключения как есть: клиент читал
    «добавьте python-docx в requirements.txt» или «задайте PLATFORM_PDF_FONT_PATH».
    """
    report_failure(f"выгрузка отчёта {format_name}", exc)
    show_error(
        f"Не удалось подготовить {format_name}. Попробуйте другой формат или "
        "повторите позже.",
        exc,
        warning=True,
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
    read_only: bool = False,
) -> None:
    default_sections = report_sections_from_project_settings(project_settings)
    selected_sections = st.multiselect(
        "Разделы отчёта",
        list(REPORT_SECTION_OPTIONS.keys()),
        default=default_sections,
        format_func=lambda s: REPORT_SECTION_OPTIONS.get(s, s),
        key=f"{key_prefix}_sections",
        help=(
            "Какие блоки собрать в Word/PDF/PNG/PowerPoint — можно оставить только то, "
            "что нужно для этой выгрузки. PNG-инфографика сама перестраивается "
            "под выбранный набор, без пустых мест."
        ),
    )
    # В демо-проекте гость с кодом редактора мог сохранить свой набор
    # разделов как умолчание для всех следующих гостей — тот же гейт, что и
    # у «Открывать проект на этом разделе» в app.py.
    if role_can_edit and project_id and not read_only:
        if st.button(
            "Сохранить как выбор по умолчанию для проекта",
            key=f"{key_prefix}_save_sections",
            help="Следующие выгрузки будут открываться с этим набором разделов.",
        ):
            from services.cached_store import clear_platform_caches, update_project

            updated = dict(project_settings or {})
            updated.update(report_sections_setting(selected_sections))
            try:
                update_project(project_id, settings=updated)
                clear_platform_caches(project_id)
                st.success("Сохранено как выбор по умолчанию для проекта.")
            except Exception as exc:  # noqa: BLE001 — сохранение не должно ронять выгрузку
                show_error("Не удалось сохранить выбор разделов.", exc, warning=True)

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
    # Код цвета (#2563eb) — техническая деталь: цвет задаётся и виден в
    # настройках проекта, а здесь достаточно сказать, чьё оформление у отчёта.
    st.caption(f"Оформление отчёта: {payload.get('client_name') or project_name}.")
    if "sentiment" in (payload.get("sections") or []):
        if not payload.get("sentiment_markup", True):
            st.caption(
                "В выгрузке нет разметки тональности — в блоке «Тональность» будет "
                "пометка вместо диаграммы."
            )
        elif _export_sentiment(payload, payload.get("comparison_sequence") or [])[4]:
            # PNG и PDF при нескольких периодах рисуют последний.
            st.caption(
                "В последнем периоде нет разметки тональности — в PNG и PDF в блоке "
                "«Тональность» будет пометка вместо диаграммы."
            )
    c1, c2, c3, c4 = st.columns(4)
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
        except Exception as exc:  # noqa: BLE001 — остальные форматы должны работать
            _export_failed("Word", exc)
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
        except Exception as exc:  # noqa: BLE001 — остальные форматы должны работать
            _export_failed("PDF", exc)
    with c3:
        try:
            st.download_button(
                "Скачать PNG",
                data=generate_summary_infographic_png(payload),
                file_name=safe_export_filename(project_name, period_label, "png"),
                mime="image/png",
                width="stretch",
                key=f"{key_prefix}_png",
            )
        except Exception as exc:  # noqa: BLE001 — остальные форматы должны работать
            _export_failed("PNG", exc)
    with c4:
        try:
            st.download_button(
                "Скачать PowerPoint",
                data=generate_summary_pptx(payload),
                file_name=safe_export_filename(project_name, period_label, "pptx"),
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                width="stretch",
                key=f"{key_prefix}_pptx",
            )
        except Exception as exc:  # noqa: BLE001 — остальные форматы должны работать
            _export_failed("PowerPoint", exc)
