# -*- coding: utf-8 -*-
"""Срез по тегам: весь дашборд — только по сообщениям выбранных тегов.

Выгрузка часто общая на несколько брендов или тем, а отчёт нужен по одному:
выбрали «Технониколь» — и «Обзор», «Источники», «Инфоповоды», «Динамика» и
отчёт считаются только по сообщениям с этим тегом. Прошлый период для
сравнения режется тем же срезом, иначе «+300 %» было бы сравнением среза с
целым периодом.

Не режутся «Индексы бренда»: доля голоса внутри одного бренда всегда 100 %.
У среза свои саммари и тексты ИИ (slice_scope в ключе хранения): их пишут и
правят при включённом срезе, а тексты всего периода остаются нетронутыми.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
import streamlit as st

from services.metrics_compute import format_int
from services.tag_compute import filter_messages_by_tags, normalize_tag_key, tag_options

SLICE_LABEL = "Срез по тегам"
BRAND_INDEX_NOTE = (
    "Индексы бренда посчитаны по всем сообщениям периода: срез по тегам к ним не "
    "применяется — доля голоса внутри одного бренда всегда была бы 100 %."
)


def slice_state_key(project_id: str) -> str:
    return f"tag_slice::{project_id}"


def render_tag_slice(messages: pd.DataFrame, project_id: str) -> list[str]:
    """Выбор тегов среза под «Гранулярностью». Пустой выбор — все сообщения."""
    options = tag_options(messages)
    key = slice_state_key(project_id)
    if not options:
        st.session_state.pop(key, None)
        return []
    counts = dict(options)
    if key in st.session_state:
        # Другой период: тег, которого в выборке нет, снимается, тот же тег в
        # другом написании остаётся выбранным.
        by_key = {normalize_tag_key(tag): tag for tag in counts}
        kept = dict.fromkeys(normalize_tag_key(tag) for tag in st.session_state[key] or [])
        st.session_state[key] = [by_key[k] for k in kept if k in by_key]
    selected = st.multiselect(
        SLICE_LABEL,
        list(counts),
        key=key,
        format_func=lambda tag: f"{tag} · {format_int(counts.get(tag, 0))}",
        placeholder="Все сообщения",
        help=(
            "Все разделы, кроме «Индексов бренда», и отчёт считаются только по "
            "сообщениям с выбранными тегами (с любым из них). Число — сколько "
            "сообщений с тегом в выбранных периодах."
        ),
    )
    return list(selected)


def slice_keys(tags: list[str]) -> tuple[str, ...]:
    """Ключ среза для кешей: без регистра и порядка."""
    return tuple(sorted({normalize_tag_key(tag) for tag in tags or [] if normalize_tag_key(tag)}))


def slice_scope(tags: list[str] | None) -> str:
    """Срез в ключе хранения саммари и текстов ИИ: «tags=технониколь»; без среза — ""."""
    keys = slice_keys(list(tags or []))
    return f"tags={'|'.join(keys)}" if keys else ""


def slice_title(tags: list[str]) -> str:
    """«теги: Технониколь, Кнауф» — для шапки и подписи периода в отчёте."""
    if not tags:
        return ""
    return ("тег: " if len(tags) == 1 else "теги: ") + ", ".join(tags)


def apply_slice(messages: pd.DataFrame | None, tags: list[str]) -> pd.DataFrame | None:
    """Сообщения среза; без среза — как есть."""
    if not tags or messages is None:
        return messages
    return filter_messages_by_tags(messages, tags)


def sliced_loader(
    load: Callable[[str], pd.DataFrame], tags: list[str]
) -> Callable[[str], pd.DataFrame]:
    """Загрузка другого периода тем же срезом — для «прошлого периода» и А/Б."""
    if not tags:
        return load
    return lambda period_id: apply_slice(load(period_id), tags)
