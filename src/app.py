from __future__ import annotations

import argparse
import os
import uuid
import re
import textwrap
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import altair as alt

from services.cached_store import (
    supabase_configured,
    list_projects,
    create_project,
    update_project,
    resolve_project_access,
    list_periods,
    load_generated_tables,
    update_period_metadata,
    delete_period,
    delete_project,
    save_report_logo_to_storage,
    download_storage_file,
    delete_storage_file,
    clear_platform_caches,
    cache_version,
    load_table,
)
from services.metrics_compute import (
    numeric_series,
    prepare_dashboard_messages,
    format_int,
    sentiment_counts,
    percent_text,
    overview_metrics,
)
from services.tag_compute import (
    split_pipe_values,
    clean_brand_analytics_tags,
    build_tag_statistics,
)
from services.message_compute import message_text_column, message_link_column
from services.event_titles import (
    DEFAULT_SIMILARITY,
    merge_similar_events,
    normalize_event_title,
    preview_merge_levels,
)
from services.ingest import IngestError, process_canonical, read_canonical_bytes
from noise_filter_ui import render_noise_filter_block
from ingest_admin_ui import render_ingest_admin_page
from brand_metrics_ui import render_brand_metrics_page
from ai_summary_ui import render_saved_ai_text
from services.ai_summary import (
    KIND_BRAND as AI_KIND_BRAND,
    KIND_RISKS as AI_KIND_RISKS,
)
from tag_hierarchy_ui import render_tag_hierarchy_block
from tag_tier_analytics_ui import render_tier_analytics_block
from services.perf import perf_block, render_perf_sidebar, reset_perf_events
from services.formatting import fmt_date, fmt_period
from services.roles import role_rank
from services.manual_moderation import apply_manual_overrides, blocked_title_merges
from services.event_enrichment import enrich_messages, aggregate_events
from summary_ui import render_period_summary
from sidebar_ui import (
    NAV_STATE_KEY,
    dashboard_view_mode_for_session,
    render_min_event_messages_control,
    cached_merge_similar_events,
    render_title_merge_control,
    filter_small_events,
    render_small_events_notice,
    normalize_section,
    render_sidebar_nav,
)
from events_ui import render_events
from upload_history_ui import (
    render_period_selector,
    render_upload_page,
    render_period_history,
)
from services.period_comparison import (
    period_metrics_for_comparison,
    build_comparison_metrics,
    selected_period_label,
)
from overview_ui import render_project_intro, render_period_comparison_metrics
from services.event_filter_state import (
    set_selected_event_filter,
    get_selected_event_filter,
    clear_selected_event_filter,
    filter_messages_by_selected_event,
    event_series_filter,
)
from messages_ui import render_messages_block, render_message_list
from tags_ui import render_tag_statistics
from client_insights_ui import render_client_insights
from project_admin_ui import render_project_access, render_project_manager
from session_presence_ui import render_presence_heartbeat, render_session_presence_page
from services.dashboard_config import (
    ALGORITHM_PROFILE_OPTIONS,
    LEGACY_PROFILE_ALIASES,
    CHART_LABEL_POSITION_OPTIONS,
    CHART_LABEL_FONT_OPTIONS,
    DEFAULT_CHART_LABEL_SETTINGS,
    REPORT_TEMPLATE_OPTIONS,
    DEFAULT_REPORT_BRANDING,
    COMPARISON_CHART_BLOCKS,
    DEFAULT_DASHBOARD_VIEW_SETTINGS,
    DASHBOARD_SECTION_OPTIONS,
    SECTION_ALIASES,
)
from services.project_settings import (
    project_settings_from_row,
    valid_hex_color,
    dashboard_view_settings_from_project_settings,
    report_branding_from_project_settings,
    project_topic_profile,
    is_brand_analytics_event_set,
    default_min_event_messages,
    chart_label_settings_from_project_settings,
    chart_label_text_kwargs,
    chart_label_radius,
)

APP_TITLE = "Платформа дайджестов"
APP_VERSION = "4.12.4: саммари от ИИ, сертификат без терминала"


