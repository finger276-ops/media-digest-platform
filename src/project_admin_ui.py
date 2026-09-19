# -*- coding: utf-8 -*-
"""Раздел «Платформа»: доступ к проекту и управление проектами.

render_project_access решает, какой проект открыт в этой сессии (владелец
платформы выбирает из списка, редактор/зритель входит по коду).
render_project_manager — создание проектов и редактирование существующих:
профиль алгоритма, подписи графиков, брендирование отчётов, клиентский вид,
опасная зона удаления.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.cached_store import (
    create_project,
    delete_project,
    delete_storage_file,
    download_storage_file,
    list_projects,
    resolve_project_access,
    save_report_logo_to_storage,
    update_project,
)
from services.dashboard_config import (
    ALGORITHM_PROFILE_OPTIONS,
    CHART_LABEL_FONT_OPTIONS,
    CHART_LABEL_POSITION_OPTIONS,
    COMPARISON_CHART_BLOCKS,
    DASHBOARD_SECTION_OPTIONS,
    DEFAULT_DASHBOARD_VIEW_SETTINGS,
    REPORT_SECTION_OPTIONS,
)
from services.formatting import fmt_date
from services.metrics_compute import format_int
from services.roles import role_rank, role_title
from services.project_settings import (
    DEMO_AI_LIMIT,
    chart_label_settings_from_project_settings,
    dashboard_view_settings_from_project_settings,
    demo_ai_runs_used,
    is_demo_project,
    merged_dashboard_view_settings,
    project_settings_from_row,
    report_branding_from_project_settings,
    report_sections_from_project_settings,
    valid_hex_color,
)


def render_project_access(is_admin: bool) -> tuple[str | None, str, pd.DataFrame]:
    projects = list_projects(include_inactive=is_admin)
    if projects.empty:
        if is_admin:
            st.info(
                "Пока нет проектов. Создайте первый проект в блоке управления ниже."
            )
        else:
            st.warning("Пока нет доступных проектов.")
        return None, "owner" if is_admin else "none", projects

    if is_admin:
        options = projects["project_id"].astype(str).tolist()
        labels = {
            str(r["project_id"]): str(r.get("project_name") or r["project_id"])
            for _, r in projects.iterrows()
        }
        selected = st.sidebar.selectbox(
            "Проект", options, format_func=lambda x: labels.get(x, x)
        )
        return selected, "owner", projects

    if st.session_state.get("platform_project_id"):
        project_id = st.session_state["platform_project_id"]
        role = st.session_state.get("platform_project_role", "viewer")
        project_row = projects[projects["project_id"].astype(str) == str(project_id)]
        project_name = str(
            project_row.iloc[0].get("project_name")
            if not project_row.empty
            else project_id
        )
        st.sidebar.success(f"Доступ: {project_name} · {role_title(role)}")
        if st.sidebar.button("Сменить проект / выйти"):
            st.session_state.pop("platform_project_id", None)
            st.session_state.pop("platform_project_role", None)
            st.rerun()
        return project_id, role, projects

    st.sidebar.info("Введите код доступа к проекту")
    access_code = st.sidebar.text_input(
        "Код проекта", type="password", key="project_access_code"
    )
    if st.sidebar.button("Открыть проект"):
        project_id, role = resolve_project_access(access_code)
        if project_id:
            st.session_state["platform_project_id"] = project_id
            st.session_state["platform_project_role"] = role
            st.rerun()
        else:
            st.sidebar.error("Код проекта не найден.")
    return None, "none", projects


def render_project_manager(
    projects: pd.DataFrame,
    *,
    is_admin: bool = True,
    role: str = "owner",
    current_project_id: str | None = None,
) -> None:
    """Настройки проектов. Владельцу платформы — все, аналитику — только свой.

    Проверка роли стоит здесь, а не только в сборке меню. Раньше страница
    полагалась на то, что её пункт просто не попадёт в боковое меню: любая
    будущая правка навигации сразу становилась дырой в правах, а на этой
    странице лежат коды доступа и необратимое удаление проекта.

    «Владелец проекта» в платформе — это тот, у кого код редактора: личности
    у кодов нет, привязать проект к человеку нечем. Поэтому аналитик работает
    ровно с тем проектом, в который вошёл, и заводит новые, сам задавая им
    коды. Чужие проекты и их коды ему не показываются.
    """
    can_manage = is_admin or role_rank(role) >= role_rank("editor")
    if not can_manage:
        st.info("Управление проектами доступно аналитику и владельцу платформы.")
        return

    if not is_admin:
        projects = projects[
            projects["project_id"].astype(str) == str(current_project_id or "")
        ]

    st.header("Управление проектами")
    if not is_admin:
        st.caption(
            "Показан проект, в который вы вошли. Созданный проект открывается "
            "кодом аналитика, который вы ему зададите."
        )
    with st.expander("Создать проект", expanded=projects.empty):
        name = st.text_input("Название проекта", key="new_project_name")
        description = st.text_area("Описание проекта", key="new_project_description")
        topic_profile = st.selectbox(
            "Профиль алгоритма",
            list(ALGORITHM_PROFILE_OPTIONS.keys()),
            format_func=lambda x: ALGORITHM_PROFILE_OPTIONS.get(x, x),
            key="new_topic_profile",
            help="Профиль не привязывает платформу к одной отрасли: по умолчанию темы берутся из колонок выгрузки и универсальных правил.",
        )
        viewer_code = st.text_input(
            "Код пользователя",
            type="password",
            key="new_viewer_code",
            help="Смотрит аналитику, выбирает периоды и скачивает отчёт.",
        )
        editor_code = st.text_input(
            "Код аналитика",
            type="password",
            key="new_editor_code",
            help="Всё то же плюс правка данных, загрузка выгрузок и настройки проекта.",
        )
        if st.button("Создать проект", type="primary"):
            if not name.strip():
                st.error("Укажите название проекта.")
            else:
                project_id = create_project(
                    project_name=name,
                    description=description,
                    viewer_code=viewer_code,
                    editor_code=editor_code,
                    settings={"topic_profile": topic_profile},
                )
                st.success(f"Проект создан: {project_id}")
                if not is_admin:
                    # Аналитик сидит в проекте, в который вошёл кодом. Новый
                    # проект откроется только своим кодом — сказать об этом
                    # надо сразу, иначе человек будет искать его в списке.
                    st.info(
                        "Чтобы перейти в новый проект, нажмите «Сменить проект "
                        "/ выйти» и войдите кодом аналитика, который вы только "
                        "что задали."
                    )
                else:
                    st.rerun()

    if projects.empty:
        return
    st.subheader("Существующие проекты")
    view = projects.copy()
    for col in ["created_at", "updated_at"]:
        if col in view.columns:
            view[col] = view[col].apply(fmt_date)
    show = view[
        [
            c
            for c in [
                "project_name",
                "description",
                "status",
                "created_at",
                "updated_at",
                "project_id",
            ]
            if c in view.columns
        ]
    ].rename(
        columns={
            "project_name": "Проект",
            "description": "Описание",
            "status": "Статус",
            "created_at": "Создан",
            "updated_at": "Обновлен",
            "project_id": "ID",
        }
    )
    event = st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        selection_mode="single-row",
        on_select="rerun",
    )
    rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
    if rows:
        row = projects.iloc[rows[0]]
        project_id = str(row["project_id"])
        with st.expander(f"Редактировать: {row.get('project_name')}", expanded=True):
            new_name = st.text_input(
                "Название",
                value=str(row.get("project_name") or ""),
                key=f"edit_project_name_{project_id}",
            )
            new_description = st.text_area(
                "Описание",
                value=str(row.get("description") or ""),
                key=f"edit_project_description_{project_id}",
            )
            new_status = st.selectbox(
                "Статус",
                ["active", "hidden", "archived"],
                index=(
                    ["active", "hidden", "archived"].index(
                        str(row.get("status") or "active")
                    )
                    if str(row.get("status") or "active")
                    in ["active", "hidden", "archived"]
                    else 0
                ),
                key=f"edit_project_status_{project_id}",
            )
            current_settings = project_settings_from_row(row)
            current_profile = str(current_settings.get("topic_profile") or "universal")
            if current_profile not in ALGORITHM_PROFILE_OPTIONS:
                current_profile = "universal"
            new_topic_profile = st.selectbox(
                "Профиль алгоритма",
                list(ALGORITHM_PROFILE_OPTIONS.keys()),
                index=list(ALGORITHM_PROFILE_OPTIONS.keys()).index(current_profile),
                format_func=lambda x: ALGORITHM_PROFILE_OPTIONS.get(x, x),
                key=f"edit_topic_profile_{project_id}",
            )
            current_chart_labels = chart_label_settings_from_project_settings(
                current_settings
            )
            with st.expander("Настройки подписей на графиках", expanded=False):
                chart_font = st.selectbox(
                    "Шрифт значений",
                    CHART_LABEL_FONT_OPTIONS,
                    index=(
                        CHART_LABEL_FONT_OPTIONS.index(
                            current_chart_labels.get("font", "Arial")
                        )
                        if current_chart_labels.get("font", "Arial")
                        in CHART_LABEL_FONT_OPTIONS
                        else 0
                    ),
                    key=f"chart_label_font_{project_id}",
                )
                chart_font_size = st.slider(
                    "Размер шрифта",
                    min_value=8,
                    max_value=28,
                    value=int(current_chart_labels.get("font_size", 11)),
                    step=1,
                    key=f"chart_label_size_{project_id}",
                )
                position_keys = list(CHART_LABEL_POSITION_OPTIONS.keys())
                current_position = current_chart_labels.get("position", "top")
                chart_position = st.radio(
                    "Местоположение значений",
                    position_keys,
                    index=(
                        position_keys.index(current_position)
                        if current_position in position_keys
                        else 0
                    ),
                    format_func=lambda x: CHART_LABEL_POSITION_OPTIONS.get(x, x),
                    horizontal=True,
                    key=f"chart_label_position_{project_id}",
                )
                show_donut_legend = st.checkbox(
                    "Показывать легенду круговой диаграммы",
                    value=bool(current_chart_labels.get("show_donut_legend", False)),
                    help="По умолчанию легенда скрыта, потому что рядом с круговой диаграммой уже есть блок значений.",
                    key=f"chart_label_show_donut_legend_{project_id}",
                )
                st.caption(
                    "Настройка применяется к подписям на линейных, столбчатых и круговых графиках сравнения периодов."
                )

            current_branding = report_branding_from_project_settings(
                current_settings, project_name=str(row.get("project_name") or "")
            )
            with st.expander("Брендирование отчетов", expanded=False):
                report_client_name = st.text_input(
                    "Название клиента в отчете",
                    value=str(current_branding.get("client_name") or ""),
                    key=f"report_client_name_{project_id}",
                    help="Это название будет отображаться на титульной инфографике и в Word/PDF.",
                )
                report_title = st.text_input(
                    "Заголовок отчета",
                    value=str(
                        current_branding.get("report_title") or "Дайджест упоминаний"
                    ),
                    key=f"report_title_{project_id}",
                )
                b1, b2 = st.columns(2)
                with b1:
                    report_accent_color = st.color_picker(
                        "Акцентный цвет",
                        value=valid_hex_color(
                            current_branding.get("accent_color"), "#2563eb"
                        ),
                        key=f"report_accent_color_{project_id}",
                    )
                with b2:
                    report_background_color = st.color_picker(
                        "Фон инфографики",
                        value=valid_hex_color(
                            current_branding.get("background_color"), "#ffffff"
                        ),
                        key=f"report_background_color_{project_id}",
                    )
                report_footer_text = st.text_input(
                    "Подпись в футере",
                    value=str(current_branding.get("footer_text") or ""),
                    key=f"report_footer_text_{project_id}",
                    help="Например: подготовлено агентством / внутренний аналитический отчет.",
                )
                report_logo_url = st.text_input(
                    "URL логотипа",
                    value=str(current_branding.get("logo_url") or ""),
                    key=f"report_logo_url_{project_id}",
                    help="Можно указать публичную ссылку на логотип или загрузить файл ниже. Для стабильной выгрузки лучше использовать загрузку файла.",
                )
                current_logo_path = str(
                    current_branding.get("logo_storage_path") or ""
                ).strip()
                if current_logo_path or report_logo_url:
                    st.caption(
                        f"Текущий логотип: {current_branding.get('logo_filename') or report_logo_url or current_logo_path}"
                    )
                    try:
                        if current_logo_path:
                            st.image(
                                download_storage_file(current_logo_path), width=160
                            )
                        elif report_logo_url:
                            st.image(report_logo_url, width=160)
                    except Exception:
                        st.caption(
                            "Предпросмотр логотипа недоступен, но ссылка/путь сохранены в настройках."
                        )
                report_logo_file = st.file_uploader(
                    "Загрузить / заменить логотип",
                    type=["png", "jpg", "jpeg", "webp"],
                    key=f"report_logo_file_{project_id}",
                    help="Рекомендуемый формат — PNG с прозрачным фоном, ширина 600–1200 px, вес до 1 МБ.",
                )
                remove_report_logo = st.checkbox(
                    "Удалить сохраненный логотип",
                    value=False,
                    key=f"remove_report_logo_{project_id}",
                    help="Удалит файл логотипа из Storage и очистит настройки логотипа после сохранения проекта.",
                )
                st.caption(
                    "Брендирование применяется к Word/PDF/PNG-выгрузкам саммари и клиентских отчетов."
                )

            current_report_sections = report_sections_from_project_settings(
                current_settings
            )
            with st.expander("Разделы отчёта по умолчанию", expanded=False):
                st.caption(
                    "Какие блоки открыты при выгрузке саммари по умолчанию. "
                    "Аналитик может изменить набор перед конкретной выгрузкой — "
                    "здесь настраивается только стартовый выбор."
                )
                report_sections = st.multiselect(
                    "Разделы",
                    list(REPORT_SECTION_OPTIONS.keys()),
                    default=current_report_sections,
                    format_func=lambda s: REPORT_SECTION_OPTIONS.get(s, s),
                    key=f"report_sections_{project_id}",
                )

            current_view_settings = dashboard_view_settings_from_project_settings(
                current_settings
            )
            with st.expander(
                "Клиентский режим и сохраненные представления", expanded=False
            ):
                view_mode_options = ["client", "analyst"]
                default_view_mode = st.radio(
                    "Вид дашборда по умолчанию",
                    view_mode_options,
                    index=(
                        view_mode_options.index(
                            current_view_settings.get("default_view_mode", "client")
                        )
                        if current_view_settings.get("default_view_mode", "client")
                        in view_mode_options
                        else 0
                    ),
                    format_func=lambda x: {
                        "client": "Клиентский",
                        "analyst": "Аналитический",
                    }.get(x, x),
                    horizontal=True,
                    key=f"default_view_mode_{project_id}",
                    help="Клиентский вид подходит для демонстрации заказчику: меньше технических настроек и больше готовой аналитики.",
                )
                start_section = st.selectbox(
                    "Стартовый раздел обычного проекта",
                    DASHBOARD_SECTION_OPTIONS,
                    index=(
                        DASHBOARD_SECTION_OPTIONS.index(
                            current_view_settings.get(
                                "start_section", "Клиентский обзор"
                            )
                        )
                        if current_view_settings.get(
                            "start_section", "Клиентский обзор"
                        )
                        in DASHBOARD_SECTION_OPTIONS
                        else 0
                    ),
                    key=f"start_section_{project_id}",
                )
                default_comparison_charts = st.multiselect(
                    "Графики сравнения по умолчанию",
                    COMPARISON_CHART_BLOCKS,
                    default=[
                        x
                        for x in current_view_settings.get(
                            "comparison_visible_charts",
                            DEFAULT_DASHBOARD_VIEW_SETTINGS[
                                "comparison_visible_charts"
                            ],
                        )
                        if x in COMPARISON_CHART_BLOCKS
                    ],
                    key=f"default_comparison_charts_{project_id}",
                    help="Эти графики будут включены по умолчанию в блоке «Визуализация сравнений». Пользователь сможет изменить выбор на странице.",
                )
                client_hide_technical = st.checkbox(
                    "Скрывать технические настройки в клиентском виде",
                    value=bool(
                        current_view_settings.get("client_hide_technical", True)
                    ),
                    key=f"client_hide_technical_{project_id}",
                )
                st.caption(
                    "Настройки сохраняют подготовленный клиентский вид проекта: стартовый раздел, набор графиков и уровень технических элементов."
                )

            with st.expander("Демонстрационный проект", expanded=False):
                demo_mode = st.checkbox(
                    "Тестовый доступ: витрина без правки",
                    value=is_demo_project(current_settings),
                    key=f"demo_mode_{project_id}",
                    help=(
                        "Проект показывают снаружи. Разделы и аналитика видны "
                        "целиком, но менять нельзя ничего: правка описаний, "
                        "саммари и настроек выключается, загрузка новых файлов "
                        "закрыта. Генерация ИИ остаётся, но не больше "
                        f"{DEMO_AI_LIMIT} запусков на проект."
                    ),
                )
                used = demo_ai_runs_used(current_settings)
                st.caption(
                    f"Израсходовано запусков ИИ: {used} из {DEMO_AI_LIMIT}. "
                    "Счётчик не сбрасывается — демо выдаётся многим, и "
                    "обнуление сделало бы лимит бесконечным."
                )
                reset_demo_ai = st.checkbox(
                    "Обнулить счётчик запусков ИИ",
                    value=False,
                    key=f"demo_ai_reset_{project_id}",
                    help="Разовое действие владельца платформы, а не автоматика.",
                )

            st.caption("Коды доступа заполняйте только если хотите заменить текущие.")
            new_viewer_code = st.text_input(
                "Новый код пользователя",
                type="password",
                key=f"edit_viewer_code_{project_id}",
            )
            new_editor_code = st.text_input(
                "Новый код аналитика",
                type="password",
                key=f"edit_editor_code_{project_id}",
            )
            if st.button("Сохранить проект", key=f"save_project_{project_id}"):
                updated_settings = dict(current_settings)
                updated_settings["topic_profile"] = new_topic_profile
                updated_settings["chart_label_settings"] = {
                    "font": chart_font,
                    "font_size": int(chart_font_size),
                    "position": chart_position,
                    "show_donut_legend": bool(show_donut_legend),
                }
                logo_storage_path = str(
                    current_branding.get("logo_storage_path") or ""
                ).strip()
                logo_filename = str(current_branding.get("logo_filename") or "").strip()
                logo_mime_type = str(
                    current_branding.get("logo_mime_type") or ""
                ).strip()
                logo_url_value = str(report_logo_url or "").strip()

                if remove_report_logo:
                    if logo_storage_path:
                        delete_storage_file(logo_storage_path)
                    logo_storage_path = ""
                    logo_filename = ""
                    logo_mime_type = ""
                    logo_url_value = ""

                if report_logo_file is not None:
                    logo_bytes = report_logo_file.getvalue()
                    if len(logo_bytes) > 2 * 1024 * 1024:
                        st.error("Логотип слишком большой. Загрузите файл до 2 МБ.")
                        st.stop()
                    if logo_storage_path:
                        delete_storage_file(logo_storage_path)
                    try:
                        logo_meta = save_report_logo_to_storage(
                            project_id, report_logo_file.name, logo_bytes
                        )
                        logo_storage_path = str(
                            logo_meta.get("logo_storage_path") or ""
                        )
                        logo_url_value = str(
                            logo_meta.get("logo_url") or logo_url_value or ""
                        )
                        logo_filename = str(
                            logo_meta.get("logo_filename") or report_logo_file.name
                        )
                        logo_mime_type = str(
                            logo_meta.get("logo_mime_type")
                            or report_logo_file.type
                            or ""
                        )
                    except Exception as exc:
                        st.error(f"Не удалось загрузить логотип в Storage: {exc}")
                        st.stop()

                updated_settings["report_branding"] = {
                    "client_name": report_client_name,
                    "report_title": report_title,
                    "accent_color": report_accent_color,
                    "background_color": report_background_color,
                    "footer_text": report_footer_text,
                    "logo_url": logo_url_value,
                    "logo_storage_path": logo_storage_path,
                    "logo_filename": logo_filename,
                    "logo_mime_type": logo_mime_type,
                }
                updated_settings["report_sections"] = list(report_sections) or list(
                    REPORT_SECTION_OPTIONS.keys()
                )
                updated_settings["demo_mode"] = bool(demo_mode)
                if reset_demo_ai:
                    updated_settings["demo_ai_runs"] = 0
                updated_settings["dashboard_view_settings"] = (
                    merged_dashboard_view_settings(
                        current_settings,
                        {
                            "default_view_mode": default_view_mode,
                            "start_section": start_section,
                            "comparison_visible_charts": list(default_comparison_charts),
                            "client_hide_technical": bool(client_hide_technical),
                        },
                    )
                )
                update_project(
                    project_id,
                    project_name=new_name,
                    description=new_description,
                    status=new_status,
                    viewer_code=new_viewer_code,
                    editor_code=new_editor_code,
                    settings=updated_settings,
                )
                st.success("Проект обновлен.")
                st.rerun()

            # Удаление проекта со всеми периодами необратимо и остаётся за
            # владельцем платформы. Аналитик настраивает проект и заводит
            # новые, но снести чужую работу одним нажатием не может: код
            # редактора живёт у подрядчиков и меняется чаще, чем хотелось бы.
            if not is_admin:
                st.caption(
                    "Удаление проекта доступно только владельцу платформы."
                )
                return

            with st.expander("Опасная зона: удалить проект", expanded=False):
                st.warning(
                    "Удаление проекта необратимо: будут удалены проект, периоды, обработанные строки, "
                    "ручные правки и доступы. Другие проекты не затрагиваются."
                )
                delete_storage_files = st.checkbox(
                    "Удалить исходные файлы выгрузок из Storage, если они были сохранены",
                    value=True,
                    key=f"delete_project_storage_{project_id}",
                )
                confirm_delete_project = st.checkbox(
                    "Я понимаю, что проект будет удален без восстановления",
                    value=False,
                    key=f"confirm_delete_project_{project_id}",
                )
                delete_disabled = not confirm_delete_project
                if st.button(
                    "Удалить проект",
                    type="secondary",
                    disabled=delete_disabled,
                    key=f"delete_project_{project_id}",
                ):
                    try:
                        result = delete_project(
                            project_id, delete_storage=delete_storage_files
                        )
                        st.success(
                            "Проект удален: "
                            f"периодов — {result.get('periods_deleted', 0)}, "
                            f"строк данных — {format_int(result.get('table_rows_deleted', 0))}, "
                            f"ручных правок — {format_int(result.get('manual_rows_deleted', 0))}."
                        )
                        warnings = result.get("warnings") or []
                        for warning in warnings:
                            st.warning(str(warning))
                        st.rerun()
                    except Exception as exc:
                        st.error("Не удалось удалить проект.")
                        st.exception(exc)
