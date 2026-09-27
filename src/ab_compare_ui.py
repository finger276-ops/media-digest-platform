# -*- coding: utf-8 -*-
"""Блок «Сравнить два периода» в разделе «Динамика».

Выбор А и Б не зависит от периодов в боковой панели: сравнить можно любые
два периода проекта, например май этого года с маем прошлого. Расчёт —
services.ab_compare.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
import streamlit as st

from services.ab_compare import (
    compare_metrics,
    compare_sources,
    compare_tags,
    comparison_notes,
)
from services.formatting import period_picker_label
from services.period_comparison import ordered_period_ids


def default_pair(periods: pd.DataFrame) -> tuple[str, str] | None:
    """По умолчанию: предпоследний период против последнего."""
    if not isinstance(periods, pd.DataFrame) or periods.empty or "period_id" not in periods.columns:
        return None
    ordered = ordered_period_ids(periods, periods["period_id"].astype(str).tolist())
    if len(ordered) < 2:
        return None
    return str(ordered[-2]), str(ordered[-1])


def render_ab_comparison(
    project_id: str,
    periods: pd.DataFrame,
    load_period_messages: Callable[[str], pd.DataFrame],
) -> None:
    pair = default_pair(periods)
    if pair is None:
        return
    st.divider()
    st.markdown("**Сравнить два периода**")
    st.caption(
        "Любые два периода проекта, без цепочки: например, этот месяц с тем же "
        "месяцем прошлого года или до кампании и после. Изменение считается от А к Б."
    )
    ordered = ordered_period_ids(periods, periods["period_id"].astype(str).tolist())
    rows = {str(row["period_id"]): row for _, row in periods.iterrows()}
    labels = {pid: period_picker_label(rows[pid], pid) for pid in ordered if pid in rows}
    left, right = st.columns(2)
    with left:
        period_a = st.selectbox(
            "Период А",
            ordered,
            index=ordered.index(pair[0]),
            format_func=lambda pid: labels.get(pid, pid),
            key=f"ab_period_a_{project_id}",
        )
    with right:
        period_b = st.selectbox(
            "Период Б",
            ordered,
            index=ordered.index(pair[1]),
            format_func=lambda pid: labels.get(pid, pid),
            key=f"ab_period_b_{project_id}",
        )
    if period_a == period_b:
        st.info("Выберите два разных периода.")
        return
    try:
        messages_a = load_period_messages(period_a)
        messages_b = load_period_messages(period_b)
    except Exception:  # noqa: BLE001 — сбой загрузки не должен ронять «Динамику»
        st.warning("Не удалось загрузить один из периодов. Попробуйте ещё раз.")
        return

    st.dataframe(compare_metrics(messages_a, messages_b), hide_index=True, width="stretch")
    for note in comparison_notes(messages_a, messages_b):
        st.caption(note)

    tags = compare_tags(messages_a, messages_b)
    new_sources, gone_sources = compare_sources(messages_a, messages_b)
    tab_tags, tab_new, tab_gone = st.tabs(
        [
            "Теги: наибольшие изменения",
            f"Новые площадки в Б ({len(new_sources)})",
            f"Пропали после А ({len(gone_sources)})",
        ]
    )
    with tab_tags:
        if tags.empty:
            st.caption("Тегов нет ни в одном из периодов.")
        else:
            st.dataframe(tags, hide_index=True, width="stretch")
    with tab_new:
        if new_sources.empty:
            st.caption("Новых площадок нет: все площадки Б были и в А.")
        else:
            st.dataframe(new_sources, hide_index=True, width="stretch")
    with tab_gone:
        if gone_sources.empty:
            st.caption("Все площадки А есть и в Б.")
        else:
            st.dataframe(gone_sources, hide_index=True, width="stretch")
