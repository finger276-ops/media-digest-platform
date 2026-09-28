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
from .message_kinds import message_type_counts, message_type_key
from .period_comparison import metric_delta, pp_delta
from .source_stats import build_source_statistics
from .tag_compute import build_tag_statistics_compute, normalize_tag_key, tag_options

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


def compare_tags(
    messages_a: pd.DataFrame,
    messages_b: pd.DataFrame,
    limit: int = 15,
    *,
    exclude: list[str] | None = None,
) -> pd.DataFrame:
    """Теги с наибольшим изменением числа сообщений от А к Б.

    exclude — теги, которыми выбраны сами А и Б при сравнении брендов: «Кнауф —
    0 в А, 120 в Б» — это определение выборки, а не находка.
    """
    stats_a = build_tag_statistics_compute(messages_a) if isinstance(messages_a, pd.DataFrame) else pd.DataFrame()
    stats_b = build_tag_statistics_compute(messages_b) if isinstance(messages_b, pd.DataFrame) else pd.DataFrame()
    skip = {normalize_tag_key(tag) for tag in exclude or []}
    if skip:
        stats_a = stats_a[~stats_a["Тег"].map(normalize_tag_key).isin(skip)] if not stats_a.empty else stats_a
        stats_b = stats_b[~stats_b["Тег"].map(normalize_tag_key).isin(skip)] if not stats_b.empty else stats_b
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


def default_tag_pair(
    messages: pd.DataFrame, brand_map: dict[str, list[str]] | None = None
) -> tuple[str, str] | None:
    """По умолчанию — свой бренд против первого конкурента из настроек проекта,
    а без настройки — два самых частых тега выборки."""
    options = [label for label, _ in tag_options(messages)]
    if len(options) < 2:
        return None
    by_key = {normalize_tag_key(label): label for label in options}
    own = next((by_key[normalize_tag_key(t)] for t in (brand_map or {}).get("own", []) if normalize_tag_key(t) in by_key), None)
    rival = next(
        (by_key[normalize_tag_key(t)] for t in (brand_map or {}).get("competitors", []) if normalize_tag_key(t) in by_key),
        None,
    )
    if own and rival and own != rival:
        return own, rival
    return options[0], options[1]


def _share_text(count: int, total: int) -> str:
    return f"{format_int(count)} · {count / total * 100:.0f}%" if total else NO_METRIC_VALUE


def compare_type_shares(messages_a: pd.DataFrame, messages_b: pd.DataFrame) -> pd.DataFrame:
    """Типы сообщений в А и Б: «412 · 67%» и разница долей в п.п.

    Нет типа в выгрузке одной из сторон — прочерк у неё и в разнице.
    """
    counts_a, counts_b = message_type_counts(messages_a), message_type_counts(messages_b)
    if not counts_a and not counts_b:
        return pd.DataFrame()
    total_a, total_b = sum(c for _, c in counts_a), sum(c for _, c in counts_b)
    by_a = {message_type_key(label): count for label, count in counts_a}
    by_b = {message_type_key(label): count for label, count in counts_b}
    names: dict[str, str] = {}
    for label, _ in counts_a + counts_b:
        names.setdefault(message_type_key(label), label)
    order = sorted(names, key=lambda key: -(by_a.get(key, 0) + by_b.get(key, 0)))
    rows = []
    for key in order:
        a, b = by_a.get(key, 0), by_b.get(key, 0)
        rows.append(
            {
                "Тип сообщения": names[key],
                "А": _share_text(a, total_a) if counts_a else NO_METRIC_VALUE,
                "Б": _share_text(b, total_b) if counts_b else NO_METRIC_VALUE,
                "Разница доли": (
                    pp_delta(b / total_b, a / total_a) if counts_a and counts_b else NO_METRIC_VALUE
                ),
            }
        )
    return pd.DataFrame(rows)


def compare_platform_shares(
    messages_a: pd.DataFrame, messages_b: pd.DataFrame, limit: int = 10
) -> pd.DataFrame:
    """Площадки А и Б: сообщений и доля у каждой стороны, разница долей в п.п."""
    stats_a, stats_b = build_source_statistics(messages_a), build_source_statistics(messages_b)
    if stats_a.empty and stats_b.empty:
        return pd.DataFrame()
    total_a = int(len(messages_a)) if isinstance(messages_a, pd.DataFrame) else 0
    total_b = int(len(messages_b)) if isinstance(messages_b, pd.DataFrame) else 0
    by_a = dict(zip(stats_a["_key"], stats_a["messages"])) if not stats_a.empty else {}
    by_b = dict(zip(stats_b["_key"], stats_b["messages"])) if not stats_b.empty else {}
    labels = {}
    for stats in (stats_a, stats_b):
        if not stats.empty:
            for key, label in zip(stats["_key"], stats["label"]):
                labels.setdefault(key, str(label))
    order = sorted(labels, key=lambda key: -(by_a.get(key, 0) + by_b.get(key, 0)))[:limit]
    rows = []
    for key in order:
        a, b = int(by_a.get(key, 0)), int(by_b.get(key, 0))
        rows.append(
            {
                "Площадка": labels[key],
                "А": _share_text(a, total_a),
                "Б": _share_text(b, total_b),
                "Разница доли": pp_delta(b / total_b if total_b else 0, a / total_a if total_a else 0),
            }
        )
    return pd.DataFrame(rows)
