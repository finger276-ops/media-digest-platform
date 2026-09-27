# -*- coding: utf-8 -*-
"""Площадки и авторы: где и кто пишет о бренде.

Площадка — блог, канал, сообщество или СМИ, где опубликовано сообщение.
Узнаётся она по адресу (chat_profile), а не по названию: названия совпадают у
разных сообществ и меняются между периодами. Нет адреса — по названию блога,
нет и его — по площадке верхнего уровня (vk.com, Отзовик). Автор — так же:
профиль, затем имя.

Аудитория площадки — число её подписчиков, а не сумма по сообщениям: у поста и
десяти комментариев под ним одна и та же аудитория (см. metrics_compute).
Охват и вовлечённость суммируются по сообщениям. Если метрики нет в выгрузке
или тональность не размечена, колонка заполняется прочерком, а не нулями —
то же правило, что во всём продукте.
"""

from __future__ import annotations

import pandas as pd

from .metrics_compute import (
    NO_METRIC_VALUE,
    has_sentiment_markup,
    metric_known,
    numeric_series,
    sentiment_masks,
)

NO_SOURCE_LABEL = "Без площадки"
NO_AUTHOR_LABEL = "Без автора"
_SOURCE_KEY_COLUMNS = ("chat_profile", "chat_title", "platform")
_SOURCE_LABEL_COLUMNS = ("chat_title", "platform", "chat_profile")
_AUTHOR_KEY_COLUMNS = ("author_profile", "author")
_AUTHOR_LABEL_COLUMNS = ("author", "author_profile")
_EMPTY = {"", "nan", "none", "null", "nat"}


def _first_filled(messages: pd.DataFrame, columns: tuple[str, ...], fallback: str) -> pd.Series:
    result = pd.Series([""] * len(messages), index=messages.index, dtype="object")
    for column in columns:
        if column not in messages.columns:
            continue
        values = messages[column].fillna("").astype(str).str.strip()
        values = values.where(~values.str.lower().isin(_EMPTY), "")
        result = result.where(result != "", values)
    return result.where(result != "", fallback)


def source_keys(messages: pd.DataFrame) -> pd.Series:
    """Ключ площадки: адрес блога, затем название, затем площадка."""
    return _first_filled(messages, _SOURCE_KEY_COLUMNS, NO_SOURCE_LABEL).str.lower()


def author_keys(messages: pd.DataFrame) -> pd.Series:
    return _first_filled(messages, _AUTHOR_KEY_COLUMNS, NO_AUTHOR_LABEL).str.lower()


def _metric(messages: pd.DataFrame, prepared: str, raw: list[str]) -> pd.Series:
    if prepared in messages.columns:
        return pd.to_numeric(messages[prepared], errors="coerce").fillna(0)
    return numeric_series(messages, raw)


def _group_stats(messages: pd.DataFrame, keys: pd.Series, labels: pd.Series) -> pd.DataFrame:
    _, negative = sentiment_masks(messages)
    frame = pd.DataFrame(
        {
            "_key": keys.values,
            "_label": labels.values,
            "_audience": _metric(messages, "_audience", ["audience", "Аудитория"]).values,
            "_reach": _metric(messages, "_reach", ["views", "Просмотры", "Просмотров", "reach", "Охват"]).values,
            "_engagement": _metric(messages, "_engagement", ["engagement", "Вовлечённость", "Вовлеченность"]).values,
            "_negative": negative.astype(bool).values,
            "_author": author_keys(messages).values,
        }
    )
    grouped = frame.groupby("_key", sort=False)
    stats = pd.DataFrame(
        {
            "label": grouped["_label"].agg(lambda s: s.value_counts().index[0]),
            "messages": grouped.size(),
            "authors": grouped["_author"].nunique(),
            # Аудитория площадки — подписчики, одни на все её сообщения.
            "audience": grouped["_audience"].max(),
            "reach": grouped["_reach"].sum(),
            "engagement": grouped["_engagement"].sum(),
            "negative": grouped["_negative"].sum(),
        }
    ).reset_index()
    stats["negative_share"] = (stats["negative"] / stats["messages"]).fillna(0.0)
    return stats.sort_values(["messages", "reach", "engagement"], ascending=False, kind="stable")


