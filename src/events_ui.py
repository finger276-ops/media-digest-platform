# -*- coding: utf-8 -*-
"""Раздел «Инфоповоды»: таблица инфоповодов, карточка выбранного, ручная
модерация (создание/правка/скрытие/объединение) и отчёт по склейке похожих
заголовков.

Карточка выбранного инфоповода — в events_detail_ui, ручная модерация
(конфликты правок, сообщения вне инфоповодов, склейка, отмена) — в
events_manual_ui.
"""

from __future__ import annotations

import uuid
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from services.cached_store import (
    ManualEditConflict,
    get_manual,
    save_manual,
)
from services.event_filter_state import set_selected_event_filter
from services.formatting import fmt_date
from services.manual_moderation import (
    create_manual_event,
    edit_target_ids,
    event_select_options,
    hide_payloads,
    merge_payloads,
)
from services.message_compute import message_link_column, message_text_column
from services.chart_style import CATEGORICAL_PALETTE
from services.metrics_compute import (
    NO_SENTIMENT_REASON,
    format_int,
    has_sentiment_markup,
)
from services.project_settings import DEMO_MESSAGE
from services.roles import role_rank
from services.story_recovery import (
    ORIGIN_CLUSTERED,
    ORIGIN_INHERITED,
    ORIGIN_SOURCE,
)
# Карточка инфоповода и ручная модерация вынесены в свои модули; прежние
# имена остаются доступны отсюда.
from events_detail_ui import render_selected_event_detail
from events_manual_ui import (  # noqa: F401
    CONFLICT_MESSAGE,
    _captured_versions,
    _event_title_lookup,
    _forget_captured_versions,
    _on_manual_conflict,
    render_manual_undo,
    render_residual_events,
    render_title_merge_diagnostics,
    render_title_merge_report,
)


OPEN_COLUMN = "Открыть"


def render_events_table(
    project_id: str,
    show: pd.DataFrame,
    events: pd.DataFrame,
    *,
    can_edit: bool,
    manual_state: dict[str, Any] | None = None,
    read_only: bool = False,
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
    editor_key = f"events_editor_{project_id}"
    # В демо столбец описания остаётся на виду — гость должен видеть, что тема
    # правится руками, — но запись выключена. Поэтому «показывать как
    # аналитику» и «разрешать запись» здесь разные условия.
    can_write = can_edit and not read_only
    # До создания виджета: версии должны быть заморожены в тот же момент,
    # когда таблица впервые показана редактору.
    versions = _captured_versions(editor_key, manual_state) if can_write else {}

    work = show.copy()
    work.insert(0, OPEN_COLUMN, [key == opened for key in keys])

    if read_only:
        st.caption(f"Описание правится прямо в таблице. {DEMO_MESSAGE}.")
    elif not can_edit:
        st.caption("Отметьте инфоповод, чтобы раскрыть его сообщения.")

    edited = st.data_editor(
        work,
        hide_index=True,
        width="stretch",
        key=editor_key,
        column_config={
            OPEN_COLUMN: st.column_config.CheckboxColumn(
                OPEN_COLUMN, width="small", help="Раскрыть инфоповод под таблицей."
            ),
            "Описание": st.column_config.TextColumn(
                "Описание",
                width="large",
                help=(
                    "О чём тема. Автоматическое описание собрано из тегов — "
                    "перепишите своими словами: его увидят все, кто открывает "
                    "этот раздел. В отчёт Word/PDF/PNG описания не входят."
                    if can_edit
                    else "О чём тема."
                ),
            ),
        },
        disabled=(
            ["Сюжет / инфоповод", "Период", "Сообщений", "Источников", "Негатив", "Важность"]
            if can_write
            else [c for c in work.columns if c != OPEN_COLUMN]
        ),
    )

    if can_write:
        _save_edited_descriptions(
            project_id, show, edited, events, versions=versions, editor_key=editor_key
        )

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
    project_id: str,
    before: pd.DataFrame,
    after: pd.DataFrame,
    events: pd.DataFrame,
    *,
    versions: dict[str, Any],
    editor_key: str,
) -> None:
    """Сохранить изменённые описания.

    Сохраняется только то, что изменилось: таблица возвращает весь кадр на
    каждой перерисовке, и запись всех строк подряд давала бы десятки обращений
    к базе на одно нажатие.
    """
    saved = 0
    conflict = False
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
                save_manual(
                    project_id,
                    "event_edits",
                    row_key,
                    payload,
                    expected_updated_at=versions.get(row_key),
                )
                saved += 1
            except ManualEditConflict:
                conflict = True
            except Exception:  # noqa: BLE001 — описание не стоит падения раздела
                st.warning("Не удалось сохранить описание инфоповода.")
    if conflict:
        _on_manual_conflict(project_id, editor_key)
        # Несохранённая правка остаётся в состоянии таблицы и на следующей
        # перерисовке ушла бы в базу уже без предупреждения — от свежей
        # версии. Сбрасываем состояние: таблица покажет то, что в базе.
        try:
            st.session_state.pop(editor_key, None)
        except Exception:  # noqa: BLE001 — сброс виджета не стоит падения
            pass
        return
    if saved:
        _forget_captured_versions(editor_key)
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


