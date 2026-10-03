from __future__ import annotations

import argparse
import logging
import os

import pandas as pd
import streamlit as st

from error_ui import set_error_details_allowed, show_error_details
from metric_cards_ui import inject_metric_css
from services.cached_store import supabase_configured
from ai_summary_ui import RERUN_AFTER_RENDER_KEY, render_saved_ai_text
from services.ai_summary import (
    KIND_BRAND as AI_KIND_BRAND,
    KIND_RISKS as AI_KIND_RISKS,
)
from services.observability import report_failure
from services.perf import render_perf_sidebar, reset_perf_events
from services.roles import can_see_error_details, role_rank
from services.manual_moderation import (
    blocked_title_merges,
    drop_events_without_messages,
    recompute_event_counts,
)
from services.event_enrichment import aggregate_events
from services.dashboard_data import cached_period_messages
from summary_ui import render_period_summary
from sidebar_ui import (
    NAV_STATE_KEY,
    dashboard_view_mode_for_session,
    cached_merge_similar_events,
    filter_small_events,
    normalize_section,
    render_sidebar_nav,
)
from upload_history_ui import render_period_selector
from services.period_comparison import (
    build_comparison_metrics,
    filter_messages_by_buckets,
    selected_period_label,
)
from granularity_ui import render_granularity_selector
from saved_views_ui import apply_view_from_link, render_saved_views
from tag_slice_ui import (
    BRAND_INDEX_NOTE,
    apply_slice,
    render_tag_slice,
    slice_keys,
    slice_scope,
    slice_title,
    sliced_loader,
)
from overview_ui import render_period_comparison_metrics
from reviews_ui import render_reviews
from ab_compare_ui import render_ab_comparison
from audience_ui import render_audience_page
from client_insights_ui import render_client_insights
from project_admin_ui import render_project_access, render_project_manager
from session_presence_ui import render_presence_heartbeat
from services.dashboard_config import (
    ALGORITHM_PROFILE_OPTIONS,
    DASHBOARD_SECTION_OPTIONS,
)
from services.project_settings import (
    category_brands_from_project_settings,
    demo_ai_runs_left,
    is_demo_project,
    project_settings_from_row,
    dashboard_view_settings_from_project_settings,
    report_branding_from_project_settings,
    project_topic_profile,
    chart_label_settings_from_project_settings,
)
from dashboard_header_ui import render_header_metrics, render_project_header, render_view_panel
from dashboard_sections_ui import (
    _section_brand_metrics,
    _section_events,
    _section_messages,
    _section_sources,
    _section_tags,
)
from service_pages_ui import render_page_without_periods

# Прежние имена app: данные дашборда и граница отказа живут в своих модулях,
# их по-прежнему можно импортировать отсюда (tests/boundary_app.py и др.).
from dashboard_loader import load_dashboard_data, period_overview_metrics  # noqa: F401
from section_boundary_ui import _as_fragment, render_section_safely  # noqa: F401

APP_TITLE = "Платформа дайджестов"
APP_VERSION = "4.12.5: GigaChat понимает формат v2"


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
    # Клиенту, который уже вошёл кодом проекта, форма владельца ни к чему:
    # владелец входит с пустой сессии, после «Сменить проект / выйти».
    if st.session_state.get("platform_project_id"):
        return False
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


LOGGER = logging.getLogger("platform.app")


def main() -> None:
    """Точка входа с общей границей отказа.

    Всё, что падает вне разделов — вход, список периодов, выбор дат, — иначе
    показывал бы трейсбек Streamlit любому, кто открыл страницу. Подробности
    видит только владелец платформы (error_ui).
    """
    try:
        _main()
    except Exception as exc:  # noqa: BLE001 — верхняя граница отказа
        LOGGER.error("Страница не отрисовалась", exc_info=exc)
        report_failure("страница платформы", exc)
        st.error("Не удалось открыть страницу платформы.")
        st.caption(
            "Попробуйте обновить страницу через минуту. Если ошибка "
            "повторяется — напишите владельцу платформы."
        )
        show_error_details(exc)