def _dashboard_data_uncached(
    project_id: str, period_ids: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Загрузить и подготовить данные проекта за выбранные периоды."""
    events, _discussions, messages, discussion_messages, event_discussions = (
        load_generated_tables(project_id, period_ids)
    )
    enriched = enrich_messages(messages, event_discussions, discussion_messages, events)
    events, enriched, manual_state = apply_manual_overrides(project_id, events, enriched)
    # Brand Analytics: в блоке тегов остаются только системные колонки после
    # «Обработано», без legacy-меток старых алгоритмов.
    enriched = clean_brand_analytics_tags(enriched)
    enriched = prepare_dashboard_messages(enriched)
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


def previous_period_id(
    periods: pd.DataFrame, selected_ids: list[str]
) -> str | None:
    """Период, который идёт перед самым ранним из выбранных.

    Нужен, чтобы в шапке была видна динамика даже когда открыт один период —
    клиенту важно не абсолютное число, а «стало больше или меньше».
    """
    if periods is None or periods.empty or not selected_ids:
        return None
    if "period_id" not in periods.columns:
        return None
    work = periods.copy()
    order = pd.to_datetime(work.get("date_from"), errors="coerce")
    if order.isna().all():
        order = pd.to_datetime(work.get("uploaded_at"), errors="coerce")
    work["_order"] = order
    work = work.sort_values("_order", na_position="first")
    ordered = work["period_id"].astype(str).tolist()
    selected = {str(x) for x in selected_ids}
    positions = [i for i, pid in enumerate(ordered) if pid in selected]
    if not positions or positions[0] == 0:
        return None
    return ordered[positions[0] - 1]


@st.cache_data(show_spinner=False, max_entries=6, ttl=900)
def _cached_period_overview(project_id: str, period_id: str, data_version: int):
    """Метрики одного периода без полной подготовки дашборда."""
    messages = load_table(project_id, [period_id], "messages")
    if messages is None or messages.empty:
        return None
    return overview_metrics(prepare_dashboard_messages(messages))


def period_overview_metrics(project_id: str, period_id: str | None):
    if not project_id or not period_id:
        return None
    try:
        return _cached_period_overview(
            str(project_id), str(period_id), cache_version(project_id, "data")
        )
    except Exception:  # noqa: BLE001 - дельта не критична для страницы
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--work-dir", default=os.getenv("PLATFORM_WORK_DIR", "data/work")
    )
    args, _ = parser.parse_known_args()
    return args


def get_secret_value(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
    except Exception:
        value = os.getenv(name, default)
    return str(value or "").strip()


def is_platform_admin() -> bool:
    admin_password = get_secret_value("PLATFORM_ADMIN_PASSWORD") or get_secret_value(
        "ADMIN_PASSWORD"
    )
    if "platform_is_admin" not in st.session_state:
        st.session_state["platform_is_admin"] = False
    if not admin_password:
        st.sidebar.warning(
            "PLATFORM_ADMIN_PASSWORD не настроен: режим владельца временно доступен всем."
        )
        return True
    if st.session_state.get("platform_is_admin"):
        st.sidebar.success("Режим: владелец платформы")
        if st.sidebar.button("Выйти из режима владельца"):
            st.session_state["platform_is_admin"] = False
            st.rerun()
        return True
    with st.sidebar.expander("Вход владельца платформы", expanded=False):
        password = st.text_input(
            "Пароль владельца", type="password", key="platform_admin_password"
        )
        if st.button("Войти", key="platform_admin_login"):
            if password == admin_password:
                st.session_state["platform_is_admin"] = True
                st.rerun()
            else:
                st.error("Неверный пароль.")
    return False


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _as_fragment(func):
    """Обернуть раздел во фрагмент, если версия Streamlit это умеет.

    Внутри фрагмента перерисовывается только он сам: пагинация ленты, выбор
    тега или инфоповода больше не заставляют приложение заново собирать данные
    всего проекта.
    """
    fragment = getattr(st, "fragment", None)
    return fragment(func) if callable(fragment) else func


@_as_fragment
def _section_tags(messages: pd.DataFrame, project_id: str) -> None:
    render_tag_statistics(messages, project_id=project_id)
    render_tier_analytics_block(messages, project_id=project_id)


@_as_fragment
def _section_messages(messages: pd.DataFrame, project_id: str) -> None:
    render_messages_block(messages, project_id=project_id)


@_as_fragment
def _section_events(
    project_id: str,
    role: str,
    events_agg: pd.DataFrame,
    messages: pd.DataFrame,
    manual_state: dict[str, Any],
    hidden_events: int,
    hidden_messages: int,
    min_event_messages: int,
) -> None:
    render_small_events_notice(hidden_events, hidden_messages, min_event_messages)
    render_events(project_id, role, events_agg, messages, manual_state)


@_as_fragment
def _section_brand_metrics(
    project_id: str,
    project_settings: dict[str, Any],
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    role_can_edit: bool,
) -> None:
    render_brand_metrics_page(
        project_id,
        project_settings,
        messages,
        periods,
        period_ids,
        role_can_edit=role_can_edit,
    )


def main() -> None:
    args = parse_args()
    reset_perf_events()
    st.set_page_config(page_title=APP_TITLE, layout="wide")

    if not supabase_configured():
        st.error(
            "Supabase не настроен. Добавьте SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY в Secrets."
        )
        st.stop()

    is_admin = is_platform_admin()
    project_id, role, projects = render_project_access(is_admin)

    heartbeat_role = "owner" if is_admin else role
    if heartbeat_role in {"owner", "editor", "viewer"}:
        render_presence_heartbeat(project_id, heartbeat_role)

    # --- метаданные выбранного проекта ---
    project_row = (
        projects[projects["project_id"].astype(str) == str(project_id)]
        if project_id and not projects.empty
        else pd.DataFrame()
    )
    current_project_row = project_row.iloc[0] if not project_row.empty else None
    project_name = str(
        current_project_row.get("project_name")
        if current_project_row is not None
        else (project_id or "")
    )
    project_profile = project_topic_profile(current_project_row)
    current_project_settings = (
        project_settings_from_row(current_project_row)
        if current_project_row is not None
        else {}
    )
    chart_label_settings = chart_label_settings_from_project_settings(
        current_project_settings
    )
    report_branding = report_branding_from_project_settings(
        current_project_settings, project_name=project_name
    )
    dashboard_view_settings = dashboard_view_settings_from_project_settings(
        current_project_settings
    )

    # --- боковое меню: разделы аналитики, работа с данными, платформа ---
    section_options = list(DASHBOARD_SECTION_OPTIONS)
    groups: list[tuple[str, list[str]]] = []
    if project_id:
        groups.append(("Аналитика", section_options))
        # Зрителю страницы загрузки не нужны: он туда всё равно не может.
        if role_rank(role) >= role_rank("editor"):
            groups.append(
                ("Данные", ["Загрузка файла", "История периодов", "Автозагрузка"])
            )
    if is_admin:
        groups.append(("Платформа", ["Проекты", "Сессии"]))

    if not groups:
        st.info("Выберите проект или войдите как владелец платформы.")
        if is_admin:
            render_project_manager(projects)
        return

    start_key = "start_section"
    default_section = normalize_section(
        dashboard_view_settings.get(start_key), section_options
    )

    # Выбор периодов относится к аналитике, поэтому он рисуется прямо под
    # её разделами — до блоков «Данные» и «Платформа».
    selected_period_ids: list[str] = []
    periods = pd.DataFrame()
    client_view = True
    current_page = st.session_state.get(NAV_STATE_KEY) or default_section

    def _periods_block() -> None:
        nonlocal selected_period_ids, periods, client_view
        if not project_id or current_page not in section_options:
            return
        selected_period_ids, periods = render_period_selector(project_id)
        client_view = (
            dashboard_view_mode_for_session(role, dashboard_view_settings) == "client"
        )

    page = render_sidebar_nav(
        groups, default_section, after_group={"Аналитика": _periods_block}
    )

    # --- служебный низ боковой панели ---
    if is_admin:
        with st.sidebar.expander("Управление платформой", expanded=False):
            if st.button("Открыть управление проектами", key="open_projects_page"):
                st.session_state[NAV_STATE_KEY] = "Проекты"
                st.rerun()
        st.sidebar.checkbox(
            "Диагностика скорости", value=False, key="platform_perf_debug"
        )
    st.sidebar.caption(f"{APP_TITLE} · {APP_VERSION}")

    # --- страницы, которым не нужны данные периодов ---
    if page == "Проекты":
        render_project_manager(projects)
        return
    if page == "Сессии":
        render_session_presence_page()
        return
    if not project_id:
        st.info("Введите код доступа к проекту или войдите как владелец платформы.")
        return
    if page == "Загрузка файла":
        render_upload_page(project_id, role, args.work_dir)
        return
    if page == "История периодов":
        render_period_history(project_id, role)
        return
    if page == "Автозагрузка":
        render_ingest_admin_page(project_id, project_name, args.work_dir)
        return

    if not selected_period_ids:
        st.info("Выберите период в боковой панели или загрузите первый файл.")
        return

    with st.spinner("Загружаю данные проекта..."):
        events, enriched_messages, raw_events_agg, manual_state = load_dashboard_data(
            project_id, selected_period_ids
        )
    render_perf_sidebar()

    hide_technical = client_view and bool(
        dashboard_view_settings.get("client_hide_technical", True)
    )
    saved_blocks = set(
        dashboard_view_settings.get("main_visible_blocks")
        or ["metrics", "comparison", "summary", "threshold"]
    )

    # --- компактная шапка проекта: одна строка вместо трёх заголовков ---
    period_label = selected_period_label(periods, selected_period_ids)
    profile_label = ALGORITHM_PROFILE_OPTIONS.get(project_profile, project_profile)
    # Панель «Вид» держит только рабочие настройки аналитика, поэтому клиенту
    # она не показывается — вместе с ней исчезает и лишний столбец в шапке.
    show_view_panel = role_rank(role) >= role_rank("editor")
    if show_view_panel:
        head_left, head_right = st.columns([6, 1])
    else:
        head_left, head_right = st.container(), None
    with head_left:
        st.markdown(f"### {project_name}")
        # Профиль алгоритма — техническая деталь, клиенту он ничего не говорит.
        head_parts = (
            [page, period_label]
            if hide_technical
            else [profile_label, page, period_label]
        )
        st.caption(" · ".join(x for x in head_parts if x))

    # Заголовки, которые аналитик запретил склеивать автоматически.
    blocked_merge_titles = blocked_title_merges(manual_state)
    saved_title_merge = float(dashboard_view_settings.get("event_title_merge") or 0.0)

    min_event_messages: int | None = None
    event_title_merge_threshold = saved_title_merge
    if not show_view_panel:
        show_metrics = "metrics" in saved_blocks
        min_event_messages = int(default_min_event_messages(project_profile, events))
    else:
        with head_right:
            if hasattr(st, "popover"):
                view_box = st.popover("⚙️ Вид", use_container_width=True)
            else:
                view_box = st.expander("⚙️ Вид")
            with view_box:
                show_metrics = st.checkbox(
                    "Метрики периода в шапке",
                    value=("metrics" in saved_blocks),
                    key="view_show_metrics",
                    help="Полоса из четырёх показателей и тональности под названием проекта.",
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
                if role_rank(role) >= role_rank("editor"):
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
                        except Exception as exc:
                            st.warning(f"Не удалось сохранить: {exc}")
    # Смысловая склейка заголовков идёт до порога по числу сообщений: иначе
    # одна тема, разбитая источником на три формулировки по два сообщения,
    # отсекается как мелочь, хотя вместе это шесть сообщений.
    merged_events_agg, title_merge_report = cached_merge_similar_events(
        raw_events_agg,
        project_id,
        tuple(selected_period_ids),
        float(event_title_merge_threshold),
        tuple(sorted(blocked_merge_titles)),
    )
    st.session_state[f"title_merge_report_{project_id}"] = title_merge_report
    st.session_state[f"title_merge_threshold_{project_id}"] = float(
        event_title_merge_threshold
    )
    # Диагностика порогов должна считаться по несклеенному агрегату, иначе
    # она меряет склейку поверх склейки.
    st.session_state[f"title_merge_source_{project_id}"] = raw_events_agg
    events_agg, hidden_events, hidden_messages = filter_small_events(
        merged_events_agg, int(min_event_messages or 0)
    )

    metrics = None
    if show_metrics:
        # Дельты в шапке: сравниваем с периодом, который идёт перед выбранными.
        prev_id = previous_period_id(periods, selected_period_ids)
        prev_metrics = period_overview_metrics(project_id, prev_id)
        prev_label = ""
        if prev_metrics and prev_id and not periods.empty:
            prev_row = periods[periods["period_id"].astype(str) == str(prev_id)]
            if not prev_row.empty:
                prev_label = str(prev_row.iloc[0].get("period_name") or prev_id)
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
        )
    st.divider()

    # --- содержимое выбранного раздела ---
    if page == "Обзор":
        render_saved_ai_text(
            project_id,
            AI_KIND_RISKS,
            selected_period_ids,
            heading="Риски периода",
        )
        render_client_insights(
            enriched_messages,
            events_agg,
            periods,
            selected_period_ids,
            profile=project_profile,
        )
    elif page == "Индексы бренда":
        _section_brand_metrics(
            project_id,
            current_project_settings,
            enriched_messages,
            periods,
            selected_period_ids,
            role_rank(role) >= role_rank("editor"),
        )
        render_saved_ai_text(
            project_id,
            AI_KIND_BRAND,
            selected_period_ids,
            heading="Что говорят индексы",
        )
    elif page == "Теги":
        _section_tags(enriched_messages, project_id)
    elif page == "Инфоповоды":
        _section_events(
            project_id,
            role,
            events_agg,
            enriched_messages,
            manual_state,
            hidden_events,
            hidden_messages,
            int(min_event_messages or 0),
        )
    elif page == "Сообщения":
        _section_messages(enriched_messages, project_id)
    elif page == "Динамика":
        if len(selected_period_ids) < 2:
            st.info(
                "Выберите в боковой панели два периода или больше — тогда появится "
                "сравнение и графики динамики."
            )
        else:
            render_period_comparison_metrics(
                enriched_messages,
                periods,
                selected_period_ids,
                chart_label_settings=chart_label_settings,
                comparison_visible_charts=dashboard_view_settings.get(
                    "comparison_visible_charts"
                ),
            )
    elif page == "Отчёт":
        report_metrics = (
            build_comparison_metrics(enriched_messages, periods, selected_period_ids)
            or metrics
        )
        render_period_summary(
            project_id,
            project_name,
            selected_period_ids,
            enriched_messages,
            events_agg,
            periods,
            role,
            profile=project_profile,
            metrics=report_metrics,
            branding=report_branding,
            project_settings=current_project_settings,
        )


if __name__ == "__main__":
    main()
