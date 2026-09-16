# -*- coding: utf-8 -*-
"""Раздел «Инфоповоды»: таблица инфоповодов, карточка выбранного, ручная
модерация (создание/правка/скрытие/объединение) и отчёт по склейке похожих
заголовков.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.cached_store import delete_manual, get_manual, save_manual
from services.event_filter_state import (
    event_series_filter,
    filter_messages_by_selected_event,
    set_selected_event_filter,
)
from services.event_titles import DEFAULT_SIMILARITY, normalize_event_title, preview_merge_levels
from services.formatting import fmt_date
from services.manual_moderation import create_manual_event, event_select_options
from services.message_compute import message_link_column, message_text_column
from services.metrics_compute import (
    format_int,
    numeric_series,
    overview_metrics,
    percent_text,
    sentiment_counts,
)
from services.roles import role_rank
from services.story_recovery import (
    ORIGIN_CLUSTERED,
    ORIGIN_INHERITED,
    ORIGIN_SOURCE,
)
from services.tag_compute import split_pipe_values
from messages_ui import render_message_list


OPEN_COLUMN = "Открыть"


def render_events_table(
    project_id: str,
    show: pd.DataFrame,
    events: pd.DataFrame,
    *,
    can_edit: bool,
) -> pd.Series | None:
    """Таблица инфоповодов: описание правится прямо здесь.

    Описание правилось и раньше, но в свёрнутом блоке под таблицей: выбрать
    строку, прокрутить, раскрыть, сохранить — и так для каждого из двадцати
    инфоповодов дайджеста. В таблице это одна правка на строку.

    Выбор строки — галочкой, а не кликом: редактируемая таблица Streamlit не
    отдаёт выбранную строку, а обычная не даёт править. Из двух ограничений
    выбрано то, где правка возможна: ради неё всё и затевалось.
    """
    state_key = f"events_open_row_{project_id}"
    opened = str(st.session_state.get(state_key) or "")
    keys = [str(k) for k in events.get("group_key", pd.Series(dtype=str))]

    work = show.copy()
    work.insert(0, OPEN_COLUMN, [key == opened for key in keys])

    if not can_edit:
        st.caption("Отметьте инфоповод, чтобы раскрыть его сообщения.")

    edited = st.data_editor(
        work,
        hide_index=True,
        width="stretch",
        key=f"events_editor_{project_id}",
        column_config={
            OPEN_COLUMN: st.column_config.CheckboxColumn(
                OPEN_COLUMN, width="small", help="Раскрыть инфоповод под таблицей."
            ),
            "Описание": st.column_config.TextColumn(
                "Описание",
                width="large",
                help=(
                    "О чём тема. Автоматическое описание собрано из тегов — "
                    "перепишите своими словами, оно попадёт в отчёт."
                    if can_edit
                    else "О чём тема."
                ),
            ),
        },
        disabled=(
            ["Сюжет / инфоповод", "Период", "Сообщений", "Источников", "Негатив", "Важность"]
            if can_edit
            else [c for c in work.columns if c != OPEN_COLUMN]
        ),
    )

    if can_edit:
        _save_edited_descriptions(project_id, show, edited, events)

    # Галочек может оказаться несколько: открываем ту, что поставили сейчас.
    checked = [
        keys[position]
        for position, value in enumerate(edited[OPEN_COLUMN].fillna(False))
        if bool(value) and position < len(keys)
    ]
    new_key = next((key for key in checked if key != opened), "")
    if new_key:
        st.session_state[state_key] = new_key
        st.rerun()
    if not checked and opened:
        st.session_state[state_key] = ""
        st.rerun()
    if not opened:
        return None

    match = events[events["group_key"].astype(str) == opened]
    return match.iloc[0] if not match.empty else None


def _save_edited_descriptions(
    project_id: str, before: pd.DataFrame, after: pd.DataFrame, events: pd.DataFrame
) -> None:
    """Сохранить изменённые описания.

    Сохраняется только то, что изменилось: таблица возвращает весь кадр на
    каждой перерисовке, и запись всех строк подряд давала бы десятки обращений
    к базе на одно нажатие.
    """
    saved = 0
    for position, (old, new) in enumerate(
        zip(before["Описание"].fillna("").astype(str), after["Описание"].fillna("").astype(str))
    ):
        if old.strip() == new.strip() or position >= len(events):
            continue
        row = events.iloc[position]
        # Строка таблицы — это склеенный инфоповод, за ней может стоять
        # несколько исходных событий. Правка распространяется на все, как и в
        # блоке ручной правки под таблицей.
        event_ids = [str(x) for x in (row.get("event_ids") or []) if str(x).strip()]
        for event_id in event_ids:
            row_key = f"event_edit::{event_id}"
            # Запись заменяет payload целиком, поэтому прежние правки нужно
            # перечитать: иначе изменение описания стёрло бы сохранённое
            # название и теги того же инфоповода.
            payload = dict(get_manual(project_id, row_key) or {})
            payload["event_id"] = event_id
            payload["description"] = new.strip()
            try:
                save_manual(project_id, "event_edits", row_key, payload)
                saved += 1
            except Exception:  # noqa: BLE001 — описание не стоит падения раздела
                st.warning("Не удалось сохранить описание инфоповода.")
    if saved:
        st.rerun()


def render_assembly_notice(messages: pd.DataFrame) -> None:
    """Предупредить, что инфоповоды собраны машиной и требуют проверки.

    Часть поводов приходит размеченными из системы мониторинга, часть платформа
    досчитывает сама: наследует по совпадению публикации и группирует похожие.
    У выгрузок без разметки сюжетов — Медиалогия, универсальный формат — весь
    список собран автоматически.

    Автоматика ошибается предсказуемо: склеивает разное, дробит одно, называет
    повод первой попавшейся формулировкой. Аналитик это правит здесь же, но
    сначала должен знать, что правка нужна.
    """
    origins: dict[str, int] = {}
    if isinstance(messages, pd.DataFrame) and "story_origin" in messages.columns:
        counts = messages["story_origin"].fillna("").astype(str).value_counts()
        origins = {str(k): int(v) for k, v in counts.items()}

    from_source = origins.get(ORIGIN_SOURCE, 0)
    assembled = origins.get(ORIGIN_INHERITED, 0) + origins.get(ORIGIN_CLUSTERED, 0)

    st.info(
        "**Инфоповоды собраны автоматически и требуют проверки аналитика.** "
        "Перед отправкой заказчику просмотрите список: названия, состав и "
        "важность правятся вручную — переименовать, объединить или скрыть "
        "инфоповод можно в блоке правки под таблицей."
    )
    if from_source or assembled:
        parts = []
        if from_source:
            parts.append(f"из разметки системы мониторинга — {format_int(from_source)}")
        if assembled:
            parts.append(f"собрано платформой — {format_int(assembled)}")
        st.caption("Сообщений: " + ", ".join(parts) + ".")


def render_residual_events(
    residual_events: pd.DataFrame, messages: pd.DataFrame
) -> None:
    """Показать то, что не собралось в инфоповоды, отдельным блоком.

    Раньше эта корзина была строкой в общей таблице — и первой, потому что вес
    считается от числа сообщений, а их там сотни. Заказчик видел на месте
    главной новости периода мешок из несвязанных публикаций.

    Прятать её совсем нельзя: это половина периода, и человек должен понимать,
    что именно осталось за кадром.
    """
    if residual_events is None or residual_events.empty:
        return

    total = int(
        pd.to_numeric(residual_events.get("message_count", 0), errors="coerce")
        .fillna(0)
        .sum()
    )
    if not total:
        return
    negative = int(
        pd.to_numeric(residual_events.get("negative_count", 0), errors="coerce")
        .fillna(0)
        .sum()
    )

    with st.expander(
        f"Вне инфоповодов — {format_int(total)} сообщений", expanded=False
    ):
        st.caption(
            "Публикации, которые не сложились в общий сюжет: разовые посты, "
            "реклама и обсуждения, о которых написал кто-то один. Это не "
            "инфоповоды, поэтому в рейтинг важности они не попадают."
        )
        if negative:
            st.caption(
                f"Негативных среди них: {format_int(negative)}. "
                "Отзывы о товаре разбираются в своём разделе."
            )
        event_ids: set[str] = set()
        for raw in residual_events.get("event_ids", pd.Series(dtype=object)):
            for value in raw if isinstance(raw, (list, tuple, set)) else []:
                event_ids.add(str(value))
        if event_ids and "event_id" in messages.columns:
            subset = messages[messages["event_id"].astype(str).isin(event_ids)]
            if not subset.empty:
                # Сортируем по вовлечённости: если уж смотреть мешок, то
                # начиная с того, что заметили люди.
                if "engagement" in subset.columns:
                    subset = subset.sort_values(
                        "engagement",
                        key=lambda s: pd.to_numeric(s, errors="coerce").fillna(0),
                        ascending=False,
                    )
                render_message_list(
                    subset.head(50),
                    text_col=message_text_column(subset),
                    link_col=message_link_column(subset),
                )


def _event_tags_text(selected: pd.Series, event_messages: pd.DataFrame) -> str:
    values: list[str] = []
    for col in ["tags", "main_tags", "display_tags", "source_topics"]:
        if col in selected.index:
            values.extend(split_pipe_values(str(selected.get(col) or "")))
    if (
        not values
        and isinstance(event_messages, pd.DataFrame)
        and not event_messages.empty
        and "tags" in event_messages.columns
    ):
        for item in event_messages["tags"].fillna("").astype(str).head(500).tolist():
            values.extend(split_pipe_values(item))
    seen: set[str] = set()
    clean: list[str] = []
    for value in values:
        key = value.strip().lower().replace("ё", "е")
        if value.strip() and key not in seen:
            seen.add(key)
            clean.append(value.strip())
    return ", ".join(clean[:12])


def _event_auto_summary(selected: pd.Series, event_messages: pd.DataFrame) -> str:
    description = str(
        selected.get("description")
        or selected.get("display_description")
        or selected.get("summary")
        or ""
    ).strip()
    if description:
        return description
    title = str(
        selected.get("title") or selected.get("event_title") or "инфоповод"
    ).strip()
    count = (
        int(len(event_messages))
        if isinstance(event_messages, pd.DataFrame)
        else int(selected.get("message_count", 0) or 0)
    )
    negative_count = 0
    if isinstance(event_messages, pd.DataFrame) and not event_messages.empty:
        sent = sentiment_counts(event_messages)
        negative_count = int(sent.get("negative", 0) or 0)
    return f"В теме «{title}» собрано {format_int(count)} сообщений. Негативных сообщений: {format_int(negative_count)}."


def render_selected_event_detail(
    project_id: str, selected: pd.Series, messages: pd.DataFrame
) -> None:
    """Render a unified selected-infopoint card across all project profiles."""
    event_filter = event_series_filter(selected)
    event_messages = filter_messages_by_selected_event(messages, event_filter)
    title = str(
        selected.get("title") or selected.get("event_title") or "Выбранный инфоповод"
    )
    summary = _event_auto_summary(selected, event_messages)
    tags_text = _event_tags_text(selected, event_messages)

    st.markdown(f"## {title}")
    if summary:
        st.info(summary)

    metrics = overview_metrics(
        event_messages if isinstance(event_messages, pd.DataFrame) else pd.DataFrame()
    )
    sent = metrics.get("sentiment", {}) or {}
    total = int(sent.get("total", metrics.get("messages", 0)) or 0)
    chat_count = int(selected.get("chat_count", 0) or 0)
    if (
        not chat_count
        and isinstance(event_messages, pd.DataFrame)
        and not event_messages.empty
    ):
        for col in ["chat_title", "platform", "source", "Источник", "Место публикации"]:
            if col in event_messages.columns:
                chat_count = int(
                    event_messages[col]
                    .fillna("")
                    .astype(str)
                    .replace("", pd.NA)
                    .dropna()
                    .nunique()
                )
                break
    author_count = 0
    if isinstance(event_messages, pd.DataFrame) and not event_messages.empty:
        for col in ["author", "Автор"]:
            if col in event_messages.columns:
                author_count = int(
                    event_messages[col]
                    .fillna("")
                    .astype(str)
                    .replace("", pd.NA)
                    .dropna()
                    .nunique()
                )
                break

    # Карточки в рамках, как в шапке «Обзора»: одинаковые числа должны и
    # выглядеть одинаково, иначе показатели инфоповода читаются как подпись к
    # заголовку, а не как самостоятельная сводка.
    volume_cards = [
        ("Сообщений", format_int(metrics.get("messages", 0))),
        ("Источников/чатов", format_int(chat_count)),
        ("Авторов", format_int(author_count)),
        ("Негатив", percent_text(int(sent.get("negative", 0) or 0), total)),
        ("Важность", str(round(float(selected.get("importance_score", 0) or 0), 2))),
    ]
    for column, (label, value) in zip(st.columns(5), volume_cards):
        with column, st.container(border=True):
            st.metric(label, value)

    scale_cards = [
        ("Аудитория", format_int(metrics.get("audience", 0))),
        ("Охват", format_int(metrics.get("reach", 0))),
        ("Вовлеченность", format_int(metrics.get("engagement", 0))),
    ]
    for column, (label, value) in zip(st.columns(3), scale_cards):
        with column, st.container(border=True):
            st.metric(label, value)

    if tags_text:
        st.caption(f"Теги: {tags_text}")
    if isinstance(event_messages, pd.DataFrame):
        st.caption(
            f"В выбранном инфоповоде найдено сообщений: {format_int(len(event_messages))}."
        )

    mode = st.radio(
        "Сообщения инфоповода",
        ["Ключевые сообщения", "Вся лента"],
        horizontal=True,
        key=f"selected_event_messages_mode_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}",
    )

    if event_messages is None or event_messages.empty:
        st.info("Сообщений по выбранному инфоповоду не найдено.")
        return

    work = event_messages.copy()
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
        st.caption(
            "Показаны топ-15 сообщений выбранного инфоповода по вовлеченности. Если вовлеченность равна 0, учитываются охват и аудитория."
        )
        view = (
            work.sort_values(["_engagement", "_reach", "_audience"], ascending=False)
            .head(15)
            .copy()
        )
    else:
        search_key = f"selected_event_feed_search_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}"
        search = st.text_input(
            "Поиск по ленте инфоповода",
            placeholder="Введите слово или фразу",
            key=search_key,
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
                key=f"selected_event_feed_page_size_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}",
            )
        )
        total_pages = max(1, (total_found + page_size - 1) // page_size)
        page = int(
            st.number_input(
                "Страница",
                min_value=1,
                max_value=total_pages,
                value=1,
                step=1,
                key=f"selected_event_feed_page_{project_id}_{abs(hash(str(event_filter.get('group_key'))))}",
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


def render_title_merge_diagnostics(
    project_id: str, events_agg: pd.DataFrame, threshold: float
) -> None:
    """Померить, сколько инфоповодов схлопывается при разных порогах.

    Вопрос «дробит ли источник одну тему на несколько» решается измерением на
    своих данных, а не на глаз. Расчёт запускается по кнопке: он перебирает
    четыре порога и на больших проектах заметен.
    """
    source = st.session_state.get(f"title_merge_source_{project_id}")
    if source is None or not isinstance(source, pd.DataFrame) or source.empty:
        source = events_agg
    if source is None or source.empty:
        return
    state_key = f"title_merge_preview_{project_id}"
    if st.button(
        "Проверить, сколько заголовков дублируется",
        key=f"title_merge_diag_{project_id}",
        help=(
            "Сравнить число инфоповодов при разных порогах склейки на текущих "
            "данных. Настройки не меняются."
        ),
    ):
        st.session_state[state_key] = preview_merge_levels(
            source, levels=(0.9, 0.75, DEFAULT_SIMILARITY, 0.5)
        )
    preview = st.session_state.get(state_key)
    if preview is None or getattr(preview, "empty", True):
        return
    st.caption(
        f"Без склейки инфоповодов: {len(source)}. "
        f"Текущий порог: {threshold if threshold > 0 else 'выключено'}. "
        "Строки ниже показывают, что было бы при других порогах."
    )
    st.dataframe(preview, hide_index=True, width="stretch")


def render_title_merge_report(
    project_id: str,
    events_agg: pd.DataFrame,
    can_edit: bool,
    manual_state: dict[str, Any] | None = None,
) -> None:
    """Показать, какие заголовки платформа объединила автоматически.

    Автоматическая склейка полезна ровно до тех пор, пока её видно: аналитик
    должен уметь проверить каждое решение и отменить неверное.
    """
    report = st.session_state.get(f"title_merge_report_{project_id}") or []
    threshold = float(
        st.session_state.get(f"title_merge_threshold_{project_id}") or 0.0
    )
    blocked = sorted((manual_state or {}).get("title_merge_blocks") or set())
    if blocked and can_edit:
        with st.expander(f"Заголовки без автосклейки: {len(blocked)}", expanded=False):
            for title in blocked:
                cols = st.columns([8, 2])
                with cols[0]:
                    st.caption(title)
                with cols[1]:
                    if st.button(
                        "Вернуть",
                        key=f"reallow_title_{project_id}_{abs(hash(title))}",
                        width="stretch",
                    ):
                        delete_manual(
                            project_id,
                            f"title_merge_block::{normalize_event_title(title)}",
                        )
                        st.rerun()
    if can_edit:
        render_title_merge_diagnostics(project_id, events_agg, threshold)
    if threshold <= 0:
        return
    if not report:
        st.caption(
            "Похожих заголовков не найдено — каждый инфоповод собран по точному "
            "совпадению заголовка."
        )
        return

    merged_titles = sum(len(item.get("variants") or []) - 1 for item in report)
    with st.expander(
        f"Склеено похожих заголовков: {merged_titles} "
        f"в {len(report)} инфоповодах",
        expanded=False,
    ):
        st.caption(
            "Заголовки ниже платформа сочла разными формулировками одного сюжета. "
            "Если склейка неверна, нажмите «Не склеивать» — заголовок вернётся в "
            "отдельный инфоповод и больше не будет объединяться."
        )
        for index, item in enumerate(report[:40]):
            variants = list(item.get("variants") or [])
            st.markdown(f"**{item.get('title')}** — {item.get('message_count', 0)} сообщ.")
            for variant in variants[1:]:
                cols = st.columns([8, 2]) if can_edit else [st.container()]
                with cols[0]:
                    st.caption(f"↳ {variant}")
                if can_edit:
                    with cols[1]:
                        if st.button(
                            "Не склеивать",
                            key=f"unmerge_title_{project_id}_{index}_{abs(hash(variant))}",
                            width="stretch",
                        ):
                            save_manual(
                                project_id,
                                "title_merge_blocks",
                                f"title_merge_block::{normalize_event_title(variant)}",
                                {"title": variant},
                            )
                            st.success("Заголовок больше не объединяется.")
                            st.rerun()
        if len(report) > 40:
            st.caption(f"…и ещё {len(report) - 40} инфоповодов со склейкой.")


def render_events(
    project_id: str,
    role: str,
    events_agg: pd.DataFrame,
    messages: pd.DataFrame,
    manual_state: dict[str, Any],
) -> None:
    st.subheader("Инфоповоды")
    render_assembly_notice(messages)
    can_edit = role_rank(role) >= role_rank("editor")

    if can_edit:
        with st.expander("Создать инфоповод вручную", expanded=False):
            title = st.text_input(
                "Название нового инфоповода", key="new_manual_event_title"
            )
            description = st.text_area("Описание", key="new_manual_event_description")
            tags = st.text_input("Теги", key="new_manual_event_tags")
            if st.button(
                "Создать инфоповод", type="primary", key="create_manual_event"
            ):
                if not title.strip():
                    st.error("Укажите название инфоповода.")
                else:
                    create_manual_event(project_id, title, description, tags)
                    st.success("Инфоповод создан.")
                    st.rerun()

    if events_agg.empty:
        st.info("Инфоповоды не найдены.")
        return

    render_title_merge_report(project_id, events_agg, can_edit, manual_state)

    word = st.text_input(
        "Фильтр по слову в сообщениях",
        placeholder="Например: доставка, качество, сертификат",
    )
    filtered_events = events_agg.copy()
    filtered_messages = messages.copy()

    text_col = message_text_column(filtered_messages)
    if word.strip() and text_col:
        mask = (
            filtered_messages[text_col]
            .fillna("")
            .astype(str)
            .str.contains(word.strip(), case=False, regex=False)
        )
        filtered_messages = filtered_messages[mask]
        if "event_id" in filtered_messages.columns:
            allowed = set(filtered_messages["event_id"].dropna().astype(str))
            filtered_events = filtered_events[
                filtered_events["event_ids"].apply(
                    lambda ids: bool(set(map(str, ids)) & allowed)
                )
            ]
        st.caption(f"Найдено сообщений: {len(filtered_messages):,}".replace(",", " "))
        msg_view = filtered_messages.copy()
        if not msg_view.empty:
            msg_view["Дата"] = msg_view.get("datetime", "").apply(fmt_date)
            if text_col:
                msg_view["Текст"] = (
                    msg_view[text_col].fillna("").astype(str).str.slice(0, 700)
                )
            link_col = message_link_column(msg_view)
            msg_view["Ссылка"] = (
                msg_view[link_col].fillna("").astype(str) if link_col else ""
            )
            columns = [
                c
                for c in [
                    "Дата",
                    "chat_title",
                    "author",
                    "event_title",
                    "Текст",
                    "Ссылка",
                ]
                if c in msg_view.columns
            ]
            st.dataframe(
                msg_view[columns]
                .rename(
                    columns={
                        "chat_title": "Источник/площадка",
                        "author": "Автор",
                        "event_title": "Инфоповод",
                    }
                )
                .head(500),
                hide_index=True,
                width="stretch",
                column_config={"Ссылка": st.column_config.LinkColumn("Ссылка")},
            )

    # Остаточная корзина — всё, что не собралось в инфоповод, — показывается
    # отдельно от списка. В ней сотни сообщений, и в общей таблице она стояла
    # первой строкой: заказчик видел на месте главной новости мешок.
    residual_events = pd.DataFrame()
    if "is_residual" in filtered_events.columns:
        residual_mask = filtered_events["is_residual"].fillna(False).astype(bool)
        residual_events = filtered_events[residual_mask]
        filtered_events = filtered_events[~residual_mask]

    if filtered_events.empty:
        st.info(
            "За выбранный период не собралось ни одного инфоповода. "
            "Сообщения периода — в блоке ниже и в разделе «Сообщения»."
        )
        render_residual_events(residual_events, messages)
        return

    table = filtered_events.copy()
    table["Период"] = table.apply(
        lambda r: (
            f"{fmt_date(r.get('start_date'))}–{fmt_date(r.get('end_date'))}"
            if fmt_date(r.get("start_date")) != fmt_date(r.get("end_date"))
            else fmt_date(r.get("start_date"))
        ),
        axis=1,
    )
    table["Негатив"] = (table["negative_share"] * 100).round(1).astype(str) + "%"
    if "merged_titles" in table.columns:
        # «+2» рядом с сюжетом означает, что под ним лежат ещё две формулировки
        # заголовка. Подробности — в блоке склейки над таблицей.
        merged_counts = (
            pd.to_numeric(table["merged_titles"], errors="coerce").fillna(0).astype(int)
        )
        table["title"] = [
            f"{title} (+{count})" if count > 0 else title
            for title, count in zip(table["title"].astype(str), merged_counts)
        ]
    show = table[
        [
            "title",
            "description",
            "Период",
            "message_count",
            "chat_count",
            "Негатив",
            "importance_score",
        ]
    ].rename(
        columns={
            "title": "Сюжет / инфоповод",
            "description": "Описание",
            "message_count": "Сообщений",
            "chat_count": "Источников",
            "importance_score": "Важность",
        }
    )
    selected_row = render_events_table(
        project_id, show, filtered_events, can_edit=can_edit
    )

    render_residual_events(residual_events, messages)

    if selected_row is None:
        return

    selected = selected_row
    set_selected_event_filter(project_id, selected)
    selected_ids = set(map(str, selected.get("event_ids", [])))
    render_selected_event_detail(project_id, selected, messages)

    if can_edit:
        with st.expander("Правка выбранного инфоповода", expanded=False):
            new_title = st.text_input(
                "Название",
                value=str(selected.get("title") or ""),
                key=f"edit_title_{selected.get('group_key')}",
            )
            new_desc = st.text_area(
                "Описание",
                value=str(selected.get("description") or ""),
                height=160,
                key=f"edit_desc_{selected.get('group_key')}",
            )
            new_tags = st.text_input(
                "Теги",
                value=str(selected.get("tags") or ""),
                key=f"edit_tags_{selected.get('group_key')}",
            )
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button(
                    "Сохранить правки",
                    key=f"save_event_edit_{selected.get('group_key')}",
                ):
                    for event_id in selected_ids:
                        save_manual(
                            project_id,
                            "event_edits",
                            f"event_edit::{event_id}",
                            {
                                "event_id": event_id,
                                "title": new_title,
                                "description": new_desc,
                                "tags": new_tags,
                                "status": "active",
                            },
                        )
                    st.success("Правки сохранены.")
                    st.rerun()
            with c2:
                if st.button(
                    "Скрыть инфоповод", key=f"hide_event_{selected.get('group_key')}"
                ):
                    for event_id in selected_ids:
                        save_manual(
                            project_id,
                            "event_edits",
                            f"event_edit::{event_id}",
                            {
                                "event_id": event_id,
                                "title": new_title,
                                "description": new_desc,
                                "tags": new_tags,
                                "status": "hidden",
                            },
                        )
                    st.success("Инфоповод скрыт.")
                    st.rerun()
            with c3:
                options = event_select_options(
                    events_agg, exclude_event_ids=selected_ids
                )
                if options:
                    target = st.selectbox(
                        "Объединить с темой",
                        options,
                        format_func=lambda x: x[1],
                        key=f"merge_target_{selected.get('group_key')}",
                    )
                    if st.button(
                        "Объединить", key=f"merge_event_{selected.get('group_key')}"
                    ):
                        target_event_id = target[0]
                        for source_event_id in selected_ids:
                            if source_event_id != target_event_id:
                                save_manual(
                                    project_id,
                                    "event_merges",
                                    f"event_merge::{source_event_id}",
                                    {
                                        "source_event_id": source_event_id,
                                        "target_event_id": target_event_id,
                                    },
                                )
                        st.success("Инфоповоды объединены.")
                        st.rerun()

    st.caption(
        "Этот же инфоповод сохранен как фильтр для общего раздела «Ключевые сообщения / Вся лента»."
    )
