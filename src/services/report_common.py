# -*- coding: utf-8 -*-
"""Общее для всех форматов отчёта: разделы, строки о типах и площадках,
подписи и шрифты карточек, тональность блока.

Без Streamlit и без отрисовки: этот модуль берут и report_export (данные
отчёта), и каждый формат — report_png, report_docx, report_pdf, report_pptx.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any


from .dashboard_config import DEFAULT_REPORT_SECTIONS, REPORT_SECTION_OPTIONS
from .metrics_compute import (
    format_int,
    metric_missing,
    metric_text,
    percent_text,
)

LOGGER = logging.getLogger("platform.report_export")


# Разделы, у которых вообще есть что нарисовать на PNG-инфографике. Если
# аналитик оставил только "Полный текст саммари", инфографика с одним
# заголовком и пустым телом — лишняя страница, а не полезный блок.
_VISUAL_SECTIONS = {"metrics", "sentiment", "top_tags", "top_events", "top_sources", "highlights"}


def _classify_summary_line(raw: str) -> tuple[str, str]:
    """Разобрать одну строку summary_text на (тип, видимый текст).

    Строки текста саммари (автотекст из summary_ui.build_auto_summary или
    вручную отредактированный) размечены минимально: "## " - подзаголовок
    раздела (тот же маркер, что Streamlit понимает "из коробки" в live-
    превью через st.markdown), "• "/"- " - пункт списка. И PDF, и DOCX
    рисуют эти три вида по-разному вместо одного и того же стиля абзаца на
    каждую строку."""
    line = raw.strip()
    if line.startswith("## "):
        return "heading", line[3:].strip()
    if line.startswith("• ") or line.startswith("- "):
        return "bullet", line[2:].strip()
    return "text", line


def _logo_image_from_payload(payload: dict[str, Any]):
    logo_bytes = payload.get("logo_bytes") or b""
    if not logo_bytes:
        return None
    try:
        from PIL import Image as PILImage

        return PILImage.open(BytesIO(logo_bytes)).convert("RGBA")
    except Exception:  # noqa: BLE001 — битая картинка не должна ломать выгрузку
        LOGGER.warning("Логотип не открылся как изображение", exc_info=True)
        return None


def sources_line(payload: dict[str, Any]) -> str:
    """«Площадки: vk.com 60% · telegram.org 30%» — одна строка для PNG."""
    items = payload.get("top_sources") or []
    if not items:
        return ""
    return "Площадки: " + " · ".join(f"{item['name']} {item['share'] * 100:.0f}%" for item in items)


def top_sources_lines(payload: dict[str, Any]) -> list[str]:
    """«vk.com — 412 сообщ. (67%)» — для Word и PDF."""
    return [
        f"{item['name']} — {format_int(item['messages'])} сообщ. ({item['share'] * 100:.0f}%)"
        for item in payload.get("top_sources") or []
    ]


def message_types_line(payload: dict[str, Any]) -> str:
    """«Типы сообщений: Пост — 412 (67%), Комментарий — 150 (24%)…» или ""."""
    types = payload.get("message_types") or []
    total = sum(int(count or 0) for _, count in types)
    if not types or not total:
        return ""
    parts = [f"{label} — {format_int(count)} ({percent_text(count, total)})" for label, count in types]
    return "Типы сообщений: " + ", ".join(parts) + "."


def resolve_report_sections(sections: list[str] | None) -> list[str]:
    """Нормализовать выбор блоков конструктора: неизвестные id отбрасываются,
    пустой/некорректный выбор откатывается на полный набор по умолчанию —
    отчёт без единого блока никому не нужен и обычно означает баг вызова,
    а не осознанный выбор аналитика."""
    if not sections:
        return list(DEFAULT_REPORT_SECTIONS)
    result = [s for s in sections if s in REPORT_SECTION_OPTIONS]
    return result or list(DEFAULT_REPORT_SECTIONS)


def _short_label(value: Any, max_len: int = 34) -> str:
    text = str(value or "").strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def _adaptive_font_size(value: str, *, base: int = 15, min_size: int = 10) -> int:
    """Keep large metric values readable inside report cards."""
    length = len(str(value or ""))
    if length > 15:
        return max(min_size, base - 5)
    if length > 12:
        return max(min_size, base - 4)
    if length > 9:
        return max(min_size, base - 2)
    return base


def _export_sentiment(
    payload: dict[str, Any], comparison: list[dict[str, Any]]
) -> tuple[int, int, int, int, bool]:
    """Тональность для блока отчёта: (позитив, нейтрал, негатив, всего, не размечено).

    Итог по всей выбранной области, а не по последней точке разбивки
    comparison_sequence — тот же рассинхрон с «Главным», что и у карточек
    сообщений/аудитории/охвата/вовлечённости (см. _draw_metrics_section):
    диаграмма рисовала бы тональность только последнего дня, хотя рядом
    «Сообщений» уже показывает сумму за весь период.
    """
    total = int(payload.get("total", 0) or 0)
    return (
        int(payload.get("positive", 0) or 0),
        int(payload.get("neutral", 0) or 0),
        int(payload.get("negative", 0) or 0),
        total,
        bool(total) and not payload.get("sentiment_markup", True),
    )


def _docx_metric(payload: dict[str, Any], key: str) -> str:
    """Метрика в тексте Word: «нет в выгрузке» читается лучше голого прочерка."""
    if metric_missing(payload, key):
        return "нет в выгрузке"
    return format_int(payload.get(key, 0))


def _pdf_metric_cards(payload: dict[str, Any]) -> tuple[list[tuple[str, str, str]], str]:
    """Карточки «Сообщения/Аудитория/Охват/Вовлеченность» для PDF — то же
    правило, что у _draw_metrics_section (PNG): итог по всей выбранной
    области, никогда по последней точке comparison_sequence."""
    cards = [
        ("Сообщения", format_int(payload.get("messages", 0)), ""),
        ("Аудитория", metric_text(payload, "audience"), ""),
        ("Охват", metric_text(payload, "reach"), ""),
        ("Вовлеченность", metric_text(payload, "engagement"), ""),
    ]
    return cards, ""