def build_source_statistics(messages: pd.DataFrame) -> pd.DataFrame:
    """Площадки выборки: сообщения, авторы, аудитория, охват, вовлечённость, негатив."""
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        return pd.DataFrame()
    stats = _group_stats(
        messages,
        source_keys(messages),
        _first_filled(messages, _SOURCE_LABEL_COLUMNS, NO_SOURCE_LABEL),
    )
    types = _first_filled(messages, ("platform_type", "platform"), "")
    type_by_key = (
        pd.DataFrame({"_key": source_keys(messages).values, "_type": types.values})
        .groupby("_key")["_type"]
        .agg(lambda s: s[s != ""].value_counts().index[0] if (s != "").any() else "")
    )
    stats["type"] = stats["_key"].map(type_by_key).fillna("")
    return stats.reset_index(drop=True)


def build_author_statistics(messages: pd.DataFrame) -> pd.DataFrame:
    """Авторы выборки и площадки, где они пишут чаще всего."""
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        return pd.DataFrame()
    keys = author_keys(messages)
    stats = _group_stats(
        messages,
        keys,
        _first_filled(messages, _AUTHOR_LABEL_COLUMNS, NO_AUTHOR_LABEL),
    )
    places = _first_filled(messages, _SOURCE_LABEL_COLUMNS, NO_SOURCE_LABEL)
    top_places = (
        pd.DataFrame({"_key": keys.values, "_place": places.values})
        .groupby("_key")["_place"]
        .agg(lambda s: ", ".join(s.value_counts().index[:2]))
    )
    stats["places"] = stats["_key"].map(top_places).fillna("")
    return stats.drop(columns=["authors"]).reset_index(drop=True)


def new_sources(current: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    """Площадки текущей выборки, которых не было в прошлом периоде."""
    stats = build_source_statistics(current)
    if stats.empty or not isinstance(previous, pd.DataFrame) or previous.empty:
        return stats.iloc[0:0] if not stats.empty else stats
    seen = set(source_keys(previous))
    return stats[~stats["_key"].isin(seen)].reset_index(drop=True)


def _format_int(value) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def display_table(stats: pd.DataFrame, messages: pd.DataFrame, *, kind: str) -> pd.DataFrame:
    """Таблица для экрана: подписи колонок, прочерки вместо ложных нулей."""
    if stats is None or stats.empty:
        return pd.DataFrame()
    marked = has_sentiment_markup(messages)
    first = "Площадка" if kind == "sources" else "Автор"
    table = pd.DataFrame({first: stats["label"].astype(str)})
    if kind == "sources":
        table["Тип"] = stats.get("type", "")
    else:
        table["Где пишет"] = stats.get("places", "")
    table["Сообщений"] = stats["messages"].map(_format_int)
    if kind == "sources":
        table["Авторов"] = stats["authors"].map(_format_int)
        table["Аудитория"] = (
            stats["audience"].map(_format_int) if metric_known(messages, "audience") else NO_METRIC_VALUE
        )
    table["Охват"] = stats["reach"].map(_format_int) if metric_known(messages, "reach") else NO_METRIC_VALUE
    table["Вовлеченность"] = (
        stats["engagement"].map(_format_int) if metric_known(messages, "engagement") else NO_METRIC_VALUE
    )
    if marked:
        table["Негатив"] = stats["negative"].map(_format_int)
        table["Доля негатива"] = (stats["negative_share"] * 100).round(0).astype(int).astype(str) + "%"
    else:
        table["Негатив"] = NO_METRIC_VALUE
        table["Доля негатива"] = NO_METRIC_VALUE
    return table.reset_index(drop=True)


def messages_of_source(messages: pd.DataFrame, key: str) -> pd.DataFrame:
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        return messages
    return messages[source_keys(messages) == str(key)]


def messages_of_author(messages: pd.DataFrame, key: str) -> pd.DataFrame:
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        return messages
    return messages[author_keys(messages) == str(key)]
