# -*- coding: utf-8 -*-
"""Раздел «Обзор»: шапка проекта, метрики периода, последовательное сравнение
периодов (карточки, графики, сравнительная таблица).

render_project_intro — единый верхний блок для всех профилей проекта:
агрегаты за выбранные периоды (объём, тональность, тип сообщения) плюс
(если выбрано 2+ периодов) цепочка последовательного сравнения через
render_period_comparison_metrics.

Графики (динамика, круговые диаграммы, типы сообщений) — в overview_charts.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from metric_cards_ui import (
    DELTA_INVERSE,
    DELTA_NEUTRAL,
    DELTA_NORMAL,
    metric_card,
    render_metric_row,
)
from messages_ui import TYPE_CARDS, message_type_cards
# Графики вынесены в overview_charts; прежние имена остаются доступны отсюда.
from overview_charts import (  # noqa: F401
    _fill_daily_chart_gaps,
    render_period_comparison_charts,
)
from services.metrics_compute import (
    METRIC_TITLES,
    NO_METRIC_REASON,
    NO_METRIC_VALUE,
    NO_SENTIMENT_LABEL,
    NO_SENTIMENT_REASON,
    PERIOD_MARKUP_COLUMN,
    VOLUME_METRICS,
    format_int,
    metric_missing,
    metric_partly_known,
    metric_text,
    metrics_comparable,
    overview_metrics,
    percent_text,
    sentiment_unmarked,
)
from services.period_comparison import (
    COMPARISON_TABLE_VIEWS,
    TYPES_VIEW,
    build_comparison_metrics,
    build_comparison_table,
    comparison_has_types,
    metric_delta,
    period_coverage_days,
    pp_delta,
    selected_period_label,
)


TONE_CARDS = [("Позитив", "positive"), ("Нейтрал", "neutral"), ("Негатив", "negative")]
# Рост позитива однозначно хорош (обычный зелёный цвет по умолчанию), а вот
# рост негатива однозначно плох — раньше он тоже красился в зелёный, потому
# что цвет карточки не различал метрики. Нейтрал неоднозначен в обе стороны
# (рост нейтрала может значить и что скандал утих, и что бренд перестали
# обсуждать), поэтому его изменение серое, без оценки.
_TONE_DELTA_COLOR = {
    "positive": DELTA_NORMAL,
    "neutral": DELTA_NEUTRAL,
    "negative": DELTA_INVERSE,
}


def _tone_cards(
    sent: dict[str, Any] | None,
    prev_sent: dict[str, Any] | None = None,
    *,
    count_hint: str = "сообщений",
    require_prev_messages: bool = True,
) -> list[dict[str, Any]]:
    """Три карточки тональности с изменением к прошлому периоду.

    Без разметки тональности в выгрузке все сообщения попадают в «нейтрал»,
    и «Нейтрал 100 %, Негатив 0 %» было бы ложным «всё спокойно»: вместо чисел
    прочерк с причиной. Изменение долей показывается, только если размечены
    оба периода: иначе рост негатива «с нуля» — это разметка, появившаяся в
    выгрузке, а не событие.
    """
    sent = sent or {}
    if sentiment_unmarked(sent):
        return [
            metric_card(label, "—", help_text=NO_SENTIMENT_REASON)
            for label, _key in TONE_CARDS
        ]
    total = int(sent.get("total", 0) or 0)
    has_previous = prev_sent is not None
    prev_sent = prev_sent or {}
    prev_total = int(prev_sent.get("total", 0) or 0)
    # Шапка «Обзора» не сравнивает с пустым прошлым периодом, а «Сравнение
    # периодов» сравнивает (у пустой точки доли нулевые) — так было и раньше.
    comparable = (
        has_previous
        and bool(total)
        and (bool(prev_total) or not require_prev_messages)
        and not sentiment_unmarked(prev_sent)
    )

    def _share_delta(key: str) -> str | None:
        if not comparable:
            return None
        prev_share = prev_sent.get(key, 0) / prev_total if prev_total else 0.0
        share = sent.get(key, 0) / total
        # Streamlit считает «без изменений» только строку "0": «0,0 п.п.» он
        # рисует стрелкой вверх, а у негатива с инверсией цвета это красная
        # стрелка «негатив вырос», хотя доля не менялась.
        if round((share - prev_share) * 100, 1) == 0:
            return "0"
        return pp_delta(share, prev_share)

    return [
        metric_card(
            label,
            percent_text(sent.get(key, 0), total),
            delta=_share_delta(key),
            delta_color=_TONE_DELTA_COLOR.get(key, DELTA_NORMAL),
            help_text=f"{format_int(sent.get(key, 0))} {count_hint}",
        )
        for label, key in TONE_CARDS
    ]


VOLUME_CARDS = [
    ("Сообщений", "messages"),
    ("Аудитория", "audience"),
    ("Охват", "reach"),
    ("Вовлеченность", "engagement"),
]


def _volume_cards(
    metrics: dict[str, Any], previous: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Четыре карточки объёма с изменением к прошлому периоду.

    Метрики нет в выгрузке — прочерк с причиной, а не «0». Изменение
    показывается, только если метрика есть в обоих периодах: иначе «−100 %»
    — это пропавшая колонка, а не событие.
    """
    cards = []
    for label, key in VOLUME_CARDS:
        if metric_missing(metrics, key):
            cards.append(metric_card(label, NO_METRIC_VALUE, help_text=NO_METRIC_REASON[key]))
            continue
        delta = None
        if previous and (key == "messages" or metrics_comparable(metrics, previous, key)):
            delta = metric_delta(metrics.get(key, 0), previous.get(key, 0))
        cards.append(metric_card(label, format_int(metrics.get(key, 0)), delta=delta))
    return cards