# Меньше трёх столбцов — график рядом с таблицей на пару строк не даёт
# ничего, кроме лишнего скролла: сама таблица уже читается с одного взгляда.
TOP_EVENTS_CHART_MIN_ROWS = 3
TOP_EVENTS_CHART_MAX_ROWS = 10


def _render_top_events_chart(events: pd.DataFrame, *, tone_ok: bool = True) -> None:
    """Топ инфоповодов по важности — горизонтальный бар, цвет — доля негатива.

    Таблица ниже даёт точные числа по каждому инфоповоду, а этот график —
    ответ на вопрос с одного взгляда: что было главным в периоде и где из
    этого главного был негатив. Обе величины уже посчитаны в aggregate_events,
    здесь только отрисовка.

    tone_ok=False — в выгрузке нет разметки тональности: все столбцы были бы
    окрашены в «0 % негатива», поэтому цвет один и без шкалы негатива.
    """
    top = events.head(TOP_EVENTS_CHART_MAX_ROWS).copy()
    if len(top) < TOP_EVENTS_CHART_MIN_ROWS:
        return
    top["Сюжет / инфоповод"] = top["title"].astype(str).str.slice(0, 70)
    top["Сообщений"] = pd.to_numeric(top["message_count"], errors="coerce").fillna(0)
    top["Источников"] = pd.to_numeric(top["chat_count"], errors="coerce").fillna(0).astype(int)
    top["Важность"] = pd.to_numeric(top["importance_score"], errors="coerce").fillna(0)
    top["Доля негатива"] = (
        pd.to_numeric(top.get("negative_share", 0), errors="coerce").fillna(0)
    )

    if not tone_ok:
        chart = (
            alt.Chart(top)
            .mark_bar(color=CATEGORICAL_PALETTE[0])
            .encode(
                x=alt.X("Важность:Q", title="Важность"),
                y=alt.Y(
                    "Сюжет / инфоповод:N",
                    sort=alt.EncodingSortField(field="Важность", order="descending"),
                    title=None,
                    axis=alt.Axis(labelLimit=260),
                ),
                tooltip=[
                    alt.Tooltip("Сюжет / инфоповод:N", title="Инфоповод"),
                    alt.Tooltip("Сообщений:Q", format=","),
                    alt.Tooltip("Источников:Q"),
                    alt.Tooltip("Важность:Q", format=".1f"),
                ],
            )
            .properties(height=alt.Step(28))
        )
        st.altair_chart(chart, width="stretch")
        return

    chart = (
        alt.Chart(top)
        .mark_bar()
        .encode(
            x=alt.X("Важность:Q", title="Важность"),
            y=alt.Y(
                "Сюжет / инфоповод:N",
                sort=alt.EncodingSortField(field="Важность", order="descending"),
                title=None,
                axis=alt.Axis(labelLimit=260),
            ),
            # Доля негатива — величина, а не категория: один оттенок от
            # светлого к тёмному, не радуга. Домен зафиксирован 0..1, а не по
            # данным периода — иначе одинаковая доля в разных периодах
            # красилась бы разным цветом и графики нельзя было бы сравнивать.
            color=alt.Color(
                "Доля негатива:Q",
                title="Доля негатива",
                scale=alt.Scale(scheme="reds", domain=[0, 1]),
                legend=alt.Legend(format=".0%"),
            ),
            tooltip=[
                alt.Tooltip("Сюжет / инфоповод:N", title="Инфоповод"),
                alt.Tooltip("Сообщений:Q", format=","),
                alt.Tooltip("Источников:Q"),
                alt.Tooltip("Доля негатива:Q", format=".0%"),
                alt.Tooltip("Важность:Q", format=".1f"),
            ],
        )
        .properties(height=alt.Step(28))
    )
    st.altair_chart(chart, width="stretch")


