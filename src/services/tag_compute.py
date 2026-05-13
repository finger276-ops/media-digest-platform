from __future__ import annotations

from typing import Any

import pandas as pd

from .metrics_compute import numeric_series


AUTO_GENERATED_TAGS_TO_HIDE = {
    "коэффициент", "законы и налоги", "яндекс", "wb такси", "фастен",
    "приложение и сбои", "яндекс про", "забастовка", "аэропорты",
    "детские кресла", "карты и навигация",
    "проблемы, жалобы и негативный опыт", "цены, стоимость и условия",
    "качество продукта или услуги", "наличие, поставки и логистика",
    "монтаж, применение и эксплуатация", "документы, сертификаты и требования",
    "безопасность и пожарные свойства", "безопасность и риски",
    "экология и энергоэффективность", "конкуренты и сравнение на рынке",
    "поддержка и клиентский сервис", "общие обсуждения", "прочие обсуждения", "без тега",
}


def split_pipe_values(value: Any) -> list[str]:
    """Split platform pipe-separated tags into clean unique labels."""
    raw = str(value or "").replace(";", "|").replace(",", "|")
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
    if messages is None or messages.empty or "source_tag_columns" not in messages.columns:
        return set()
    tags: set[str] = set()
    for raw in messages["source_tag_columns"].dropna().astype(str).unique().tolist():
        for item in str(raw or "").replace(";", "|").replace(",", "|").split("|"):
            label = " ".join(str(item or "").split()).strip()
            if label:
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


def clean_brand_analytics_tags(messages: pd.DataFrame) -> pd.DataFrame:
    """Keep only real Brand Analytics tags from columns after `Обработано`."""
    if messages is None or messages.empty or "tags" not in messages.columns:
        return messages
    if not is_brand_analytics_messages(messages):
        return messages

    allowed = declared_ba_tag_set(messages)
    out = messages.copy()

    def filter_tags(value: Any) -> str:
        cleaned: list[str] = []
        seen: set[str] = set()
        for label in split_pipe_values(value):
            key = normalize_tag_key(label)
            if not key or key in seen:
                continue
            if key in AUTO_GENERATED_TAGS_TO_HIDE:
                continue
            if allowed and key not in allowed:
                continue
            seen.add(key)
            cleaned.append(label)
        return "|".join(cleaned)

    out["tags"] = out["tags"].apply(filter_tags)
    out["tag_count"] = out["tags"].apply(lambda x: len(split_pipe_values(x)))
    return out


def build_tag_statistics_compute(messages: pd.DataFrame) -> pd.DataFrame:
    """Build tag-level analytics: messages, total views/reach and engagement."""
    if messages is None or messages.empty or "tags" not in messages.columns:
        return pd.DataFrame(columns=["Тег", "Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"])

    work = messages.copy()
    work["_tag"] = work["tags"].fillna("").astype(str).apply(split_pipe_values)
    work = work.explode("_tag")
    work["_tag"] = work["_tag"].fillna("").astype(str).str.strip()
    work = work[work["_tag"] != ""]
    if work.empty:
        return pd.DataFrame(columns=["Тег", "Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"])

    work["_audience"] = numeric_series(work, ["audience", "Аудитория"])
    work["_reach"] = numeric_series(work, ["views", "Просмотры", "Просмотров", "reach", "Охват"])
    work["_engagement"] = numeric_series(work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"])
    if "sentiment" in work.columns:
        work["_negative"] = work["sentiment"].fillna("").astype(str).str.lower().str.contains("нег", regex=True).astype(int)
    else:
        work["_negative"] = 0

    stats = (
        work.groupby("_tag", as_index=False)
        .agg(
            Сообщений=("message_id", "nunique") if "message_id" in work.columns else ("_tag", "size"),
            Аудитория=("_audience", "sum"),
            Охват=("_reach", "sum"),
            Вовлеченность=("_engagement", "sum"),
            Негатив=("_negative", "sum"),
        )
        .rename(columns={"_tag": "Тег"})
    )
    for col in ["Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"]:
        if col in stats.columns:
            stats[col] = pd.to_numeric(stats[col], errors="coerce").fillna(0).astype(int)
    stats["Доля негатива"] = (stats["Негатив"] / stats["Сообщений"].replace(0, pd.NA) * 100).fillna(0).round(1)
    return stats.sort_values(["Сообщений", "Аудитория", "Охват", "Вовлеченность"], ascending=False).reset_index(drop=True)