def _metric_notes(
    messages: pd.DataFrame | None,
    metrics: dict[str, Any] | None,
    previous: dict[str, Any] | None = None,
) -> None:
    """Подписи под карточками: где метрика есть не везде."""
    partly = [
        METRIC_TITLES[key]
        for key in VOLUME_METRICS
        if isinstance(messages, pd.DataFrame)
        and not metric_missing(metrics, key)
        and metric_partly_known(messages, key)
    ]
    if partly:
        st.caption(
            f"В части выбранных периодов нет колонок: {', '.join(partly)} — "
            "сумма посчитана по периодам, где они есть."
        )
    if previous:
        lost = [
            METRIC_TITLES[key]
            for key in VOLUME_METRICS
            if not metric_missing(metrics, key) and metric_missing(previous, key)
        ]
        if lost:
            st.caption(
                f"В прошлом периоде нет колонок: {', '.join(lost)} — изменение не показано."
            )


def _mixed_markup_note(messages: pd.DataFrame, sent: dict[str, Any] | None) -> None:
    """Выбраны размеченные и неразмеченные периоды вместе.

    Итог считается как раньше — неразмеченные сообщения идут в «нейтрал», — но
    доля негатива при этом разбавлена, и об этом нужно сказать.
    """
    if sentiment_unmarked(sent) or not isinstance(messages, pd.DataFrame):
        return
    if PERIOD_MARKUP_COLUMN not in messages.columns:
        return
    flags = messages[PERIOD_MARKUP_COLUMN].dropna().astype(bool)
    if bool(flags.any()) and not bool(flags.all()):
        st.caption(
            "В части выбранных периодов нет разметки тональности — их сообщения "
            "учтены как нейтральные."
        )


def _previous_unmarked_note(sent: dict[str, Any] | None, prev_sent: dict[str, Any] | None) -> None:
    if prev_sent and not sentiment_unmarked(sent) and sentiment_unmarked(prev_sent):
        st.caption(
            "В прошлом периоде нет разметки тональности — изменение долей не показано."
        )