def _main() -> None:
    args = parse_args()
    reset_perf_events()
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    inject_metric_css()

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

    # Демо-проект: витрина. Смотреть можно всё, что видит аналитик, включая
    # формулы, настройки индексов и тексты от ИИ, — иначе демонстрировать
    # нечего. Менять нельзя ничего: запрет висит не на видимости блоков, а на
    # самих элементах записи, поэтому гость видит интерфейс целиком и понимает,
    # что именно он получит. Владельца платформы демо не касается: ему проект
    # надо готовить.
    demo_project = is_demo_project(current_project_settings)
    demo_read_only = demo_project and not is_admin

    # --- боковое меню: разделы аналитики, работа с данными, платформа ---
    section_options = list(DASHBOARD_SECTION_OPTIONS)
    groups: list[tuple[str, list[str]]] = []
    if project_id:
        groups.append(("Аналитика", section_options))
        # Зрителю страницы загрузки не нужны: он туда всё равно не может.
        # В демо их нет и у редактора: работа идёт с тем, что уже загружено.
        if role_rank(role) >= role_rank("editor") and not demo_read_only:
            # «Настройки проекта» — это карточка того же проекта, в который
            # вошёл аналитик: название, профиль алгоритма, брендирование
            # отчёта, коды доступа. Владелец платформы видит тот же раздел
            # «Проекты» со всеми проектами и опасной зоной.
            groups.append(
                (
                    "Данные",
                    [
                        "Загрузка файла",
                        "История периодов",
                        "Автозагрузка",
                        "Настройки проекта",
                    ],
                )
            )
    if is_admin:
        groups.append(("Платформа", ["Проекты", "Сессии", "Журнал"]))

    if not groups:
        st.info("Выберите проект или войдите как владелец платформы.")
        if is_admin:
            render_project_manager(projects, is_admin=True, role="owner")
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

    # Вид по ссылке ?view=… — до меню и фильтров: он выбирает раздел и
    # выставляет срез и фильтры, а их можно менять только до отрисовки.
    if project_id:
        apply_view_from_link(project_id)

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
        # Номер версии нужен тому, кто выкатывает платформу, а не заказчику.
        st.sidebar.caption(f"{APP_TITLE} · {APP_VERSION}")

    # Технические подробности ошибки — только владельцу платформы: см.
    # can_see_error_details.
    show_error_details = can_see_error_details(
        role, is_admin=is_admin, read_only=demo_read_only
    )
    set_error_details_allowed(show_error_details)

    # --- страницы, которым не нужны данные периодов ---
    if render_page_without_periods(
        page,
        projects,
        project_id=project_id,
        project_name=project_name,
        role=role,
        is_admin=is_admin,
        work_dir=args.work_dir,
        current_project_settings=current_project_settings,
        demo_read_only=demo_read_only,
        show_error_details=show_error_details,
    ):
        return

    if not selected_period_ids:
        st.info("Выберите период в боковой панели или загрузите первый файл.")
        return

    # Падение здесь оставило бы страницу без данных, поэтому дальше идти
    # нельзя — но и трейсбеком на весь экран отвечать не нужно.
    try:
        with st.spinner("Загружаю данные проекта..."):
            events, enriched_messages, raw_events_agg, manual_state = (
                load_dashboard_data(project_id, selected_period_ids)
            )
    except Exception as exc:  # noqa: BLE001 — граница отказа
        LOGGER.exception("Не удалось загрузить данные проекта %s", project_id)
        st.error("Не удалось загрузить данные проекта за выбранные периоды.")
        st.caption(
            "Попробуйте выбрать другой период или обновить страницу. "
            "Если не помогает — период мог быть загружен с ошибкой."
        )
        if show_error_details:
            with st.expander("Подробности ошибки", expanded=False):
                st.exception(exc)
        return
    render_perf_sidebar()

    hide_technical = client_view and bool(
        dashboard_view_settings.get("client_hide_technical", True)
    )
    # Клиентский вид — это предпросмотр кабинета заказчика, а не смена
    # оформления. Раньше он прятал ровно две настройки в поповере «Вид» и одно
    # слово в подписи, поэтому владелец справедливо не видел разницы: правка
    # инфоповодов, формулы методики, веса BPI, разметка брендов и «Редактировать
    # саммари» оставались на экране — то есть показать проект заказчику «как он
    # его увидит» было нельзя. Теперь внутри разделов роль понижается до зрителя.
    #
    # Понижение только сужает права, расширить их так нельзя: у зрителя
    # role_rank уже минимальный, и content_role никогда не выше настоящей роли.
    # Сайдбар и панель «⚙️ Вид» считаются по настоящей роли — иначе из
    # предпросмотра нельзя было бы выйти.
    client_preview = hide_technical and role_rank(role) >= role_rank("editor")
    content_role = "viewer" if client_preview else role
    # Подписи «для аналитика» — откуда взяты теги, что проверить перед
    # отправкой заказчику — видит тот, кто работает с проектом, и не видит
    # заказчик, в том числе в клиентском предпросмотре.
    analyst_view = role_rank(content_role) >= role_rank("editor")
    # Демо и клиентский предпросмотр запрещают правку по-разному, и это
    # намеренно. Предпросмотр показывает кабинет заказчика, поэтому прячет
    # аналитические блоки целиком. Демо наоборот — оставляет их на виду и
    # гасит только элементы записи: гость должен увидеть, что умеет платформа.
    read_only = demo_read_only
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
    render_project_header(
        head_left,
        project_id=project_id,
        project_name=project_name,
        report_branding=report_branding,
        page=page,
        period_label=period_label,
        profile_label=profile_label,
        hide_technical=hide_technical,
    )

    # Заголовки, которые аналитик запретил склеивать автоматически.
    blocked_merge_titles = blocked_title_merges(manual_state)
    saved_title_merge = float(dashboard_view_settings.get("event_title_merge") or 0.0)

    show_metrics, min_event_messages, event_title_merge_threshold = render_view_panel(
        head_right,
        page=page,
        project_id=project_id,
        project_profile=project_profile,
        current_project_settings=current_project_settings,
        events=events,
        saved_blocks=saved_blocks,
        saved_title_merge=saved_title_merge,
        start_key=start_key,
        role=role,
        hide_technical=hide_technical,
        read_only=read_only,
    )

    if demo_read_only:
        st.info(
            f"Тестовый доступ к демонстрационному проекту. Разделы и аналитика "
            f"открыты целиком, но изменить ничего нельзя, а новые выгрузки не "
            f"загружаются. Генерация ИИ доступна: осталось "
            f"{demo_ai_runs_left(current_project_settings)} запусков."
        )

    # Гранулярность (день/неделя/месяц) поверх уже загруженных файлов —
    # выбор файлов в сайдбаре («Периоды») не трогаем: он остаётся тем, ЧТО
    # загружать в сессию. Здесь дробим уже загруженные сообщения по их
    # СОБСТВЕННОЙ дате. Пороги выше («⚙️ Вид» — мин. сообщений в
    # инфоповоде, сила склейки заголовков) сознательно посчитаны ДО этого
    # места, по полным файловым данным: это настройки сборки/кластеризации
    # проекта, они не должны «прыгать» при переключении гранулярности
    # отображения.
    granularity, selected_bucket_ids = render_granularity_selector(
        enriched_messages, project_id, selected_period_ids, periods
    )
    granularity_key = ""
    granularity_narrowed = False
    if granularity != "period":
        narrowed_messages = filter_messages_by_buckets(
            enriched_messages, granularity, selected_bucket_ids
        )
        granularity_key = f"{granularity}::{'|'.join(sorted(str(x) for x in selected_bucket_ids))}"
        if len(narrowed_messages) != len(enriched_messages):
            # Пересчитать счётчики инфоповодов под суженный набор сообщений -
            # тот же приём, что apply_manual_overrides уже делает при каждой
            # загрузке (recompute_event_counts — чистый pandas, дёшево).
            # Членство сообщения в инфоповоде не меняется (оно определено
            # один раз при импорте), меняются только счётчики.
            enriched_messages = narrowed_messages
            granularity_narrowed = True
            events = recompute_event_counts(
                drop_events_without_messages(events, enriched_messages),
                enriched_messages,
            )
            raw_events_agg = aggregate_events(events)

    # Срез по тегам — после гранулярности, по тем же правилам: сообщения
    # сужаются, счётчики инфоповодов пересчитываются, инфоповоды без
    # сообщений среза уходят. «Индексы бренда» получают выборку без среза.
    unsliced_messages = enriched_messages
    tag_slice = render_tag_slice(enriched_messages, project_id)
    render_saved_views(
        project_id, can_edit=role_rank(role) >= role_rank("editor") and not read_only
    )
    if tag_slice:
        enriched_messages = apply_slice(enriched_messages, tag_slice)
        granularity_key = f"{granularity_key}::tags={'|'.join(slice_keys(tag_slice))}"
        events = recompute_event_counts(
            drop_events_without_messages(events, enriched_messages),
            enriched_messages,
        )
        raw_events_agg = aggregate_events(events)

    # Смысловая склейка заголовков идёт до порога по числу сообщений: иначе
    # одна тема, разбитая источником на три формулировки по два сообщения,
    # отсекается как мелочь, хотя вместе это шесть сообщений.
    # Склейка — самый тяжёлый и самый «умный» шаг на странице. Если она
    # сломается, показывать инфоповоды без неё честнее, чем не показывать
    # вообще: данные те же, просто близкие заголовки останутся раздельными.
    try:
        merged_events_agg, title_merge_report = cached_merge_similar_events(
            raw_events_agg,
            project_id,
            tuple(selected_period_ids),
            float(event_title_merge_threshold),
            tuple(sorted(blocked_merge_titles)),
            granularity_key,
        )
    except Exception:  # noqa: BLE001 — граница отказа
        LOGGER.exception("Склейка заголовков не отработала для проекта %s", project_id)
        merged_events_agg, title_merge_report = raw_events_agg, []
        st.caption(
            "Склейка похожих заголовков сейчас недоступна — "
            "инфоповоды показаны без неё."
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
        metrics = render_header_metrics(
            page,
            project_id=project_id,
            project_name=project_name,
            enriched_messages=enriched_messages,
            periods=periods,
            selected_period_ids=selected_period_ids,
            granularity_narrowed=granularity_narrowed,
            tag_slice=tag_slice,
            profile_label=profile_label,
            chart_label_settings=chart_label_settings,
            dashboard_view_settings=dashboard_view_settings,
        )
    st.divider()

    # --- содержимое выбранного раздела ---
    # Вложенная функция, а не отдельная: разделы читают полтора десятка
    # локальных значений, и тащить их через параметры значило бы переписать
    # роутер ради границы отказа.
    def _render_selected_section() -> None:
        if page == "Обзор":
            # При срезе — риски среза, если их написали; иначе риски всего
            # периода с прямой подписью, что они не по срезу.
            shown_slice_risks = bool(tag_slice) and render_saved_ai_text(
                project_id,
                AI_KIND_RISKS,
                selected_period_ids,
                heading=f"Риски среза ({slice_title(tag_slice)})",
                show_model=is_admin and not client_preview,
                scope=slice_scope(tag_slice),
            )
            if not shown_slice_risks:
                render_saved_ai_text(
                    project_id,
                    AI_KIND_RISKS,
                    selected_period_ids,
                    heading=(
                        "Риски периода — по всем сообщениям, без среза по тегам"
                        if tag_slice
                        else "Риски периода"
                    ),
                    show_model=is_admin and not client_preview,
                )
            render_client_insights(
                enriched_messages,
                events_agg,
                periods,
                selected_period_ids,
                profile=project_profile,
                granularity_narrowed=granularity_narrowed,
                analyst_view=analyst_view,
            )
        elif page == "Индексы бренда":
            if tag_slice:
                st.caption(BRAND_INDEX_NOTE)
            _section_brand_metrics(
                project_id,
                current_project_settings,
                unsliced_messages,
                periods,
                selected_period_ids,
                role_rank(content_role) >= role_rank("editor"),
                read_only,
                granularity_narrowed,
            )
            render_saved_ai_text(
                project_id,
                AI_KIND_BRAND,
                selected_period_ids,
                heading="Что говорят индексы",
                show_model=is_admin and not client_preview,
            )
        elif page == "Теги":
            _section_tags(enriched_messages, project_id, analyst_view)
        elif page == "Инфоповоды":
            _section_events(
                project_id,
                content_role,
                events_agg,
                enriched_messages,
                manual_state,
                hidden_events,
                hidden_messages,
                int(min_event_messages or 0),
                read_only,
            )
        elif page == "Отзывы":
            render_reviews(enriched_messages)
        elif page == "Источники":
            _section_sources(
                enriched_messages, periods, selected_period_ids, project_id, tag_slice
            )
        elif page == "Аудитория":
            render_audience_page(enriched_messages)
        elif page == "Сообщения":
            _section_messages(
                enriched_messages, project_id, project_name, period_label, tag_slice
            )
        elif page == "Динамика":
            # Гейт раньше был по числу периодов (нужно 2+), но динамика теперь
            # в первую очередь по дням: одного периода на неделю хватает, если
            # в нём различимы хотя бы два календарных дня. Решение о том,
            # достаточно ли данных, принимает сама функция (build_comparison_
            # metrics с откатом на периоды), а не подсчёт периодов здесь.
            rendered = render_period_comparison_metrics(
                enriched_messages,
                periods,
                selected_period_ids,
                granularity=granularity,
                granularity_narrowed=granularity_narrowed,
                chart_label_settings=chart_label_settings,
                comparison_visible_charts=dashboard_view_settings.get(
                    "comparison_visible_charts"
                ),
            )
            if rendered is None:
                st.info(
                    "Пока не из чего строить динамику: нужно минимум два дня с "
                    "распознанной датой в выбранных сообщениях, либо два "
                    "периода в боковой панели."
                )
            # Два любых периода проекта, независимо от выбора в боковой панели.
            render_ab_comparison(
                project_id,
                periods,
                sliced_loader(
                    lambda period_id: cached_period_messages(project_id, [period_id]),
                    tag_slice,
                ),
                # Два тега сравниваются без среза: теги выбираются в самом блоке.
                current_messages=unsliced_messages,
                brand_map=category_brands_from_project_settings(current_project_settings),
            )
        elif page == "Отчёт":
            report_metrics = (
                build_comparison_metrics(
                    enriched_messages, periods, selected_period_ids, granularity=granularity
                )
                or metrics
            )
            render_period_summary(
                project_id,
                project_name,
                selected_period_ids,
                enriched_messages,
                events_agg,
                periods,
                content_role,
                profile=project_profile,
                metrics=report_metrics,
                branding=report_branding,
                project_settings=current_project_settings,
                client_preview=client_preview,
                read_only=read_only,
                granularity_narrowed=granularity_narrowed,
                tag_slice=tag_slice,
            )

    render_section_safely(page, _render_selected_section, _details=show_error_details)

    # Перерисовка, которую раздел попросил посреди страницы (демо-лимит ИИ):
    # только теперь, когда все поля страницы уже появились и их введённые
    # значения не пропадут.
    if st.session_state.pop(RERUN_AFTER_RENDER_KEY, False):
        st.rerun()


if __name__ == "__main__":
    main()
