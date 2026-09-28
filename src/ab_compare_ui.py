# -*- coding: utf-8 -*-
"""Блок «Сравнить А и Б» в разделе «Динамика»: два периода или два тега.

Два периода: выбор А и Б не зависит от периодов в боковой панели — сравнить
можно любые два периода проекта, например май этого года с маем прошлого.

Два тега (бренда): «Технониколь» против «Кнауф» за периоды из боковой
панели — объём, тональность, площадки, типы сообщений, соседние темы. Срез
по тегам здесь не действует: теги выбираются в самом блоке. Расчёт —
services.ab_compare.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd
import streamlit as st

from services.ab_compare import (
    compare_metrics,
    compare_platform_shares,
    compare_sources,
    compare_tags,
    compare_type_shares,
    comparison_notes,
    default_tag_pair,
)
from services.formatting import period_picker_label
from services.metrics_compute import format_int
from services.period_comparison import ordered_period_ids
from services.tag_compute import filter_messages_by_tags, tag_options

MODE_PERIODS = "Два периода"
MODE_TAGS = "Два тега (бренда)"


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
    *,
    current_messages: pd.DataFrame | None = None,
    brand_map: dict[str, list[str]] | None = None,
) -> None:
    pair = default_pair(periods)
    tag_pair = default_tag_pair(current_messages, brand_map) if current_messages is not None else None
    if pair is None and tag_pair is None:
        return
    st.divider()
    st.markdown("**Сравнить А и Б**")
    modes = [mode for mode, ok in ((MODE_PERIODS, pair), (MODE_TAGS, tag_pair)) if ok]
    mode = (
        st.radio("Что сравнивать", modes, horizontal=True, key=f"ab_mode_{project_id}")
        if len(modes) > 1
        else modes[0]
    )
    if mode == MODE_TAGS:
        _render_tag_comparison(project_id, current_messages, tag_pair)
        return
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


def _render_tag_comparison(
    project_id: str, messages: pd.DataFrame, pair: tuple[str, str]
) -> None:
    """Два тега за периоды из боковой панели — например, свой бренд и конкурент."""
    st.caption(
        "Два тега за периоды, выбранные в боковой панели: например, свой бренд и "
        "конкурент. Срез по тегам здесь не действует — теги выбираются ниже. "
        "Сообщение с обоими тегами считается в обоих. Разница — от А к Б."
    )
    counts = dict(tag_options(messages))
    options = list(counts)
    label = lambda tag: f"{tag} · {format_int(counts.get(tag, 0))}"  # noqa: E731
    left, right = st.columns(2)
    with left:
        tag_a = st.selectbox("Тег А", options, index=options.index(pair[0]), format_func=label,
                             key=f"ab_tag_a_{project_id}")
    with right:
        tag_b = st.selectbox("Тег Б", options, index=options.index(pair[1]), format_func=label,
                             key=f"ab_tag_b_{project_id}")
    if tag_a == tag_b:
        st.info("Выберите два разных тега.")
        return
    messages_a = filter_messages_by_tags(messages, [tag_a])
    messages_b = filter_messages_by_tags(messages, [tag_b])

    table = compare_metrics(messages_a, messages_b).rename(
        columns={"А": tag_a, "Б": tag_b, "Изменение": "Разница Б к А"}
    )
    st.dataframe(table, hide_index=True, width="stretch")
    for note in comparison_notes(messages_a, messages_b):
        st.caption(note.replace("периода А", f"«{tag_a}»").replace("периода Б", f"«{tag_b}»")
                   .replace("периоде А", f"«{tag_a}»").replace("периоде Б", f"«{tag_b}»"))

    names = {"А": tag_a, "Б": tag_b}
    platforms = compare_platform_shares(messages_a, messages_b).rename(columns=names)
    types = compare_type_shares(messages_a, messages_b).rename(columns=names)
    neighbours = compare_tags(messages_a, messages_b, exclude=[tag_a, tag_b]).rename(
        columns={"Сообщений в А": tag_a, "Сообщений в Б": tag_b, "Изменение": "Разница Б к А"}
    )
    tab_platforms, tab_types, tab_tags = st.tabs(["Площадки", "Типы сообщений", "Другие теги"])
    with tab_platforms:
        if platforms.empty:
            st.caption("Площадок нет.")
        else:
            st.caption("Сколько сообщений у каждого тега на площадке и какая это доля всех его сообщений.")
            st.dataframe(platforms, hide_index=True, width="stretch")
    with tab_types:
        if types.empty:
            st.caption("Разбивки по типу сообщения нет: в выгрузке не указан тип сообщения.")
        else:
            st.dataframe(types, hide_index=True, width="stretch")
    with tab_tags:
        if neighbours.empty:
            st.caption("Других тегов у сообщений этих двух тегов нет.")
        else:
            st.caption("Темы, которые чаще встречаются рядом с одним из тегов, чем с другим.")
            st.dataframe(neighbours, hide_index=True, width="stretch")