def render_period_comparison_metrics(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    *,
    granularity: str = "day",
    granularity_narrowed: bool = False,
    chart_label_settings: dict[str, Any] | None = None,
    comparison_visible_charts: list[str] | None = None,
) -> dict[str, Any] | None:
    """Render sequential comparison, broken down by the active granularity
    (day/week/month by default; falls back to whole periods when fewer than
    two points come out of it — see build_comparison_metrics).

    granularity_narrowed — в пикере отмечена часть дней: тогда тихие дни на
    график не добавляются вовсе, иначе снятый в пикере день выглядел бы
    нулём (см. _fill_daily_chart_gaps)."""
    aggregate_metrics = build_comparison_metrics(
        messages, periods, period_ids, granularity=granularity
    )
    if aggregate_metrics is None:
        return None
    comparison = aggregate_metrics["comparison_sequence"]
    previous = aggregate_metrics["comparison"]["previous"]
    current = aggregate_metrics["comparison"]["current"]
    first = aggregate_metrics["comparison"]["first"]
    last = aggregate_metrics["comparison"]["last"]
    st.subheader("Сравнение периодов")
    chain_labels = [str(item.get("label", item.get("period_id", ""))) for item in comparison]
    # Дневная разбивка может дать куда больше точек, чем период целиком - без
    # ограничения цепочка из месяца превратилась бы в нечитаемую простыню.
    chain = (
        " → ".join(chain_labels[:10]) + f" → … ещё {len(chain_labels) - 10}"
        if len(chain_labels) > 10
        else " → ".join(chain_labels)
    )
    st.caption("Сравнение идет цепочкой по хронологии: " + chain)

    st.markdown(
        f"**{current['label']}** — к предыдущему периоду: {previous['label']}"
    )
    render_metric_row(_volume_cards(current, previous), columns=4)
    _metric_notes(None, current, previous)

    render_metric_row(
        _tone_cards(
            current.get("sentiment"),
            previous.get("sentiment"),
            count_hint="сообщений в последнем периоде",
            require_prev_messages=False,
        ),
        columns=3,
    )
    _previous_unmarked_note(current.get("sentiment"), previous.get("sentiment"))

    # Пост / комментарий / репост последней точки к предыдущей — те же
    # карточки, что в шапке «Обзора».
    current_types = current.get("message_types") or []
    if current_types:
        render_metric_row(
            message_type_cards(
                current_types,
                int(current.get("messages", 0) or 0),
                previous.get("message_types") or None,
            ),
            columns=TYPE_CARDS,
        )

    render_period_comparison_charts(
        comparison,
        granularity=granularity,
        covered_days=(
            None if granularity_narrowed else period_coverage_days(periods, period_ids)
        ),
        label_settings=chart_label_settings,
        visible_blocks_default=comparison_visible_charts,
    )

    st.markdown("**Сравнительная таблица**")
    # Нет типа ни в одной точке — вида «Типы сообщений» нет: таблица из
    # одних прочерков ничего не сообщает.
    table_views = [
        name
        for name in COMPARISON_TABLE_VIEWS
        if name != TYPES_VIEW or comparison_has_types(comparison)
    ]
    view = st.radio(
        "Показатель",
        table_views,
        index=0,
        horizontal=True,
        key=f"comparison_table_view_{abs(hash(tuple(item.get('period_id', '') for item in comparison)))}",
        label_visibility="collapsed",
    )
    st.dataframe(
        build_comparison_table(comparison, view),
        hide_index=True,
        width="stretch",
    )
    if view == "Все показатели":
        st.caption(
            "Полная таблица шире экрана — её можно прокрутить вбок или выбрать "
            "отдельный показатель."
        )

    if len(comparison) > 2:

        def _overall(key: str) -> str:
            if key != "messages" and not metrics_comparable(last, first, key):
                return "нет в выгрузке"
            return metric_delta(last[key], first[key])

        st.caption(
            f"Итоговая динамика от первого к последнему периоду: "
            f"сообщения — {_overall('messages')}; "
            f"аудитория — {_overall('audience')}; "
            f"охват — {_overall('reach')}; "
            f"вовлеченность — {_overall('engagement')}."
        )

    return aggregate_metrics


