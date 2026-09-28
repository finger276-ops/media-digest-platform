# -*- coding: utf-8 -*-
"""Выгрузки саммари: данные отчёта (payload) и вход во все форматы.

Framework-independent (без Streamlit), как services/brand_metrics.py — платформа
только собирает данные в payload, а сама отрисовка/сборка документа не знает
про Streamlit. UI-обёртка (кнопки скачивания) — в report_export_ui.py.

Форматы разнесены по модулям: report_png (инфографика), report_docx (Word),
report_pdf (PDF), report_pptx (PowerPoint); общее для них — report_common.
Этот модуль собирает payload и ре-экспортирует всё, что раньше жило здесь:
внешний код по-прежнему пишет `from services.report_export import …`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from .formatting import ascii_filename
from .cached_store import download_storage_file
from .message_kinds import message_type_counts
from .source_stats import build_source_statistics
from .metrics_compute import (
    VOLUME_METRICS,
    metric_missing,
    sentiment_unmarked,
)
from .project_settings import report_branding_from_project_settings
from .report_highlights import event_title_column, top_report_events, top_report_tags

from .report_common import (  # noqa: F401 — ре-экспорт: имена жили здесь
    _VISUAL_SECTIONS,
    _adaptive_font_size,
    _classify_summary_line,
    _docx_metric,
    _export_sentiment,
    _logo_image_from_payload,
    _pdf_metric_cards,
    _short_label,
    message_types_line,
    resolve_report_sections,
    sources_line,
    top_sources_lines,
)
from .report_docx import _docx_bottom_border, generate_summary_docx  # noqa: F401
from .report_pdf import (  # noqa: F401
    _PdfMetricsBlock,
    _PdfSentimentBlock,
    _PdfTopListsBlock,
    _pdf_bold_font_name,
    _pdf_font_candidates,
    _pdf_font_name,
    generate_summary_pdf,
)
from .report_png import (  # noqa: F401
    _FOOTER_CLEARANCE,
    _draw_export_card,
    _draw_highlights_section,
    _draw_metrics_section,
    _draw_sentiment_section,
    _draw_sources_section,
    _draw_top_lists_section,
    generate_summary_infographic_png,
)

LOGGER = logging.getLogger("platform.report_export")


def first_existing_col(df: pd.DataFrame, columns: list[str | None]) -> str | None:
    if df is None or not isinstance(df, pd.DataFrame):
        return None
    for col in columns or []:
        if col and col in df.columns:
            return str(col)
    return None


def clean_summary_for_export(text: str) -> str:
    value = str(text or "").strip()
    value = value.replace("**", "")
    return value


def safe_export_filename(project_name: str, period_label: str, ext: str) -> str:
    """Имя файла отчёта латиницей — кириллическое браузер сохранял как «download»."""
    return ascii_filename("summary", project_name, period_label, ext=ext, fallback="summary")


def export_top_tags(
    messages: pd.DataFrame | None, limit: int = 5
) -> list[dict[str, Any]]:
    """Топ тегов для PNG/DOCX/PDF — та же выборка, что и превью на «Обзоре»
    («Что включить в отчёт», client_insights_ui.top_client_tags) и карточка
    для ИИ (ai_summary._tags_block): все три берут top_report_tags."""
    if messages is None or not isinstance(messages, pd.DataFrame) or messages.empty:
        return []
    try:
        stats = top_report_tags(messages, limit=limit)
    except Exception:  # noqa: BLE001 — выгрузка без топ-тегов лучше, чем никакая
        LOGGER.warning("Топ-теги для выгрузки не посчитались", exc_info=True)
        return []
    result: list[dict[str, Any]] = []
    for _, row in stats.iterrows():
        tag = str(row.get("Тег") or "").strip()
        if not tag:
            continue
        result.append(
            {
                "name": tag,
                "messages": int(row.get("Сообщений", 0) or 0),
                "audience": int(row.get("Аудитория", 0) or 0),
                "reach": int(row.get("Охват", 0) or 0),
                "engagement": int(row.get("Вовлеченность", 0) or 0),
            }
        )
    return result


def export_top_events(
    events: pd.DataFrame | None, limit: int = 5
) -> list[dict[str, Any]]:
    """Топ инфоповодов для PNG/DOCX/PDF — та же выборка, что top_client_events
    и ai_summary._events_block (см. export_top_tags)."""
    work = top_report_events(events, limit=limit)
    if work.empty:
        return []
    title_col = event_title_column(work)
    count_col = first_existing_col(work, ["message_count", "messages", "Сообщений"])
    reach_col = first_existing_col(
        work, ["views", "reach", "Охват", "Просмотры", "Просмотров"]
    )
    engagement_col = first_existing_col(
        work, ["engagement", "Вовлеченность", "Вовлечённость"]
    )
    result: list[dict[str, Any]] = []
    for _, row in work.iterrows():
        name = str(row.get(title_col) or "").strip() if title_col else ""
        if not name:
            continue
        result.append(
            {
                "name": name,
                "messages": int(row.get(count_col, 0) or 0) if count_col else 0,
                "reach": int(row.get(reach_col, 0) or 0) if reach_col else 0,
                "engagement": (
                    int(row.get(engagement_col, 0) or 0) if engagement_col else 0
                ),
            }
        )
    return result


def summary_highlights(summary_text: str, limit: int = 4) -> list[str]:
    lines: list[str] = []
    for raw in str(summary_text or "").replace("\r", "\n").split("\n"):
        kind, line = _classify_summary_line(raw)
        # Подзаголовки ("## ...") - не наблюдение, а название раздела; сами
        # по себе в "Главное" не годятся (там и так рядом отдельный блок
        # метрик/тональности - см. _VISUAL_SECTIONS).
        if kind == "heading":
            continue
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= limit:
            break
    if lines:
        return lines
    return ["Саммари пока не заполнено."]


def _load_report_logo_bytes(branding: dict[str, Any] | None) -> bytes:
    """Load logo bytes from Storage path or, as fallback, from public URL."""
    branding = branding or {}
    storage_path = str(branding.get("logo_storage_path") or "").strip()
    if storage_path:
        try:
            return download_storage_file(storage_path)
        except Exception:  # noqa: BLE001 — ниже есть запасной путь по ссылке
            LOGGER.warning("Логотип из Storage не скачался: %s", storage_path)
    url = str(branding.get("logo_url") or "").strip()
    if url.startswith(("http://", "https://")):
        try:
            from urllib.request import urlopen

            with urlopen(
                url, timeout=6
            ) as response:  # nosec - user-provided report asset URL
                return response.read()
        except Exception:  # noqa: BLE001 — отчёт без логотипа лучше, чем без отчёта
            LOGGER.warning("Логотип по ссылке не скачался: %s", url)
            return b""
    return b""


def report_message_types(
    metrics: dict[str, Any], messages: pd.DataFrame | None
) -> list[tuple[str, int]]:
    """Пост / комментарий / репост для отчёта — те же числа, что в шапке «Обзора»."""
    if isinstance(metrics, dict) and "message_types" in metrics:
        return [(str(label), int(count or 0)) for label, count in metrics.get("message_types") or []]
    if isinstance(messages, pd.DataFrame) and not messages.empty:
        return message_type_counts(messages)
    return []


def export_top_sources(messages: pd.DataFrame | None, limit: int = 5) -> list[dict[str, Any]]:
    """Площадки с наибольшим числом сообщений — домены, как в «Источниках»."""
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        return []
    stats = build_source_statistics(messages)
    if stats.empty:
        return []
    total = int(len(messages))
    return [
        {"name": str(row["label"]), "messages": int(row["messages"]), "share": int(row["messages"]) / total}
        for _, row in stats.head(limit).iterrows()
    ]


def summary_export_payload(
    project_name: str,
    period_label: str,
    summary_text: str,
    metrics: dict[str, Any],
    messages: pd.DataFrame | None = None,
    events_agg: pd.DataFrame | None = None,
    *,
    branding: dict[str, Any] | None = None,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    sent = metrics.get("sentiment", {}) if isinstance(metrics, dict) else {}
    sections = resolve_report_sections(sections)
    branding = report_branding_from_project_settings(
        {"report_branding": branding or {}}, project_name=project_name
    )
    return {
        "project_name": project_name,
        "client_name": branding.get("client_name") or project_name,
        "report_title": branding.get("report_title") or "Дайджест упоминаний",
        "accent_color": branding.get("accent_color") or "#2563eb",
        "background_color": branding.get("background_color") or "#ffffff",
        "footer_text": branding.get("footer_text") or "",
        "logo_url": branding.get("logo_url") or "",
        "logo_storage_path": branding.get("logo_storage_path") or "",
        "logo_filename": branding.get("logo_filename") or "",
        "logo_mime_type": branding.get("logo_mime_type") or "",
        "logo_bytes": _load_report_logo_bytes(branding),
        "sections": sections,
        "period_label": period_label,
        "summary_text": clean_summary_for_export(summary_text),
        "summary_highlights": summary_highlights(summary_text),
        "messages": int(metrics.get("messages", 0) or 0),
        "audience": int(metrics.get("audience", 0) or 0),
        "reach": int(metrics.get("reach", 0) or 0),
        "engagement": int(metrics.get("engagement", 0) or 0),
        # Каких метрик нет в выгрузке: вместо нуля отчёт печатает прочерк.
        "known": {key: not metric_missing(metrics, key) for key in VOLUME_METRICS},
        "positive": int(sent.get("positive", 0) or 0),
        "neutral": int(sent.get("neutral", 0) or 0),
        "negative": int(sent.get("negative", 0) or 0),
        "total": int(sent.get("total", 0) or 0),
        # Без разметки тональности числа выше — «всё в нейтрале»; рендереры
        # по этому признаку рисуют пометку вместо диаграммы.
        "sentiment_markup": not sentiment_unmarked(sent, messages),
        # Пусто — в выгрузке нет «Тип сообщения», и строки о типах в отчёте нет.
        "message_types": report_message_types(metrics, messages),
        "comparison_sequence": metrics.get("comparison_sequence") or [],
        # limit=5: столько же всегда и рисуют PNG/DOCX/PDF (items[:5]) -
        # раньше "полный" шаблон запрашивал 8, но лишние 3 нигде не
        # показывались, просто отбрасывались слоем отрисовки.
        "top_tags": export_top_tags(messages, limit=5),
        "top_events": export_top_events(events_agg, limit=5),
        "top_sources": export_top_sources(messages, limit=5),
        "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }


