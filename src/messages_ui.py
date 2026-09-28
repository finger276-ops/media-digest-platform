# -*- coding: utf-8 -*-
"""Раздел «Сообщения»: топ-15 по вовлеченности и вся лента с пагинацией.

Если в разделе «Инфоповоды» выбран инфоповод, обе вкладки сужаются до его
сообщений (фильтр из services.event_filter_state); сбросить его можно
кнопкой прямо в этом блоке. Фильтр по тегам сужает обе вкладки так же:
выбран тег «Технониколь» — показаны только сообщения с этим тегом.
Рядом — фильтр «Тип сообщения» (колонка выгрузки: пост, комментарий,
репост). Карточки над лентой показывают, сколько каких типов среди
сообщений с выбранными тегами; фильтр типа сужает саму ленту.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.event_filter_state import (
    clear_selected_event_filter,
    filter_messages_by_selected_event,
    get_selected_event_filter,
)
from metric_cards_ui import metric_card, render_metric_row
from services.formatting import fmt_date
from services.message_kinds import (
    filter_messages_by_type,
    message_type_counts,
    message_type_key,
)
from services.message_compute import message_link_column, message_text_column
from services.metrics_compute import (
    NO_METRIC_VALUE,
    PERIOD_METRIC_COLUMNS,
    format_int,
    numeric_series,
    percent_text,
)
from services.story_recovery import is_residual_title
from services.tag_compute import filter_messages_by_tags, normalize_tag_key, tag_options

MATCH_ANY = "С любым из тегов"
MATCH_ALL = "Со всеми тегами сразу"
TYPE_CARDS = 4


def _value_from_row(row: pd.Series, *columns: str) -> str:
    """Return the first non-empty value from a message row."""
    for col in columns:
        if col in row.index:
            value = str(row.get(col) or "").strip()
            if value and value.lower() not in {"nan", "none", "nat", "null"}:
                return value
    return ""


def render_message_list(
    view: pd.DataFrame, *, text_col: str | None, link_col: str | None
) -> None:
    """Render messages as readable cards instead of a dataframe."""
    if view is None or view.empty:
        st.info("Сообщений для показа нет.")
        return

    for _, row in view.iterrows():
        date_text = fmt_date(row.get("datetime")) if "datetime" in row.index else ""
        source = _value_from_row(
            row, "chat_title", "platform", "source", "Источник", "Место публикации"
        )
        author = _value_from_row(row, "author", "Автор")
        sentiment = _value_from_row(row, "sentiment", "Тональность")
        event_title = _value_from_row(row, "event_title", "source_main_topic", "Сюжет")
        tags = _value_from_row(row, "tags", "Теги").replace("|", ", ")
        audience = int(row.get("_audience", 0) or 0)
        reach = int(row.get("_reach", 0) or 0)
        engagement = int(row.get("_engagement", 0) or 0)
        text = str(row.get(text_col, "") or "").strip() if text_col else ""
        link = str(row.get(link_col, "") or "").strip() if link_col else ""

        meta_parts = [part for part in [date_text, source, author, sentiment] if part]
        # Признак выгрузки-периода: колонки нет — прочерк, а не «охват: 0».
        def _value(key: str, value: int) -> str:
            known = row.get(PERIOD_METRIC_COLUMNS[key], True)
            return format_int(value) if known is None or pd.isna(known) or bool(known) else NO_METRIC_VALUE

        metrics_parts = [
            f"аудитория: {_value('audience', audience)}",
            f"охват: {_value('reach', reach)}",
            f"вовлеченность: {_value('engagement', engagement)}",
        ]

        st.markdown("---")
        if meta_parts:
            st.caption(" · ".join(meta_parts))
        # «Без сюжета» — служебная корзина платформы, а не инфоповод: строка
        # «Инфоповод: Без сюжета» читателю ничего не сообщает.
        if event_title and not is_residual_title(event_title):
            st.markdown(f"**Инфоповод:** {event_title}")
        if tags:
            st.caption(f"Теги: {tags}")
        st.markdown(f"*{' · '.join(metrics_parts)}*")
        st.write(text[:1800] if text else "—")
        if link.startswith("http"):
            st.markdown(f"[Открыть сообщение]({link})")


def _keep_selection(key: str, options: list[str], normalize) -> None:
    """Сверить прошлый выбор с новыми вариантами до того, как нарисован виджет.

    Другой период или инфоповод: значение, которого в выборке нет, снимается,
    а то же значение в другом написании («ТехноНИКОЛЬ») остаётся выбранным.
    """
    if key not in st.session_state:
        return
    by_key = {normalize(option): option for option in options}
    kept = dict.fromkeys(normalize(value) for value in st.session_state[key] or [])
    st.session_state[key] = [by_key[k] for k in kept if k in by_key]


def _tag_filter(work: pd.DataFrame, project_id: str | None) -> tuple[list[str], bool]:
    """Выбор тегов над лентой. Возвращает (выбранные теги, нужны ли все сразу)."""
    options = tag_options(work)
    if not options:
        return [], False
    counts = dict(options)
    key = f"messages_tag_filter_{project_id or 'global'}"
    _keep_selection(key, list(counts), normalize_tag_key)
    selected = st.multiselect(
        "Теги",
        list(counts),
        key=key,
        format_func=lambda tag: f"{tag} · {format_int(counts.get(tag, 0))}",
        placeholder="Все теги",
        help="Показать только сообщения с выбранными тегами. Число — сколько "
        "сообщений с тегом в выборке.",
    )
    match_all = False
    if len(selected) > 1:
        match_all = (
            st.radio(
                "Сообщения",
                [MATCH_ANY, MATCH_ALL],
                horizontal=True,
                key=f"messages_tag_match_{project_id or 'global'}",
            )
            == MATCH_ALL
        )
    return list(selected), match_all


def _type_filter(work: pd.DataFrame, project_id: str | None) -> list[str]:
    """Выбор типов сообщения над лентой: пост, комментарий, репост."""
    options = message_type_counts(work)
    if not options:
        return []
    counts = dict(options)
    key = f"messages_type_filter_{project_id or 'global'}"
    _keep_selection(key, list(counts), message_type_key)
    return list(
        st.multiselect(
            "Тип сообщения",
            list(counts),
            key=key,
            format_func=lambda value: f"{value} · {format_int(counts.get(value, 0))}",
            placeholder="Все типы",
            help="Показать только сообщения выбранных типов — как в колонке «Тип "
            "сообщения» выгрузки. Число — сколько таких сообщений с учётом тегов.",
        )
    )


def _render_message_types(work: pd.DataFrame, selected_types: list[str]) -> None:
    """Сколько среди сообщений выборки постов, комментариев, репостов."""
    counts = message_type_counts(work)
    if not counts:
        st.caption("Разбивки по типу сообщения нет: в выгрузке не указан тип сообщения.")
        return
    total = int(len(work))
    if len(counts) > TYPE_CARDS:
        rest = counts[TYPE_CARDS - 1:]
        counts = counts[: TYPE_CARDS - 1] + [("Другие типы", sum(count for _, count in rest))]
        rest_help = "Остальные типы: " + ", ".join(label for label, _ in rest) + "."
    else:
        rest_help = ""
    st.caption(
        "Сообщений по типам"
        + (f" · в ленте только: {', '.join(selected_types)}" if selected_types else "")
    )
    render_metric_row(
        [
            metric_card(
                label,
                f"{format_int(count)} · {percent_text(count, total)}",
                help_text=rest_help if label == "Другие типы" else "",
            )
            for label, count in counts
        ],
        columns=TYPE_CARDS,
    )


def _tag_scope(tags: list[str], match_all: bool) -> str:
    names = ", ".join(f"«{tag}»" for tag in tags)
    if len(tags) == 1:
        return f"с тегом {names}"
    return f"со всеми тегами {names}" if match_all else f"с любым из тегов {names}"


def _feed_scope(event_filter, tags: list[str], match_all: bool, types: list[str]) -> str:
    """Для чего показан топ: «всей выборки», «сообщений с тегом «Т» и с типом «Пост»»."""
    if not tags and not types:
        return "выбранного инфоповода" if event_filter else "всей выборки"
    parts = [_tag_scope(tags, match_all)] if tags else []
    if types:
        names = ", ".join(f"«{value}»" for value in types)
        parts.append(f"с типом {names}" if len(types) == 1 else f"с типами {names}")
    return "сообщений " + " и ".join(parts) + (" в выбранном инфоповоде" if event_filter else "")


def render_messages_block(
    messages: pd.DataFrame, *, project_id: str | None = None
) -> None:
    """Render key messages and full feed as a readable list.

    If an event was selected in the «Инфоповоды» section, both modes are
    filtered by that event: top messages and the full feed show only messages
    from the selected infopoint.
    """
    # Заголовок нейтрален к режиму: ниже есть переключатель "Ключевые
    # сообщения"/"Вся лента", и если здесь написать "Ключевые сообщения",
    # заголовок будет врать при выбранной "Всей ленте".
    st.subheader("Сообщения")
    if messages is None or messages.empty:
        st.info("Сообщения не найдены.")
        return

    event_filter = get_selected_event_filter(project_id)
    if event_filter:
        c1, c2 = st.columns([4, 1])
        with c1:
            st.info(
                f"Выбран инфоповод: {event_filter.get('title')}. В топе и общей ленте показаны только сообщения этого инфоповода."
            )
        with c2:
            if st.button(
                "Сбросить фильтр",
                key=f"clear_event_message_filter_{project_id or 'global'}",
                width="stretch",
            ):
                clear_selected_event_filter(project_id)
                st.rerun()

    mode = st.radio(
        "Режим просмотра сообщений",
        ["Ключевые сообщения", "Вся лента"],
        horizontal=True,
        key="messages_block_mode",
    )

    work = messages.copy()
    if event_filter:
        work = filter_messages_by_selected_event(work, event_filter)
        if work.empty:
            st.warning(
                "По выбранному инфоповоду сообщения не найдены. Возможно, данные были пересобраны или связи инфоповодов изменились."
            )
            return
    # Порядок отбора: инфоповод → теги → тип. Числа в списке типов и карточки
    # считаются по сообщениям с выбранными тегами.
    has_types = bool(message_type_counts(work))
    tag_col, type_col = st.columns([3, 2]) if has_types else (st.container(), None)
    with tag_col:
        selected_tags, match_all = _tag_filter(work, project_id)
    if selected_tags:
        work = filter_messages_by_tags(work, selected_tags, match_all=match_all)
        if work.empty:
            st.info(
                f"Сообщений {_tag_scope(selected_tags, match_all)} нет. Уберите лишний тег "
                f"или выберите «{MATCH_ANY}»."
            )
            return
    selected_types: list[str] = []
    if type_col is not None:
        with type_col:
            selected_types = _type_filter(work, project_id)
    _render_message_types(work, selected_types)
    if selected_types:
        work = filter_messages_by_type(work, selected_types)
        if work.empty:
            st.info("Сообщений выбранных типов нет. Уберите фильтр типа.")
            return
    text_col = message_text_column(work)
    link_col = message_link_column(work)
    work["_audience"] = numeric_series(work, ["audience", "Аудитория"]).astype(int)
    work["_reach"] = numeric_series(
        work, ["views", "Просмотры", "Просмотров", "reach", "Охват"]
    ).astype(int)
    work["_engagement"] = numeric_series(
        work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]
    ).astype(int)

    if mode == "Ключевые сообщения":
        scope = _feed_scope(event_filter, selected_tags, match_all, selected_types)
        st.caption(
            f"Показано сообщений: {min(15, len(work))} — с максимальной вовлеченностью для {scope}. Если вовлеченность равна 0, дополнительными критериями выступают охват и аудитория."
        )
        view = (
            work.sort_values(["_engagement", "_reach", "_audience"], ascending=False)
            .head(15)
            .copy()
        )
    else:
        search = st.text_input(
            "Поиск по всей ленте",
            placeholder="Введите слово или фразу",
            key="full_feed_search",
        )
        view = work.copy()
        if search.strip() and text_col:
            view = view[
                view[text_col]
                .fillna("")
                .astype(str)
                .str.contains(search.strip(), case=False, regex=False)
            ]
        view = (
            view.sort_values("datetime", ascending=False)
            if "datetime" in view.columns
            else view
        )

        total_found = int(len(view))
        page_size = int(
            st.selectbox(
                "Сообщений на странице",
                [25, 50, 100, 200],
                index=1,
                key="full_feed_page_size",
            )
        )
        total_pages = max(1, (total_found + page_size - 1) // page_size)
        page = int(
            st.number_input(
                "Страница",
                min_value=1,
                max_value=total_pages,
                value=min(int(st.session_state.get("full_feed_page", 1)), total_pages),
                step=1,
                key="full_feed_page",
            )
        )
        start = (page - 1) * page_size
        end = start + page_size
        st.caption(
            f"Найдено сообщений: {format_int(total_found)}. "
            f"Показано: {format_int(start + 1 if total_found else 0)}–{format_int(min(end, total_found))} из {format_int(total_found)}."
        )
        view = view.iloc[start:end].copy()

    render_message_list(view, text_col=text_col, link_col=link_col)
