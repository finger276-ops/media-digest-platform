# -*- coding: utf-8 -*-
"""Шапка дашборда: логотип и название проекта, панель «⚙️ Вид», метрики
периода над разделом.

Вынесено из app.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import streamlit as st

from dashboard_loader import period_overview_metrics
from error_ui import show_error
from overview_ui import render_period_metrics_line, render_project_intro
from services.cached_store import clear_platform_caches, load_storage_file, update_project
from services.period_comparison import previous_period_id
from services.project_settings import default_min_event_messages
from services.roles import role_rank
from sidebar_ui import render_min_event_messages_control, render_title_merge_control
from tag_slice_ui import slice_keys

# Имя логгера — прежнее, из app: события шапки идут в тот же канал.
LOGGER = logging.getLogger("platform.app")


def project_logo_bytes(project_id: str, branding: dict[str, Any] | None) -> bytes:
    """Логотип проекта для шапки — тот же файл, что уходит в отчёт.

    Логотип не обязателен, и его отсутствие не повод ронять страницу: шапка
    просто остаётся без картинки. Ссылка на внешний адрес в шапку не идёт —
    страница не должна ждать чужой сервер при каждой перерисовке; для отчёта
    такая ссылка по-прежнему работает.
    """
    storage_path = str((branding or {}).get("logo_storage_path") or "").strip()
    if not storage_path:
        return b""
    try:
        return load_storage_file(storage_path, project_id)
    except Exception:  # noqa: BLE001 — картинка не стоит падения страницы
        LOGGER.warning("Не удалось загрузить логотип проекта %s", project_id)
        return b""


def render_project_header(
    container,
    *,
    project_id: str,
    project_name: str,
    report_branding: dict[str, Any],
    page: str,
    period_label: str,
    profile_label: str,
    hide_technical: bool,
) -> None:
    """Компактная шапка проекта: логотип, название и одна строка подписи."""
    with container:
        # Логотип берётся тот же, что уходит в отчёт: один логотип на проект,
        # загружается в настройках. Два разных неминуемо разошлись бы, а
        # заказчик увидел бы на экране одно, в присланном файле другое.
        logo = project_logo_bytes(project_id, report_branding)
        if logo:
            logo_col, name_col = st.columns([1, 6], vertical_alignment="center")
            with logo_col:
                st.image(logo, width=110)
            name_box = name_col
        else:
            name_box = st.container()
        with name_box:
            st.markdown(f"### {project_name}")
            # Профиль алгоритма — техническая деталь, клиенту он ничего не говорит.
            head_parts = (
                [page, period_label]
                if hide_technical
                else [profile_label, page, period_label]
            )
            st.caption(" · ".join(x for x in head_parts if x))


def render_view_panel(
    container,
    *,
    page: str,
    project_id: str,
    project_profile: str,
    current_project_settings: dict[str, Any],
    events: pd.DataFrame,
    saved_blocks: set[str],
    saved_title_merge: float,
    start_key: str,
    role: str,
    hide_technical: bool,
    read_only: bool,
) -> tuple[bool, int | None, float]:
    """Панель «⚙️ Вид» в шапке: метрики в шапке, порог инфоповода, склейка
    заголовков, стартовый раздел. Без панели (container=None, её не видит
    зритель) — сохранённые значения проекта.

    Возвращает (показывать метрики, мин. сообщений в инфоповоде, сила склейки).
    """
    min_event_messages: int | None = None
    event_title_merge_threshold = saved_title_merge
    if container is None:
        show_metrics = "metrics" in saved_blocks
        min_event_messages = int(default_min_event_messages(project_profile, events))
    else:
        with container:
            if hasattr(st, "popover"):
                view_box = st.popover("⚙️ Вид", width="stretch")
            else:
                view_box = st.expander("⚙️ Вид")
            with view_box:
                show_metrics = st.checkbox(
                    "Метрики периода в шапке",
                    value=("metrics" in saved_blocks),
                    key="view_show_metrics",
                    help=(
                        "В «Обзоре» — полоса из четырёх показателей и тональности. "
                        "В остальных разделах те же числа одной строкой, чтобы не "
                        "отодвигать таблицы вниз."
                    ),
                )
                if hide_technical:
                    min_event_messages = int(
                        default_min_event_messages(project_profile, events)
                    )
                else:
                    min_event_messages = render_min_event_messages_control(
                        project_profile,
                        events,
                        key="main_min_event_messages",
                        container=view_box,
                    )
                    event_title_merge_threshold = render_title_merge_control(
                        saved_title_merge,
                        key="main_title_merge",
                        container=view_box,
                    )
                # read_only — тот же признак «демо-гость, не владелец», что
                # гасит запись в панели ИИ и в разделах отчёта: без него гость
                # с кодом редактора мог сохранить свой вид как умолчание для
                # всех следующих гостей демо-проекта.
                if role_rank(role) >= role_rank("editor") and not read_only:
                    st.divider()
                    if st.button(
                        "Открывать проект на этом разделе",
                        key="view_save_start_section",
                        help=f"Запомнить «{page}» как стартовый раздел проекта.",
                    ):
                        updated = dict(current_project_settings or {})
                        dvs_raw = dict(updated.get("dashboard_view_settings") or {})
                        dvs_raw[start_key] = page
                        dvs_raw["main_visible_blocks"] = (
                            ["metrics"] if show_metrics else []
                        ) + [b for b in saved_blocks if b != "metrics"]
                        dvs_raw["event_title_merge"] = float(
                            event_title_merge_threshold
                        )
                        updated["dashboard_view_settings"] = dvs_raw
                        try:
                            update_project(project_id, settings=updated)
                            clear_platform_caches(project_id)
                            st.success("Сохранено для проекта.")
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001 — сохранение не роняет страницу
                            show_error("Не удалось сохранить настройки вида.", exc, warning=True)
    return show_metrics, min_event_messages, event_title_merge_threshold


def render_header_metrics(
    page: str,
    *,
    project_id: str,
    project_name: str,
    enriched_messages: pd.DataFrame,
    periods: pd.DataFrame,
    selected_period_ids: list[str],
    granularity_narrowed: bool,
    tag_slice: list[str],
    profile_label: str,
    chart_label_settings: dict[str, Any],
    dashboard_view_settings: dict[str, Any],
) -> dict[str, Any] | None:
    """Метрики периода в шапке: полная полоса в «Обзоре», строка — в остальных
    разделах. Возвращает метрики (их берёт «Отчёт») или None при сбое."""
    metrics = None
    # Полоса метрик — надстройка над разделом, а не сам раздел: её падение
    # не должно стоить пользователю содержимого страницы.
    try:
        if page == "Обзор":
            # В «Обзоре» показатели периода и есть содержание раздела,
            # поэтому здесь полная полоса с динамикой к прошлому периоду.
            # «Прошлый период» — это ровно ОДИН период перед самым ранним
            # выбранным (previous_period_id). Карточки выше показывают
            # сумму по ВСЕМ выбранным периодам (или по узкому куску,
            # который оставила гранулярность) — сравнивать это с одним
            # целым прошлым периодом нечестно: два выбранных периода
            # против одного такого же дали бы «+100%» на ровном месте, а
            # выбор части дней — глубокое ложное падение. Та же защита,
            # что уже стоит в «Индексах бренда» (partial_period).
            comparable_previous = (
                len(selected_period_ids) < 2 and not granularity_narrowed
            )
            prev_id = previous_period_id(periods, selected_period_ids)
            prev_metrics = (
                period_overview_metrics(project_id, prev_id, slice_keys(tag_slice))
                if comparable_previous
                else None
            )
            prev_label = ""
            prev_disabled_reason = ""
            if prev_metrics and prev_id and not periods.empty:
                prev_row = periods[periods["period_id"].astype(str) == str(prev_id)]
                if not prev_row.empty:
                    prev_label = str(prev_row.iloc[0].get("period_name") or prev_id)
            elif not comparable_previous and prev_id:
                # Подпись — только когда сравнивать было с чем: без
                # прошлого периода она объясняла бы отсутствие изменения
                # не той причиной. Для нескольких периодов она говорит о
                # периоде ДО выбранных: изменения между самими выбранными
                # периодами «Клиентский обзор» ниже показывает.
                if len(selected_period_ids) < 2:
                    prev_disabled_reason = (
                        "Изменение к предыдущему периоду не показано: в "
                        "гранулярности отмечены не все дни периода."
                    )
                elif granularity_narrowed:
                    prev_disabled_reason = (
                        "Изменение к периоду до выбранных не показано: "
                        "выбрано несколько периодов, и в гранулярности "
                        "отмечены не все их дни."
                    )
                else:
                    prev_disabled_reason = (
                        "Изменение к периоду до выбранных не показано: "
                        "выбрано несколько периодов. Изменения между ними — "
                        "ниже, в «Клиентском обзоре»."
                    )
            metrics = render_project_intro(
                project_name,
                enriched_messages,
                periods,
                selected_period_ids,
                profile_label=profile_label,
                chart_label_settings=chart_label_settings,
                comparison_visible_charts=dashboard_view_settings.get(
                    "comparison_visible_charts"
                ),
                show_comparison=False,
                show_title=False,
                previous_metrics=prev_metrics,
                previous_label=prev_label,
                previous_disabled_reason=prev_disabled_reason,
            )
        else:
            # В рабочих разделах те же числа нужны как ориентир, а не как
            # содержание: полоса из семи карточек занимала треть экрана и
            # отодвигала вниз таблицы, ради которых раздел и открывают.
            metrics = render_period_metrics_line(enriched_messages)
    except Exception:  # noqa: BLE001 — граница отказа
        LOGGER.exception("Метрики в шапке не отрисовались")
        st.caption("Метрики периода сейчас недоступны.")
    return metrics
