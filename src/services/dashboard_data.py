"""Подготовка сообщений периодов к расчёту метрик.

Дашборд показывает сообщения не такими, как они лежат в базе: к ним
привязываются инфоповоды, применяются ручные правки аналитика (скрытые
сообщения, переносы между темами, склейки тем), вычищаются служебные теги и
добавляются колонки для расчётов. Любой расчёт, который берёт периоды сверх
выбранных — динамика по всем периодам, изменение к прошлому периоду, — должен
проходить тот же путь. Иначе одинаковые по данным периоды дают разные числа:
NSS не видит инфоповодов, а скрытое аналитиком сообщение продолжает считаться.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.cached_store import cache_version, load_generated_tables
from services.event_enrichment import enrich_messages
from services.manual_moderation import apply_manual_overrides
from services.metrics_compute import prepare_dashboard_messages
from services.tag_compute import clean_brand_analytics_tags


def prepare_period_data(
    project_id: str, period_ids: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Инфоповоды, подготовленные сообщения и состояние ручных правок."""
    events, _discussions, messages, discussion_messages, event_discussions = (
        load_generated_tables(project_id, period_ids)
    )
    enriched = enrich_messages(messages, event_discussions, discussion_messages, events)
    events, enriched, manual_state = apply_manual_overrides(project_id, events, enriched)
    # Brand Analytics: в блоке тегов остаются только системные колонки после
    # «Обработано», без legacy-меток старых алгоритмов.
    enriched = clean_brand_analytics_tags(enriched)
    enriched = prepare_dashboard_messages(enriched)
    return events, enriched, manual_state


def prepare_period_messages(project_id: str, period_ids: list[str]) -> pd.DataFrame:
    """Только сообщения — для расчётов по периодам вне выбранных."""
    _events, messages, _manual_state = prepare_period_data(project_id, period_ids)
    return messages


# Подготовка не бесплатна: пересчёт счётчиков инфоповодов после ручных правок
# на десятках тысяч сообщений занимает секунды. Без кеша динамика «по всем
# периодам» платила бы их на каждом действии в разделе. Записей мало: в кеше
# лежат целые периоды.
@st.cache_data(show_spinner=False, max_entries=3, ttl=900)
def _cached_period_messages(
    project_id: str,
    period_ids_key: tuple[str, ...],
    data_version: int,
    manual_version: int,
) -> pd.DataFrame:
    return prepare_period_messages(project_id, list(period_ids_key))


def cached_period_messages(project_id: str, period_ids: list[str]) -> pd.DataFrame:
    """prepare_period_messages с кешем до смены данных или ручных правок."""
    key = tuple(sorted({str(pid) for pid in (period_ids or []) if str(pid).strip()}))
    if not key:
        return pd.DataFrame()
    return _cached_period_messages(
        str(project_id),
        key,
        cache_version(project_id, "data"),
        cache_version(project_id, "manual"),
    )
