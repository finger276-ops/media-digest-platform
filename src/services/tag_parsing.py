"""Разбор и нормализация тегов, меток и тем сообщения.

Вынесено из preprocess.py при распиле монолита. Опирается только на
services.text_cleaning — не знает ни о сообщениях, ни об обсуждениях,
ни о сюжетах.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

import pandas as pd

from settings import TAG_COLUMNS_DEFAULT

from .text_cleaning import normalize_spaces, stable_hash


def detect_tag_columns(df: pd.DataFrame) -> list[str]:
    """Detect boolean tag columns in a source export.

    Earlier versions used only the taxi-specific hardcoded columns. For a
    multi-project platform we keep those columns when they exist, but also
    detect any project-specific binary tag columns exported by monitoring
    systems: "Да/Нет", "true/false", "1/0", "+/-".
    """
    known = [col for col in TAG_COLUMNS_DEFAULT if col in df.columns]
    service_cols = {
        "№",
        "n",
        "id",
        "id сообщения",
        "hash сообщения",
        "дата",
        "время",
        "время публикации",
        "сообщение",
        "текст",
        "текст сообщения",
        "заголовок",
        "ссылка",
        "url",
        "link",
        "источник",
        "площадка",
        "автор",
        "кто пишет",
        "профиль автора",
        "url автора",
        "блог",
        "где пишет",
        "место публикации",
        "профиль блога",
        "url места публикации",
        "тип",
        "тип сообщения",
        "тип источника",
        "тональность",
        "токсичность",
        "wom",
        "страна",
        "регион",
        "город",
        "просмотры",
        "вовлечённость",
        "вовлеченность",
        "лайки",
        "комментарии",
        "репосты",
        "теги",
        "категории",
        "сюжет",
        "основная тема",
        "все темы",
        "все темы (список)",
        "релевантное",
        "source_system",
        "source_file",
        "source_tag_columns",
        "обработано",
        "processed",
    }
    positive_values = {"да", "yes", "true", "1", "+", "истина", "верно"}
    bool_values = positive_values | {
        "нет",
        "no",
        "false",
        "0",
        "-",
        "ложь",
        "неверно",
        "",
    }

    detected = list(known)

    # Brand Analytics exports store tag column names in `source_tag_columns`
    # after canonicalization. Trust this marker: these columns are more reliable
    # topic signals than generic keyword extraction.
    if "source_tag_columns" in df.columns:
        declared: list[str] = []
        for raw in df["source_tag_columns"].dropna().astype(str).unique().tolist():
            for item in re.split(r"[|;,\n]+", raw):
                item = normalize_spaces(item)
                if item and item in df.columns and item not in declared:
                    declared.append(item)
        for col in declared:
            if col not in detected:
                detected.append(col)

    for col in df.columns:
        if col in detected:
            continue
        key = str(col).strip().lower().replace("ё", "е")
        if not key or key in service_cols:
            continue
        sample = (
            df[col]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.lower()
            .replace({"nan": "", "none": ""})
        )
        if sample.empty:
            continue
        non_empty = sample[sample != ""]
        if len(non_empty) < 3:
            continue
        boolish_share = float(sample.isin(bool_values).mean())
        positive_count = int(sample.isin(positive_values).sum())
        if boolish_share >= 0.92 and positive_count > 0:
            detected.append(col)
    return detected


def split_tag_text(value: str) -> list[str]:
    value = normalize_spaces(value)
    if not value or value in {"49", "n/a", "N/A", "нет", "Нет"}:
        return []
    parts = re.split(r"[|;,\n]+", value)
    tags = []
    for part in parts:
        tag = normalize_spaces(part)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def split_source_topics(value: str) -> list[str]:
    value = normalize_spaces(value)
    if not value or value.lower() in {"nan", "none", "null", "нет", "n/a"}:
        return []
    value = value.strip("[]").replace("'", "").replace('"', "")
    parts = re.split(r"[|;,\n]+", value)
    topics = []
    for part in parts:
        topic = normalize_spaces(part)
        if topic and topic not in topics:
            topics.append(topic)
    return topics


def normalize_relevant(value: object, default: bool = True) -> bool:
    s = normalize_spaces(value).lower().replace("ё", "е")
    if not s:
        return default
    if s in {"true", "1", "да", "yes", "+", "истина", "верно", "relevant"}:
        return True
    if s in {
        "false",
        "0",
        "нет",
        "no",
        "-",
        "ложь",
        "неверно",
        "irrelevant",
        "нерелевант",
        "нерелевантное",
    }:
        return False
    return default


def source_topic_bucket_value(value: str) -> str:
    topic = normalize_spaces(value)
    return topic if topic else "без_темы_источника"


GENERIC_EMPTY_LABELS = {
    "",
    "nan",
    "none",
    "null",
    "нет",
    "n/a",
    "не указано",
    "без темы",
    "без тега",
    "прочее",
    "прочие",
    "other",
    "unknown",
    "общие",
    "общая тема",
}


def normalize_label(value: object) -> str:
    """Clean a tag/topic label while preserving the human-readable wording."""
    label = normalize_spaces(value)
    label = label.strip(" .,:;!?'\"«»()[]{}")
    if label.lower().replace("ё", "е") in GENERIC_EMPTY_LABELS:
        return ""
    # Very long analytical strings are usually snippets, not tags.
    if len(label) > 90:
        label = label[:87].rstrip() + "..."
    return label


def unique_labels(values: Iterable[object], limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in split_source_topics(str(value)) or split_tag_text(str(value)):
            label = normalize_label(part)
            key = label.lower().replace("ё", "е")
            if label and key not in seen:
                seen.add(key)
                result.append(label)
                if len(result) >= limit:
                    return result
    return result


def label_microtopic(label: str) -> str:
    """Stable technical bucket for arbitrary project-specific source topics."""
    clean = normalize_label(label)
    if not clean:
        return "general"
    return "label_" + stable_hash(clean.lower().replace("ё", "е"), prefix="")[:8]


# Колонки тем, из которых row_tags добирает метки, когда include_topic_fields.
ROW_TAG_TOPIC_FIELDS = (
    "Основная тема",
    "Все темы",
    "Все темы (список)",
    "Теги",
    "Категории",
    "Сюжет",
)


def row_tags(
    row: pd.Series, tag_cols: list[str], include_topic_fields: bool = True
) -> list[str]:
    """Return source-provided tags/topics for one message.

    Supports two common tag formats:
    1) boolean tag columns: column `Quality` contains Да/True/1;
    2) Brand Analytics tag block: columns after `Обработано` contain the tag
       label itself, for example column `ROCKWOOL` contains `ROCKWOOL`.
    """
    tags: list[str] = []
    seen: set[str] = set()
    positive_values = {"да", "yes", "true", "1", "+", "истина", "верно"}
    negative_values = {
        "",
        "нет",
        "no",
        "false",
        "0",
        "-",
        "ложь",
        "неверно",
        "nan",
        "none",
        "null",
    }

    def add_tag(value: object) -> None:
        label = normalize_label(value)
        key = label.lower().replace("ё", "е")
        if label and key not in seen:
            seen.add(key)
            tags.append(label)

    for tag in tag_cols:
        raw_value = str(row.get(tag, "") or "").strip()
        val = raw_value.lower().replace("ё", "е")
        if val in negative_values:
            continue
        if val in positive_values:
            add_tag(tag)
        else:
            # Brand Analytics and some monitoring systems put the actual tag
            # label into the cell. Prefer it over the column name.
            add_tag(raw_value or tag)

    if include_topic_fields:
        for col in ROW_TAG_TOPIC_FIELDS:
            for tag in unique_labels([row.get(col, "")], limit=12):
                add_tag(tag)

    return tags


def infer_display_tags(text: str, microtopic: str, source_tags: list[str]) -> list[str]:
    """Build display tags without binding the platform to taxi-only dictionaries."""
    tags: list[str] = []
    seen: set[str] = set()
    for tag in source_tags:
        label = normalize_label(tag)
        key = label.lower().replace("ё", "е")
        if label and key not in seen:
            seen.add(key)
            tags.append(label)
        if len(tags) >= 6:
            break

    micro_map = {
        "issue_problem": ["Проблемы, жалобы и негативный опыт"],
        "price_terms": ["Цены, стоимость и условия"],
        "product_quality": ["Качество продукта или услуги"],
        "availability_supply": ["Наличие, поставки и логистика"],
        "installation_usage": ["Монтаж, применение и эксплуатация"],
        "documents_certificates": ["Документы, сертификаты и требования"],
        "safety_fire": ["Безопасность и пожарные свойства"],
        "sustainability_energy": ["Экология и энергоэффективность"],
        "competitors_market": ["Конкуренты и сравнение на рынке"],
        "customer_service": ["Поддержка и клиентский сервис"],
        "general": ["Общие обсуждения"],
    }
    for tag in micro_map.get(str(microtopic or "other"), []):
        key = tag.lower().replace("ё", "е")
        if key not in seen:
            tags.append(tag)
            seen.add(key)
    if not tags:
        tags.append("Прочие обсуждения")
    return tags


def tag_set(value: str) -> set[str]:
    return {x.strip() for x in str(value).split("|") if x.strip()}


def tag_signature(value: str) -> str:
    return "|".join(sorted(tag_set(value)))


def main_tag(tags_series: Iterable[str]) -> str:
    counter: Counter[str] = Counter()
    for tags in tags_series:
        for tag in str(tags).split("|"):
            tag = tag.strip()
            if tag:
                # "яндекс" слишком широкий тег, по возможности уступает более предметным тегам.
                weight = 0.45 if tag == "яндекс" else 1.0
                counter[tag] += weight
    return counter.most_common(1)[0][0] if counter else "Без тега"


def tag_set_from_series(tags_series: Iterable[str]) -> set[str]:
    result = set()
    for tags in tags_series:
        for tag in str(tags).split("|"):
            tag = tag.strip()
            if tag:
                result.add(tag)
    return result