def render_events(
    project_id: str,
    role: str,
    events_agg: pd.DataFrame,
    messages: pd.DataFrame,
    manual_state: dict[str, Any],
    read_only: bool = False,
) -> None:
    st.subheader("Инфоповоды")
    # can_edit решает, показывать ли блоки правки; read_only — разрешать ли
    # запись. В демо первое остаётся истиной, второе нет.
    can_edit = role_rank(role) >= role_rank("editor")
    # «Требуют проверки аналитика» и откуда собраны сообщения — рабочие
    # заметки аналитика. Заказчику, в том числе в клиентском предпросмотре,
    # они говорят только то, что список ещё никто не проверял.
    if can_edit:
        render_assembly_notice(messages)
    can_write = can_edit and not read_only
    demo_help = DEMO_MESSAGE if read_only else None

    if can_edit:
        with st.expander("Создать инфоповод вручную", expanded=False):
            title = st.text_input(
                "Название нового инфоповода", key="new_manual_event_title"
            )
            description = st.text_area("Описание", key="new_manual_event_description")
            tags = st.text_input("Теги", key="new_manual_event_tags")
            if st.button(
                "Создать инфоповод",
                type="primary",
                key="create_manual_event",
                disabled=read_only,
                help=demo_help,
            ):
                if not title.strip():
                    st.error("Укажите название инфоповода.")
                else:
                    create_manual_event(project_id, title, description, tags)
                    st.success("Инфоповод создан.")
                    st.rerun()
        render_manual_undo(
            project_id, events_agg, messages, manual_state, read_only=read_only
        )

    if events_agg.empty:
        st.info("Инфоповоды не найдены.")
        return

    # Какие заголовки платформа склеила — проверка её решений, то есть работа
    # аналитика. Заказчик видит уже итог.
    if can_edit:
        render_title_merge_report(
            project_id, events_agg, can_edit, manual_state, read_only=read_only
        )

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
        # Переносить некуда: инфоповодов в периоде нет вовсе.
        render_residual_events(
            project_id, residual_events, messages, filtered_events, can_edit=False
        )
        return

    # Признак — по сообщениям всего раздела, до фильтра по слову: фильтр
    # сужает выборку, но разметки в выгрузке от этого не становится меньше.
    tone_ok = has_sentiment_markup(messages)
    if not tone_ok:
        st.caption(f"Доля негатива по инфоповодам не показана. {NO_SENTIMENT_REASON}")
    _render_top_events_chart(filtered_events, tone_ok=tone_ok)

    table = filtered_events.copy()
    table["Период"] = table.apply(
        lambda r: (
            f"{fmt_date(r.get('start_date'))}–{fmt_date(r.get('end_date'))}"
            if fmt_date(r.get("start_date")) != fmt_date(r.get("end_date"))
            else fmt_date(r.get("start_date"))
        ),
        axis=1,
    )
    table["Негатив"] = (
        (table["negative_share"] * 100).round(1).astype(str) + "%" if tone_ok else "—"
    )
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
        project_id,
        show,
        filtered_events,
        can_edit=can_edit,
        manual_state=manual_state,
        read_only=read_only,
    )

    render_residual_events(
        project_id,
        residual_events,
        messages,
        filtered_events,
        can_edit=can_edit,
        manual_state=manual_state,
        read_only=read_only,
    )

    if selected_row is None:
        return

    selected = selected_row
    set_selected_event_filter(project_id, selected)
    selected_ids = set(map(str, selected.get("event_ids", [])))
    render_selected_event_detail(project_id, selected, messages)

    if can_edit:
        with st.expander("Правка выбранного инфоповода", expanded=False):
            # Версии замораживаются вместе с первым полем формы: пока аналитик
            # пишет, ожидания не подтянут чужую правку из освежившегося кеша.
            form_key = f"edit_title_{selected.get('group_key')}"
            versions = _captured_versions(form_key, manual_state)
            new_title = st.text_input(
                "Название",
                value=str(selected.get("title") or ""),
                key=form_key,
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
                    disabled=read_only,
                    help=demo_help,
                ):
                    try:
                        for event_id in edit_target_ids(selected_ids, manual_state):
                            row_key = f"event_edit::{event_id}"
                            save_manual(
                                project_id,
                                "event_edits",
                                row_key,
                                {
                                    "event_id": event_id,
                                    "title": new_title,
                                    "description": new_desc,
                                    "tags": new_tags,
                                    "status": "active",
                                },
                                expected_updated_at=versions.get(row_key),
                            )
                    except ManualEditConflict:
                        _on_manual_conflict(project_id, form_key)
                    else:
                        _forget_captured_versions(form_key)
                        st.success("Правки сохранены.")
                        st.rerun()
            with c2:
                if st.button(
                    "Скрыть инфоповод",
                    key=f"hide_event_{selected.get('group_key')}",
                    disabled=read_only,
                    help=demo_help,
                ):
                    try:
                        payloads = hide_payloads(
                            selected_ids,
                            manual_state,
                            label=str(selected.get("title") or ""),
                            op_id=uuid.uuid4().hex[:12],
                        )
                        for event_id, payload in payloads.items():
                            row_key = f"event_edit::{event_id}"
                            save_manual(
                                project_id,
                                "event_edits",
                                row_key,
                                payload,
                                expected_updated_at=versions.get(row_key),
                            )
                    except ManualEditConflict:
                        _on_manual_conflict(project_id, form_key)
                    else:
                        _forget_captured_versions(form_key)
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
                        "Объединить",
                        key=f"merge_event_{selected.get('group_key')}",
                        disabled=read_only,
                        help=demo_help,
                    ):
                        target_event_id = target[0]
                        try:
                            payloads = merge_payloads(
                                selected_ids,
                                target_event_id,
                                op_id=uuid.uuid4().hex[:12],
                                source_title=str(selected.get("title") or ""),
                                target_title=_event_title_lookup(events_agg).get(
                                    str(target_event_id), ""
                                ),
                            )
                            for source_event_id, payload in payloads.items():
                                row_key = f"event_merge::{source_event_id}"
                                save_manual(
                                    project_id,
                                    "event_merges",
                                    row_key,
                                    payload,
                                    expected_updated_at=versions.get(row_key),
                                )
                        except ManualEditConflict:
                            _on_manual_conflict(project_id, form_key)
                        else:
                            _forget_captured_versions(form_key)
                            st.success("Инфоповоды объединены.")
                            st.rerun()

    st.caption(
        "Этот же инфоповод сохранен как фильтр для общего раздела «Ключевые сообщения / Вся лента»."
    )
