# -*- coding: utf-8 -*-
"""Топ тегов и топ инфоповодов — одна логика на всех три места, где они
показываются: превью «Что включить в отчёт» на «Обзоре» (client_insights_ui),
PNG/DOCX/PDF-экспорт (report_export) и карточка данных для ИИ (ai_summary).

Раньше это были три независимые реализации с разной сортировкой и разным
списком «технических» заголовков («без сюжета» и т.п.) — превью называлось
«Что включить в отчёт», но не гарантировало, что реальный экспорт покажет
тот же набор и в том же порядке (а мог показать и другой набор целиком —
не только другой порядок — при равенстве по числу сообщений). Теперь все
три места вызывают top_report_tags/top_report_events отсюда.

Не трогает services/brand_metrics.py — там свой TECHNICAL_EVENT_TITLES для
расчёта NSS по инфоповодам (basis="events"). Это влияет на число, а не на
то, что показать в отчёте, — сознательно отдельный вопрос от того, что
консолидировано здесь.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .tag_compute import build_tag_statistics_compute

# Объединение того, что раньше было в двух разных местах (Обзор фильтровал
# больше вариантов, чем экспорт) — лучше отфильтровать лишний вариант
# написания, чем пропустить служебный заголовок в клиентский документ.
TECHNICAL_EVENT_TITLES = {
    "без сюжета",
    "без_сюжета",
    "без темы",
    "прочее",
    "прочие сообщения",
    "общее обсуждение",
}

EVENT_TITLE_COLUMNS = [
    "display_title",
    "event_title",
    "title",
    "Сюжет / инфоповод",
    "Сюжет",
]


def is_technical_event_title(title: Any) -> bool:
    """Служебный заголовок-заглушка («без сюжета» и варианты), не тема."""
    value = str(title or "").strip().lower().replace("ё", "е")
    return not value or value in {x.replace("ё", "е") for x in TECHNICAL_EVENT_TITLES}


def event_title_column(events_agg: pd.DataFrame) -> str | None:
    if not isinstance(events_agg, pd.DataFrame):
        return None
    for col in EVENT_TITLE_COLUMNS:
        if col in events_agg.columns:
            return col
    return None


def top_report_tags(messages: pd.DataFrame, limit: int = 5) -> pd.DataFrame:
    """Теги с наибольшим объёмом: сообщения, затем охват, затем вовлечённость.

    Колонки не переименовываются и не урезаются — каждый вызывающий берёт из
    результата то, что ему нужно (превью показывает меньше полей, экспорт
    в PNG/DOCX больше, карточке для ИИ нужен ещё и «Негатив»).
    """
    stats = build_tag_statistics_compute(messages)
    if stats is None or stats.empty:
        return pd.DataFrame()
    work = stats.copy()
    for col in [
        "Сообщений",
        "Аудитория",
        "Охват",
        "Вовлеченность",
        "Негатив",
        "Доля негатива",
    ]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)
    sort_cols = [c for c in ["Сообщений", "Охват", "Вовлеченность"] if c in work.columns]
    if sort_cols:
        work = work.sort_values(sort_cols, ascending=False)
    return work.head(limit).reset_index(drop=True)


def top_report_events(events_agg: pd.DataFrame, limit: int = 5) -> pd.DataFrame:
    """Крупнейшие инфоповоды, без служебных заголовков вроде «Без сюжета».

    Сортировка — по числу сообщений, затем по importance_score (готовая
    оценка значимости инфоповода), а не по охвату: охват отражает размер
    площадок, а не то, насколько инфоповод заметен как история.
    """
    if (
        events_agg is None
        or not isinstance(events_agg, pd.DataFrame)
        or events_agg.empty
    ):
        return pd.DataFrame()
    title_col = event_title_column(events_agg)
    if not title_col:
        return pd.DataFrame()
    work = events_agg.copy()
    work = work[~work[title_col].apply(is_technical_event_title)].copy()
    if work.empty:
        return work
    for col in ["message_count", "negative_count", "importance_score"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)
    sort_cols = [c for c in ["message_count", "importance_score"] if c in work.columns]
    if sort_cols:
        work = work.sort_values(sort_cols, ascending=False)
    return work.head(limit).reset_index(drop=True)
