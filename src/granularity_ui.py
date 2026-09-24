# -*- coding: utf-8 -*-
"""Гранулярность метрик/динамики поверх уже загруженных файлов.

Сайдбар «Периоды» (upload_history_ui.render_period_selector) выбирает
ЗАГРУЖЕННЫЕ ФАЙЛЫ — так и остаётся: один импорт = одна загрузка целиком, в
базе нет колонки «день». Этот модуль — второй уровень поверх файлов: любую
загрузку (хоть на 3 дня, хоть на 15) можно раздробить на календарные дни по
собственной дате каждого сообщения, с удобной авто-группировкой по неделям
и месяцам. «Файлы целиком» воспроизводит поведение до этой правки —
сравнение загруженных периодов без дробления по датам.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.metrics_compute import format_int
from services.period_comparison import available_buckets, unresolved_date_count

GRANULARITY_LABELS = {
    "day": "День",
    "week": "Неделя",
    "month": "Месяц",
    "period": "Файлы целиком",
}
GRANULARITY_ORDER = ["day", "week", "month", "period"]


def render_granularity_selector(
    messages: pd.DataFrame, project_id: str, period_ids: list[str]
) -> tuple[str, list[str]]:
    """Рисует контрол «Гранулярность» и возвращает (granularity, selected_bucket_ids).

    По умолчанию — «День», выбраны все доступные дни: это ничего не меняет
    в данных по сравнению с сегодняшним поведением (Обзор/Динамика и так
    уже по дням), просто делает разбивку управляемой и добавляет её в
    экспорт (раньше отчёт был жёстко на уровне файлов).

    Ключ виджетов включает project_id и отсортированный набор period_ids —
    смена файлов в сайдбаре сбрасывает выбор гранулярности на дефолт, а не
    тащит id дней/недель, которых в новой выборке может не быть.
    """
    scope_key = f"{project_id}::{'|'.join(sorted(str(p) for p in period_ids))}"

    st.caption("Гранулярность")
    granularity = st.radio(
        "Гранулярность",
        GRANULARITY_ORDER,
        index=0,
        format_func=lambda g: GRANULARITY_LABELS.get(g, g),
        key=f"granularity_mode::{scope_key}",
        horizontal=True,
        label_visibility="collapsed",
    )

    selected_bucket_ids: list[str] = []
    if granularity != "period":
        buckets = available_buckets(messages, granularity)
        bucket_ids = [str(b["period_id"]) for b in buckets]
        labels = {
            str(b["period_id"]): f"{b['label']} ({format_int(b['messages'])} сообщ.)"
            for b in buckets
        }
        if not bucket_ids:
            st.caption(
                "Нет сообщений с распознанной датой для разбивки по "
                f"{GRANULARITY_LABELS.get(granularity, granularity).lower()}м."
            )
        else:
            with st.expander(
                f"Выбор конкретных периодов (доступно {len(bucket_ids)})",
                expanded=False,
            ):
                selected_bucket_ids = st.multiselect(
                    "Дни/недели/месяцы внутри выбранных файлов",
                    bucket_ids,
                    default=bucket_ids,
                    format_func=lambda x: labels.get(x, x),
                    key=f"granularity_buckets::{scope_key}::{granularity}",
                    label_visibility="collapsed",
                )
        unresolved = unresolved_date_count(messages)
        if unresolved:
            # Когда отмечено всё, фильтр выборку не сужает, и сообщения без
            # даты остаются в итогах — выпадают только из разбивки по точкам.
            narrowed = bool(selected_bucket_ids) and not set(bucket_ids) <= set(
                selected_bucket_ids
            )
            st.warning(
                f"{format_int(unresolved)} сообщений без распознанной даты "
                + (
                    "не входят в выбранные дни/недели/месяцы и в итогах не учтены."
                    if narrowed
                    else "учтены в итогах, но не попадают в разбивку по "
                    "дням/неделям/месяцам."
                )
            )

    return granularity, selected_bucket_ids