def render_period_metrics_line(messages: pd.DataFrame) -> dict[str, Any]:
    """Метрики периода одной строкой — для рабочих разделов.

    Полоса из семи карточек уместна в «Обзоре», где показатели периода и есть
    содержание. В разделах, где аналитик работает с таблицами, она занимает
    треть экрана и отодвигает работу вниз, а те же числа нужны там лишь как
    ориентир: с каким объёмом имеем дело и есть ли негатив.

    Динамика к прошлому периоду сюда не идёт намеренно: со стрелками и
    процентами строка перестаёт читаться с одного взгляда, а за подробностями
    есть «Обзор».
    """
    metrics = overview_metrics(messages)
    sentiment = metrics.get("sentiment") or {}
    total = int(sentiment.get("total", 0))
    parts = [
        f"{format_int(metrics.get('messages', 0))} сообщений",
        f"аудитория {metric_text(metrics, 'audience')}",
        f"охват {metric_text(metrics, 'reach')}",
        f"вовлечённость {metric_text(metrics, 'engagement')}",
    ]
    if total and sentiment_unmarked(sentiment):
        parts.append(NO_SENTIMENT_LABEL)
    elif total:
        parts.append(f"негатив {percent_text(int(sentiment.get('negative', 0)), total)}")
    st.caption(" · ".join(parts))
    return metrics


def render_project_intro(
    project_name: str,
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    *,
    profile_label: str = "",
    granularity: str = "day",
    chart_label_settings: dict[str, Any] | None = None,
    comparison_visible_charts: list[str] | None = None,
    show_comparison: bool = True,
    show_title: bool = True,
    previous_metrics: dict[str, Any] | None = None,
    previous_label: str = "",
    previous_disabled_reason: str = "",
) -> dict[str, Any]:
    """Unified top block for all project profiles.

    If several periods are selected, the top cards show aggregate values for
    the whole selected range. Sequential comparison is rendered below as a
    separate analytical block and does not replace the aggregate overview.
    """
    period_label = selected_period_label(periods, period_ids)
    selected_ids = [x for x in (period_ids or []) if str(x).strip()]
    metrics = overview_metrics(messages)
    sent = metrics["sentiment"]
    total = int(sent.get("total", 0))

    # Заголовок и подпись периода рисуются здесь только в старых вызовах.
    # На главной странице их берёт на себя компактная шапка проекта.
    if show_title:
        st.header(project_name)
        if profile_label:
            st.caption(f"Профиль проекта: {profile_label}")
        st.subheader("Период и основные метрики")
        if len(selected_ids) >= 2:
            st.caption(
                f"Выбрано периодов: {len(selected_ids)} · общие данные по выбранным периодам: {period_label}"
            )
        else:
            st.caption(f"Период: {period_label}")

    previous = previous_metrics or {}
    prev_sent = (previous.get("sentiment") or {}) if previous else {}

    render_metric_row(_volume_cards(metrics, previous or None), columns=4)
    _metric_notes(messages, metrics, previous or None)

    render_metric_row(_tone_cards(sent, prev_sent if previous else None), columns=3)
    _previous_unmarked_note(sent, prev_sent if previous else None)
    _mixed_markup_note(messages, sent)

    # Пост / комментарий / репост. Нет типа в выгрузке — ряда нет: пустая
    # разбивка клиенту ничего не сообщает. Изменение — только если тип был
    # и в прошлом периоде, иначе «+100 п.п.» — это появившаяся колонка.
    message_types = metrics.get("message_types") or []
    if message_types:
        prev_types = (previous.get("message_types") or []) if previous else []
        render_metric_row(
            message_type_cards(message_types, int(metrics.get("messages", 0) or 0), prev_types or None),
            columns=TYPE_CARDS,
        )

    if previous_label:
        st.caption(f"Изменения — к предыдущему периоду: {previous_label}")
    elif previous_disabled_reason:
        st.caption(previous_disabled_reason)

    metrics["period_label"] = period_label
    metrics["project_name"] = project_name

    if (
        show_comparison
        and len(selected_ids) >= 2
        and isinstance(messages, pd.DataFrame)
        and "period_id" in messages.columns
    ):
        st.divider()
        comparison_metrics = render_period_comparison_metrics(
            messages,
            periods,
            period_ids,
            granularity=granularity,
            chart_label_settings=chart_label_settings,
            comparison_visible_charts=comparison_visible_charts,
        )
        if comparison_metrics is not None:
            metrics["comparison_sequence"] = comparison_metrics.get(
                "comparison_sequence"
            )
            metrics["comparison"] = comparison_metrics.get("comparison")

    return metrics
