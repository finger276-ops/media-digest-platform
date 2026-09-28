# -*- coding: utf-8 -*-
"""Аудитория: кто пишет о бренде — пол, возраст и география авторов.

Brand Analytics отдаёт у сообщения пол, возраст, регион и город автора
(колонки «Пол», «Возраст», «Регион», «Город» → author_gender, author_age,
region, city). Заполнены они далеко не у всех: в отчётах заказчику пол
обычно известен у пятой части авторов, возраст — у десятой. Поэтому каждая
цифра здесь идёт вместе с покрытием: у скольких авторов признак известен.

Правила счёта — как в отчётах заказчику:
- пол и возраст — доли среди авторов, у кого признак указан; автор
  учитывается один раз, а не по числу своих сообщений: иначе один активный
  автор перевесил бы сотню остальных;
- география — число упоминаний от авторов из региона или города (сколько
  раз о бренде написали из Москвы), покрытие — доля авторов с известным
  местоположением;
- возраст раскладывается по группам 0–24, 25–34, 35–44, 45–54, 55 и
  старше; Brand Analytics отдаёт его числом («35»), встречаются и диапазоны
  («25-34») — берётся первое число.

Сообщение без автора считается отдельным автором: сложить все анонимные
сообщения в одного «автора» значило бы занизить их вес до одного голоса.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .formatting import plural
from .metrics_compute import format_int
from .source_stats import NO_AUTHOR_LABEL, author_keys

GENDER_COLUMN = "author_gender"
AGE_COLUMN = "author_age"
REGION_COLUMN = "region"
CITY_COLUMN = "city"

MALE = "Мужчины"
FEMALE = "Женщины"
GENDER_ORDER = [MALE, FEMALE]

AGE_GROUPS = [
    ("0–24", 0, 24),
    ("25–34", 25, 34),
    ("35–44", 35, 44),
    ("45–54", 45, 54),
    ("55 и старше", 55, 120),
]
AGE_ORDER = [name for name, _, _ in AGE_GROUPS]
MIN_AGE, MAX_AGE = 10, 100

TOP_PLACES = 10

# Служебные значения, которые означают «не знаем», а не место или пол.
_UNKNOWN = {
    "", "nan", "none", "null", "nat", "-", "—", "–", "?",
    "не определено", "не определён", "не определен", "неизвестно", "не указано",
    "нет данных", "unknown", "n/a", "na",
}
_FIRST_NUMBER = re.compile(r"\d{1,3}")


def _text(messages: pd.DataFrame, column: str) -> pd.Series:
    if column not in messages.columns:
        return pd.Series([""] * len(messages), index=messages.index, dtype="object")
    values = messages[column].fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    return values.where(~values.str.lower().isin(_UNKNOWN), "")


def gender_label(value: object) -> str:
    """«Мужской», «М», «male» → «Мужчины»; «Женский», «Ж», «female» → «Женщины»."""
    text = str(value or "").strip().lower()
    if not text or text in _UNKNOWN:
        return ""
    if text.startswith(("муж", "male", "man")) or text in {"м", "m"}:
        return MALE
    if text.startswith(("жен", "female", "woman")) or text in {"ж", "f", "w"}:
        return FEMALE
    return ""


def age_group(value: object) -> str:
    """Возраст → группа: «35» → «35–44», «25-34» → «25–34», мусор → ""."""
    text = str(value or "").strip().lower()
    if not text or text in _UNKNOWN:
        return ""
    match = _FIRST_NUMBER.search(text)
    if not match:
        return ""
    age = int(match.group())
    # 0 и «150» — заглушки выгрузки, а не возраст автора.
    if not MIN_AGE <= age <= MAX_AGE:
        return ""
    for name, low, high in AGE_GROUPS:
        if low <= age <= high:
            return name
    return ""


def author_ids(messages: pd.DataFrame) -> pd.Series:
    """Автор каждого сообщения; сообщение без автора — отдельный автор."""
    keys = author_keys(messages)
    anonymous = keys == NO_AUTHOR_LABEL.lower()
    if not anonymous.any():
        return keys
    if "message_id" in messages.columns:
        own = "message::" + messages["message_id"].fillna("").astype(str)
        own = own.where(own != "message::", "row::" + messages.index.astype(str))
    else:
        own = pd.Series("row::" + messages.index.astype(str), index=messages.index)
    return keys.where(~anonymous, own)


def _per_author(authors: pd.Series, values: pd.Series) -> pd.Series:
    """Признак автора — самое частое непустое значение в его сообщениях."""
    frame = pd.DataFrame({"author": authors.values, "value": values.values})
    frame = frame[frame["value"] != ""]
    if frame.empty:
        return pd.Series(dtype="object")
    counts = frame.groupby(["author", "value"], sort=False).size().reset_index(name="n")
    # При равенстве — значение по алфавиту: одинаковый ответ при каждом расчёте.
    counts = counts.sort_values(["author", "n", "value"], ascending=[True, False, True])
    return counts.drop_duplicates("author").set_index("author")["value"]


def _groups(per_author: pd.Series, order: list[str]) -> list[dict[str, Any]]:
    known = int(len(per_author))
    counts = per_author.value_counts()
    return [
        {"name": name, "authors": int(counts.get(name, 0)), "share": (int(counts.get(name, 0)) / known) if known else 0.0}
        for name in order
    ]


def _places(values: pd.Series, top: int) -> list[dict[str, Any]]:
    known = values[values != ""]
    if known.empty:
        return []
    counts = known.value_counts()
    # Равные счёты — по алфавиту, чтобы топ не менялся от перерисовки.
    ordered = sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0])))
    total = int(len(known))
    return [{"name": str(name), "messages": int(count), "share": int(count) / total} for name, count in ordered[:top]]


def audience_summary(messages: pd.DataFrame | None, *, top: int = TOP_PLACES) -> dict[str, Any]:
    """Пол, возраст и география авторов выборки — с покрытием каждого признака."""
    empty = {
        "authors": 0,
        "messages": 0,
        "has_data": False,
        "gender": {"known": 0, "share_known": 0.0, "groups": []},
        "age": {"known": 0, "share_known": 0.0, "groups": []},
        "geo": {"known": 0, "share_known": 0.0, "messages_known": 0, "regions_count": 0,
                "cities_count": 0, "regions": [], "cities": []},
    }
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        return empty
    authors = author_ids(messages)
    total_authors = int(authors.nunique())
    gender = _per_author(authors, _text(messages, GENDER_COLUMN).map(gender_label))
    age = _per_author(authors, _text(messages, AGE_COLUMN).map(age_group))
    regions = _text(messages, REGION_COLUMN)
    cities = _text(messages, CITY_COLUMN)
    located = (regions != "") | (cities != "")
    located_authors = int(authors[located].nunique())

    def share(count: int) -> float:
        return count / total_authors if total_authors else 0.0

    result = {
        "authors": total_authors,
        "messages": int(len(messages)),
        "gender": {
            "known": int(len(gender)),
            "share_known": share(len(gender)),
            "groups": _groups(gender, GENDER_ORDER) if len(gender) else [],
        },
        "age": {
            "known": int(len(age)),
            "share_known": share(len(age)),
            "groups": _groups(age, AGE_ORDER) if len(age) else [],
        },
        "geo": {
            "known": located_authors,
            "share_known": share(located_authors),
            "messages_known": int(located.sum()),
            "regions_count": int(regions[regions != ""].nunique()),
            "cities_count": int(cities[cities != ""].nunique()),
            "regions": _places(regions, top),
            "cities": _places(cities, top),
        },
    }
    result["has_data"] = bool(len(gender) or len(age) or located_authors)
    return result


def percent(value: float) -> str:
    """0.224 → «22 %»; ненулевая доля меньше процента — «<1 %», а не «0 %»."""
    pct = float(value or 0.0) * 100
    if 0 < pct < 1:
        return "<1 %"
    return f"{pct:.0f} %"


def leading(groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Самая крупная группа; при равенстве — первая по порядку групп."""
    best = None
    for item in groups or []:
        if item.get("authors", 0) > 0 and (best is None or item["authors"] > best["authors"]):
            best = item
    return best


def mentions_text(count: int) -> str:
    """«1 упоминание», «4 упоминания», «136 упоминаний»."""
    return f"{format_int(count)} {plural(count, 'упоминание', 'упоминания', 'упоминаний')}"


def places_count_text(geo: dict[str, Any]) -> str:
    """Число регионов и городов для оборота «из …»: «1 региона и 21 города», «43 регионов и 96 городов»."""
    regions, cities = int(geo.get("regions_count") or 0), int(geo.get("cities_count") or 0)
    return (f"{format_int(regions)} {plural(regions, 'региона', 'регионов', 'регионов')} и "
            f"{format_int(cities)} {plural(cities, 'города', 'городов', 'городов')}")
