# -*- coding: utf-8 -*-
"""Данные дашборда: подготовка выбранных периодов и метрики прошлого периода
для шапки «Обзора» — с кешем по версиям данных и правок проекта.

Вынесено из app; app реэкспортирует отсюда прежние имена.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.cached_store import cache_version
from services.dashboard_data import cached_period_messages, prepare_period_data
from services.event_enrichment import aggregate_events
from services.metrics_compute import overview_metrics
from services.perf import perf_block
from tag_slice_ui import apply_slice


def _dashboard_data_uncached(
    project_id: str, period_ids: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Загрузить и подготовить данные проекта за выбранные периоды."""
    events, enriched, manual_state = prepare_period_data(project_id, period_ids)
    return events, enriched, aggregate_events(events), manual_state


@st.cache_data(show_spinner=False, max_entries=4, ttl=900)
def _cached_dashboard_data(
    project_id: str,
    period_ids_key: tuple[str, ...],
    data_version: int,
    manual_version: int,
):
    with perf_block(
        "dashboard.prepare_data", project_id=project_id, periods=len(period_ids_key)
    ):
        return _dashboard_data_uncached(project_id, list(period_ids_key))


def load_dashboard_data(
    project_id: str, period_ids: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Кешированная подготовка данных дашборда.

    Ключ кеша — идентификаторы проекта и периодов плюс версии кеша, а не сами
    таблицы. Раньше Streamlit хешировал датафреймы целиком на каждом
    перезапуске страницы, и на больших выгрузках это стоило дороже самого
    расчёта. Промежуточные шаги (обогащение, ручные правки, агрегация
    инфоповодов) больше не кешируются по отдельности: результат считается один
    раз и хранится ограниченным числом записей, чтобы не съедать память.
    """
    key = tuple(sorted(str(pid) for pid in (period_ids or []) if str(pid).strip()))
    if not key:
        empty = pd.DataFrame()
        return empty, empty, empty, {}
    return _cached_dashboard_data(
        str(project_id),
        key,
        cache_version(project_id, "data"),
        cache_version(project_id, "manual"),
    )


@st.cache_data(show_spinner=False, max_entries=6, ttl=900)
def _cached_period_overview(
    project_id: str,
    period_id: str,
    data_version: int,
    manual_version: int,
    tag_keys: tuple[str, ...] = (),
):
    """Метрики прошлого периода для изменений в шапке «Обзора».

    Период готовится так же, как выбранный: раньше он читался сырым, и
    скрытое аналитиком сообщение продолжало считаться в сравнении. Срез по
    тегам — тот же, что у выбранного: срез против целого периода дал бы
    ложное падение.
    """
    messages = apply_slice(cached_period_messages(project_id, [period_id]), list(tag_keys))
    if messages is None or messages.empty:
        return None
    return overview_metrics(messages)


def period_overview_metrics(
    project_id: str, period_id: str | None, tag_keys: tuple[str, ...] = ()
):
    if not project_id or not period_id:
        return None
    try:
        return _cached_period_overview(
            str(project_id),
            str(period_id),
            cache_version(project_id, "data"),
            cache_version(project_id, "manual"),
            tuple(tag_keys),
        )
    except Exception:  # noqa: BLE001 - дельта не критична для страницы
        return None
