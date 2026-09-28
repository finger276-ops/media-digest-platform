# -*- coding: utf-8 -*-
"""«Инфоповоды»: ручная модерация — конфликты правок двух редакторов,
сообщения вне инфоповодов, отчёт о склейке заголовков, отмена правок.

Вынесено из events_ui; events_ui реэкспортирует отсюда прежние имена.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services.cached_store import (
    ManualEditConflict,
    clear_platform_caches,
    delete_manual,
    save_manual,
)
from services.event_titles import DEFAULT_SIMILARITY, normalize_event_title, preview_merge_levels
from services.manual_moderation import (
    event_select_options,
    manual_undo_items,
    manual_versions,
    undo_manual_item,
)
from services.message_compute import message_link_column, message_text_column
from services.metrics_compute import (
    format_int,
)
from services.project_settings import DEMO_MESSAGE


CONFLICT_MESSAGE = (
    "Эту запись только что изменил другой редактор — сохранение отменено, "
    "чтобы не затереть его работу. Раздел показывает свежую версию после "
    "обновления; повторное сохранение запишет ваш текст поверх."
)


def _captured_versions(
    widget_key: str, manual_state: dict[str, Any] | None
) -> dict[str, Any]:
    """Версии правок на момент, когда редактор открыл форму.

    Снимок страницы живёт в кеше с TTL и может незаметно подтянуть чужую
    правку, пока редактор набирает текст, — тогда проверка версии при
    сохранении сравнила бы «свежее со свежим» и пропустила перезапись.
    Поэтому версии замораживаются в session_state вместе с виджетом: пока
    форма открыта, ожидания не меняются.
    """
    store_key = f"manual_versions::{widget_key}"
    if widget_key not in st.session_state or store_key not in st.session_state:
        st.session_state[store_key] = manual_versions(manual_state)
    return st.session_state[store_key]


def _forget_captured_versions(widget_key: str) -> None:
    st.session_state.pop(f"manual_versions::{widget_key}", None)


def _on_manual_conflict(project_id: str, widget_key: str | None = None) -> None:
    """Показать конфликт и сбросить кеши, чтобы перерисовка перечитала базу.

    Замороженные версии тоже сбрасываются: следующее сохранение пойдёт уже от
    свежей версии — это осознанная перезапись, а не случайная.
    """
    st.error(CONFLICT_MESSAGE)
    clear_platform_caches(project_id)
    if widget_key:
        _forget_captured_versions(widget_key)


MOVE_COLUMN = "Отнести к инфоповоду"


MOVE_NONE = "— оставить вне инфоповодов —"


# Дальше этого списка превращается в ленту, которую не разбирают.
RESIDUAL_MESSAGES_SHOWN = 50


def _residual_messages_table(
    project_id: str,
    subset: pd.DataFrame,
    events_agg: pd.DataFrame,
    *,
    can_edit: bool,
    manual_state: dict[str, Any] | None = None,
    read_only: bool = False,
) -> None:
    """Сообщения вне инфоповодов — с возможностью отнести их к теме.

    Автоматика собирает инфоповод там, где о чём-то пишут разные люди. Одиночное
    сообщение по её меркам событием не является, но аналитик видит смысл: пост
    про ту же аварию, тот же запуск, ту же претензию. Отправить его в готовую
    тему одним выбором — дешевле, чем заводить ради него отдельный инфоповод.
    """
    text_col = message_text_column(subset)
    link_col = message_link_column(subset)
    options = [MOVE_NONE] + [label for _, label in event_select_options(events_agg)]
    label_to_event = {
        label: event_id for event_id, label in event_select_options(events_agg)
    }

    view = pd.DataFrame(
        {
            "Дата": subset.get("date", pd.Series([""] * len(subset))).astype(str),
            "Сообщение": (
                subset[text_col].fillna("").astype(str).str.slice(0, 300)
                if text_col
                else ""
            ),
            "Автор": subset.get("author", pd.Series([""] * len(subset))).astype(str),
            "Ссылка": (
                subset[link_col].fillna("").astype(str) if link_col else ""
            ),
            MOVE_COLUMN: MOVE_NONE,
        },
        index=subset.index,
    )

    if not can_edit or len(options) <= 1:
        if len(options) <= 1 and can_edit:
            st.caption(
                "Отнести сообщение некуда: в периоде нет ни одного инфоповода."
            )
        st.dataframe(
            view.drop(columns=[MOVE_COLUMN]),
            hide_index=True,
            width="stretch",
            column_config={
                "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="Открыть")
            },
        )
        return

    if read_only:
        # Столбец переноса оставляем на виду, но запертым: в демо важно, что
        # видно саму возможность отнести сообщение к теме.
        st.caption(
            "Если сообщение относится к одной из тем периода, его можно "
            f"отнести туда последним столбцом. {DEMO_MESSAGE}."
        )
        st.data_editor(
            view,
            hide_index=True,
            width="stretch",
            key=f"residual_moves_{project_id}",
            column_config={
                "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="Открыть"),
                "Сообщение": st.column_config.TextColumn("Сообщение", width="large"),
                MOVE_COLUMN: st.column_config.SelectboxColumn(
                    MOVE_COLUMN, options=options, width="medium", required=False
                ),
            },
            disabled=True,
        )
        return

    st.caption(
        "Если сообщение относится к одной из тем периода, выберите её в "
        "последнем столбце — сообщение уйдёт в этот инфоповод."
    )
    editor_key = f"residual_moves_{project_id}"
    versions = _captured_versions(editor_key, manual_state)
    edited = st.data_editor(
        view,
        hide_index=True,
        width="stretch",
        key=editor_key,
        column_config={
            "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="Открыть"),
            "Сообщение": st.column_config.TextColumn("Сообщение", width="large"),
            MOVE_COLUMN: st.column_config.SelectboxColumn(
                MOVE_COLUMN, options=options, width="medium", required=False
            ),
        },
        disabled=["Дата", "Сообщение", "Автор", "Ссылка"],
    )

    moved = 0
    conflict = False
    message_ids = [str(x) for x in subset.get("message_id", pd.Series(dtype=str))]
    snippets = [" ".join(str(x).split())[:80] for x in view["Сообщение"]]
    titles = _event_title_lookup(events_agg)
    for position, label in enumerate(edited[MOVE_COLUMN].fillna(MOVE_NONE)):
        target = label_to_event.get(str(label))
        if not target or position >= len(message_ids):
            continue
        message_id = message_ids[position]
        if not message_id:
            continue
        row_key = f"message_move::{message_id}"
        try:
            save_manual(
                project_id,
                "message_moves",
                row_key,
                {
                    "message_id": message_id,
                    "target_event_id": target,
                    # Подписи для списка отмены: сообщение и тема могут
                    # оказаться вне выбранных периодов, и тогда их уже не
                    # найти по текущим данным — показывать пришлось бы id.
                    "message_snippet": snippets[position] if position < len(snippets) else "",
                    "target_title": titles.get(str(target), ""),
                },
                # Сообщение видно в списке — значит, в снимке страницы переноса
                # не было. Если запись уже появилась, его перенёс кто-то другой.
                expected_updated_at=versions.get(row_key),
            )
            moved += 1
        except ManualEditConflict:
            conflict = True
        except Exception:  # noqa: BLE001 — перенос не стоит падения раздела
            st.warning("Не удалось перенести сообщение.")
    if conflict:
        _on_manual_conflict(project_id, editor_key)
        # Иначе выбор в столбце повторил бы сохранение на следующей
        # перерисовке — уже без предупреждения.
        try:
            st.session_state.pop(editor_key, None)
        except Exception:  # noqa: BLE001 — сброс виджета не стоит падения
            pass
        return
    if moved:
        _forget_captured_versions(editor_key)
        st.success(f"Перенесено сообщений: {moved}.")
        st.rerun()


def render_residual_events(
    project_id: str,
    residual_events: pd.DataFrame,
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    *,
    can_edit: bool = False,
    manual_state: dict[str, Any] | None = None,
    read_only: bool = False,
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
                shown = subset.head(RESIDUAL_MESSAGES_SHOWN)
                _residual_messages_table(
                    project_id,
                    shown,
                    events_agg,
                    can_edit=can_edit,
                    manual_state=manual_state,
                    read_only=read_only,
                )
                if len(subset) > RESIDUAL_MESSAGES_SHOWN:
                    st.caption(
                        f"Показаны первые {RESIDUAL_MESSAGES_SHOWN} из "
                        f"{format_int(len(subset))} — сначала самые заметные."
                    )


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
    read_only: bool = False,
) -> None:
    """Показать, какие заголовки платформа объединила автоматически.

    Автоматическая склейка полезна ровно до тех пор, пока её видно: аналитик
    должен уметь проверить каждое решение и отменить неверное.
    """
    demo_help = DEMO_MESSAGE if read_only else None
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
                        disabled=read_only,
                        help=demo_help,
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
                            disabled=read_only,
                            help=demo_help,
                        ):
                            row_key = (
                                "title_merge_block::"
                                f"{normalize_event_title(variant)}"
                            )
                            try:
                                save_manual(
                                    project_id,
                                    "title_merge_blocks",
                                    row_key,
                                    {"title": variant},
                                    # Клик мгновенный, формы нет — достаточно
                                    # версии из снимка страницы.
                                    expected_updated_at=manual_versions(
                                        manual_state
                                    ).get(row_key),
                                )
                            except ManualEditConflict:
                                _on_manual_conflict(project_id)
                            else:
                                st.success("Заголовок больше не объединяется.")
                                st.rerun()
        if len(report) > 40:
            st.caption(f"…и ещё {len(report) - 40} инфоповодов со склейкой.")


def _event_title_lookup(events_agg: pd.DataFrame) -> dict[str, str]:
    """event_id → заголовок строки таблицы, куда он попал после склейки."""
    lookup: dict[str, str] = {}
    if not isinstance(events_agg, pd.DataFrame) or events_agg.empty:
        return lookup
    for _, row in events_agg.iterrows():
        title = str(row.get("title") or "").strip()
        for event_id in row.get("event_ids", []) or []:
            if title:
                lookup[str(event_id)] = title
    return lookup


def render_manual_undo(
    project_id: str,
    events_agg: pd.DataFrame,
    messages: pd.DataFrame,
    manual_state: dict[str, Any] | None,
    *,
    read_only: bool = False,
) -> None:
    """Список ручных правок с кнопкой «Отменить».

    Стоит до проверки «инфоповодов нет»: если аналитик по ошибке скрыл все
    темы периода, вернуть их надо именно отсюда.
    """
    texts: dict[str, str] = {}
    if isinstance(messages, pd.DataFrame) and not messages.empty and "message_id" in messages.columns:
        text_col = message_text_column(messages)
        if text_col:
            texts = dict(
                zip(
                    messages["message_id"].astype(str),
                    messages[text_col].fillna("").astype(str),
                )
            )
    titles = _event_title_lookup(events_agg)
    items = manual_undo_items(manual_state, titles, texts)
    if not items:
        return
    demo_help = DEMO_MESSAGE if read_only else None
    with st.expander(f"Ручные правки инфоповодов: {len(items)}", expanded=False):
        st.caption(
            "Скрытые темы, объединения и перенесённые сообщения. Правка действует "
            "для всех пользователей проекта — здесь её можно отменить."
        )
        for item in items:
            cols = st.columns([8, 2])
            with cols[0]:
                st.caption(item["label"])
            with cols[1]:
                if st.button(
                    "Отменить",
                    key=f"undo_{project_id}_{item['key']}",
                    width="stretch",
                    disabled=read_only,
                    help=demo_help,
                ):
                    # save_manual/delete_manual сами сбрасывают кеш правок и
                    # данных — как у «Вернуть» для автосклейки.
                    undo_manual_item(project_id, item, manual_state, titles)
                    st.rerun()
