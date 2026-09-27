from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from .metrics_compute import audience_by_group, numeric_series, sentiment_masks
from .tag_parsing import PLATFORM_RUBRIC_TAG_KEYS

# Автотеги прежних версий платформы — словарь одного заказчика (такси). В
# старых периодах Brand Analytics они могли остаться в колонке tags. Скрываются,
# только если выгрузка их не объявляла: у другого заказчика «Аэропорты» может
# быть настоящим тегом, и прятать его молча нельзя.
LEGACY_AUTO_TAG_KEYS = frozenset(
    {
        "коэффициент",
        "законы и налоги",
        "яндекс",
        "wb такси",
        "фастен",
        "приложение и сбои",
        "яндекс про",
        "забастовка",
        "аэропорты",
        "детские кресла",
        "карты и навигация",
    }
)


def split_pipe_values(value: Any) -> list[str]:
    """Split platform pipe-separated tags into clean unique labels.

    Теги хранятся через «|». Запятая — часть названия, а не разделитель:
    раньше «Проблемы, жалобы и негативный опыт» показывалась клиенту как два
    тега, «Проблемы» и «жалобы и негативный опыт».
    """
    raw = str(value or "").replace(";", "|")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw.split("|"):
        label = " ".join(str(item or "").split()).strip()
        if not label:
            continue
        key = label.lower().replace("ё", "е")
        if key not in seen:
            seen.add(key)
            result.append(label)
    return result


def normalize_tag_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def declared_ba_tag_set(messages: pd.DataFrame) -> set[str]:
    """Return Brand Analytics tag names declared in source_tag_columns."""
    if (
        messages is None
        or messages.empty
        or "source_tag_columns" not in messages.columns
    ):
        return set()
    tags: set[str] = set()
    for raw in messages["source_tag_columns"].dropna().astype(str).unique().tolist():
        for label in split_pipe_values(raw):
            tags.add(normalize_tag_key(label))
    return tags


def is_brand_analytics_messages(messages: pd.DataFrame) -> bool:
    if messages is None or messages.empty:
        return False
    if "source_system" in messages.columns:
        values = messages["source_system"].fillna("").astype(str).str.lower()
        if values.eq("brand_analytics").any():
            return True
    return bool(declared_ba_tag_set(messages))


def clean_display_tags(messages: pd.DataFrame) -> pd.DataFrame:
    """Теги сообщений для показа — только то, что пришло из выгрузки.

    Платформа сама дописывает к тегам рубрики («Цены, стоимость и условия»,
    «Монтаж, применение и эксплуатация», «Прочие обсуждения»…) — они писались
    под одного заказчика и другим выглядели чужим списком. При загрузке они
    остаются (на них опирается сборка сюжетов), а здесь, при чтении, уходят —
    поэтому и уже загруженные периоды чистятся без перезагрузки.

    Тег, который выгрузка объявила сама (колонки Brand Analytics после
    «Обработано»), остаётся всегда, даже если совпал с рубрикой. На Brand
    Analytics с объявленными колонками показываются только они.
    """
    if messages is None or messages.empty or "tags" not in messages.columns:
        return messages

    declared = declared_ba_tag_set(messages)
    brand_analytics = is_brand_analytics_messages(messages)

    def filter_tags(value: str) -> str:
        cleaned: list[str] = []
        seen: set[str] = set()
        for label in split_pipe_values(value):
            key = normalize_tag_key(label)
            if not key or key in seen:
                continue
            if key not in declared:
                if key in PLATFORM_RUBRIC_TAG_KEYS:
                    continue
                if brand_analytics and (declared or key in LEGACY_AUTO_TAG_KEYS):
                    continue
            seen.add(key)
            cleaned.append(label)
        return "|".join(cleaned)

    out = messages.copy()
    raw = out["tags"].fillna("").astype(str)
    # Наборов тегов на порядки меньше, чем сообщений.
    mapping = {value: filter_tags(value) for value in raw.unique().tolist()}
    out["tags"] = raw.map(mapping)
    counts = {value: len(split_pipe_values(value)) for value in mapping.values()}
    out["tag_count"] = out["tags"].map(counts)
    return out


def build_tag_statistics_compute(messages: pd.DataFrame) -> pd.DataFrame:
    """Build tag-level analytics: messages, total views/reach and engagement."""
    if messages is None or messages.empty or "tags" not in messages.columns:
        return pd.DataFrame(
            columns=[
                "Тег",
                "Сообщений",
                "Аудитория",
                "Охват",
                "Вовлеченность",
                "Негатив",
            ]
        )

    work = messages.copy()
    # Общая маска: учитывает флаг is_negative и «negative»/«отриц», а не только
    # «нег». to_numpy — чтобы присваивание не зависело от повторов в индексе.
    work["_negative"] = sentiment_masks(work)[1].astype(int).to_numpy()
    work["_tag"] = work["tags"].fillna("").astype(str).apply(split_pipe_values)
    work = work.explode("_tag")
    work["_tag"] = work["_tag"].fillna("").astype(str).str.strip()
    work = work[work["_tag"] != ""]
    if work.empty:
        return pd.DataFrame(
            columns=[
                "Тег",
                "Сообщений",
                "Аудитория",
                "Охват",
                "Вовлеченность",
                "Негатив",
            ]
        )

    work["_audience"] = numeric_series(work, ["audience", "Аудитория"])
    work["_reach"] = numeric_series(
        work, ["views", "Просмотры", "Просмотров", "reach", "Охват"]
    )
    work["_engagement"] = numeric_series(
        work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]
    )
    stats = (
        work.groupby("_tag", as_index=False)
        .agg(
            Сообщений=(
                ("message_id", "nunique")
                if "message_id" in work.columns
                else ("_tag", "size")
            ),
            Охват=("_reach", "sum"),
            Вовлеченность=("_engagement", "sum"),
            Негатив=("_negative", "sum"),
        )
        .rename(columns={"_tag": "Тег"})
    )
    # Аудитория считается отдельно от остальных: её нельзя складывать по
    # строкам, площадка должна попасть в тег один раз, сколько бы сообщений
    # она с этим тегом ни опубликовала.
    stats["Аудитория"] = (
        stats["Тег"].map(audience_by_group(work, work["_tag"])).fillna(0)
    )
    for col in ["Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"]:
        if col in stats.columns:
            stats[col] = (
                pd.to_numeric(stats[col], errors="coerce").fillna(0).astype(int)
            )
    stats["Доля негатива"] = (
        (stats["Негатив"] / stats["Сообщений"].replace(0, pd.NA) * 100)
        .fillna(0)
        .round(1)
    )
    return stats.sort_values(
        ["Сообщений", "Аудитория", "Охват", "Вовлеченность"], ascending=False
    ).reset_index(drop=True)


@st.cache_data(show_spinner=False, max_entries=6, ttl=600)
def build_tag_statistics(messages: pd.DataFrame) -> pd.DataFrame:
    return build_tag_statistics_compute(messages)
