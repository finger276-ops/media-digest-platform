# -*- coding: utf-8 -*-
"""Сравнение двух произвольных периодов: А против Б.

«Динамика» сравнивает периоды цепочкой по хронологии — каждый с предыдущим.
Заказчику нужно и другое: «этот май против прошлого мая», «до кампании и
после». Здесь сравниваются ровно два периода, выбранные вручную, в любом
порядке: изменение считается от А к Б.

Правила те же, что везде: метрики, которой нет в выгрузке одного из
периодов, изменение не показывается («−100 %» было бы пропавшей колонкой);
доли тональности сравниваются, только если размечены оба периода.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .metrics_compute import (
    NO_METRIC_VALUE,
    NO_SENTIMENT_LABEL,
    format_int,
    metric_missing,
    metric_text,
    metrics_comparable,
    overview_metrics,
    sentiment_unmarked,
)
from .period_comparison import metric_delta, pp_delta
from .source_stats import build_source_statistics
from .tag_compute import build_tag_statistics_compute

VOLUME_ROWS = (
    ("Сообщений", "messages"),
    ("Аудитория", "audience"),
    ("Охват", "reach"),
    ("Вовлеченность", "engagement"),
)
TONE_ROWS = (("Позитив", "positive"), ("Нейтрал", "neutral"), ("Негатив", "negative"))


def _share(sent: dict[str, Any], key: str) -> float:
    total = int(sent.get("total", 0) or 0)
    return float(sent.get(key, 0) or 0) / total if total else 0.0


def compare_metrics(messages_a: pd.DataFrame, messages_b: pd.DataFrame) -> pd.DataFrame:
    """Таблица «Показатель | А | Б | Изменение» по объёму и тональности."""
    a, b = overview_metrics(messages_a), overview_metrics(messages_b)
    rows: list[dict[str, str]] = []
    for label, key in VOLUME_ROWS:
        if key != "messages" and not metrics_comparable(b, a, key):
            change = NO_METRIC_VALUE
        else:
            change = metric_delta(b.get(key, 0), a.get(key, 0))
        rows.append(
            {
                "Показатель": label,
                "А": format_int(a.get("messages", 0)) if key == "messages" else metric_text(a, key),
                "Б": format_int(b.get("messages", 0)) if key == "messages" else metric_text(b, key),
                "Изменение": change,
            }
        )
    sent_a, sent_b = a.get("sentiment") or {}, b.get("sentiment") or {}
    unmarked_a, unmarked_b = sentiment_unmarked(sent_a), sentiment_unmarked(sent_b)
    for label, key in TONE_ROWS:
        rows.append(
            {
                "Показатель": f"{label}, доля",
                "А": NO_METRIC_VALUE if unmarked_a else f"{_share(sent_a, key) * 100:.0f}%",
                "Б": NO_METRIC_VALUE if unmarked_b else f"{_share(sent_b, key) * 100:.0f}%",
                "Изменение": (
                    NO_METRIC_VALUE
                    if unmarked_a or unmarked_b
                    else pp_delta(_share(sent_b, key), _share(sent_a, key))
                ),
            }
        )
    return pd.DataFrame(rows)


def comparison_notes(messages_a: pd.DataFrame, messages_b: pd.DataFrame) -> list[str]:
    """Почему часть изменений — прочерк."""
    a, b = overview_metrics(messages_a), overview_metrics(messages_b)
    notes = []
    for label, key in VOLUME_ROWS[1:]:
        missing = [name for name, metrics in (("А", a), ("Б", b)) if metric_missing(metrics, key)]
        if missing:
            notes.append(f"{label}: нет в выгрузке периода {' и '.join(missing)}.")
    unmarked = [
        name for name, metrics in (("А", a), ("Б", b)) if sentiment_unmarked(metrics.get("sentiment") or {})
    ]
    if unmarked:
        notes.append(f"Тональность: {NO_SENTIMENT_LABEL} в периоде {' и '.join(unmarked)}.")
    return notes


def compare_tags(messages_a: pd.DataFrame, messages_b: pd.DataFrame, limit: int = 15) -> pd.DataFrame:
    """Теги с наибольшим изменением числа сообщений от А к Б."""
    stats_a = build_tag_statistics_compute(messages_a) if isinstance(messages_a, pd.DataFrame) else pd.DataFrame()
    stats_b = build_tag_statistics_compute(messages_b) if isinstance(messages_b, pd.DataFrame) else pd.DataFrame()
    if stats_a.empty and stats_b.empty:
        return pd.DataFrame()
    left = stats_a[["Тег", "Сообщений"]] if not stats_a.empty else pd.DataFrame(columns=["Тег", "Сообщений"])
    right = stats_b[["Тег", "Сообщений"]] if not stats_b.empty else pd.DataFrame(columns=["Тег", "Сообщений"])
    merged = left.merge(right, on="Тег", how="outer", suffixes=(" А", " Б")).fillna(0)
    merged["Сообщений А"] = merged["Сообщений А"].astype(int)
    merged["Сообщений Б"] = merged["Сообщений Б"].astype(int)
    merged["_delta"] = merged["Сообщений Б"] - merged["Сообщений А"]
    merged = merged.reindex(merged["_delta"].abs().sort_values(ascending=False, kind="stable").index).head(limit)
    return pd.DataFrame(
        {
            "Тег": merged["Тег"].astype(str),
            "Сообщений в А": merged["Сообщений А"].map(format_int),
            "Сообщений в Б": merged["Сообщений Б"].map(format_int),
            "Изменение": [
                metric_delta(b_value, a_value)
                for a_value, b_value in zip(merged["Сообщений А"], merged["Сообщений Б"])
            ],
        }
    ).reset_index(drop=True)


def compare_sources(
    messages_a: pd.DataFrame, messages_b: pd.DataFrame, limit: int = 15
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Площадки, которые появились в Б, и площадки, которые пропали после А."""
    stats_a = build_source_statistics(messages_a)
    stats_b = build_source_statistics(messages_b)

    def _only(stats: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
        if stats.empty:
            return pd.DataFrame(columns=["Площадка", "Сообщений"])
        seen = set(other["_key"]) if not other.empty else set()
        rest = stats[~stats["_key"].isin(seen)].head(limit)
        return pd.DataFrame(
            {"Площадка": rest["label"].astype(str), "Сообщений": rest["messages"].map(format_int)}
        ).reset_index(drop=True)

    return _only(stats_b, stats_a), _only(stats_a, stats_b)
