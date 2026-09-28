# -*- coding: utf-8 -*-
"""Страницы, которым не нужны данные периодов: проекты и настройки проекта,
сессии, журнал правок, загрузка файла, история периодов, автозагрузка.

Вынесено из app.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from audit_ui import render_audit_page
from backups_ui import render_backups_block
from ingest_admin_ui import render_ingest_admin_page
from project_admin_ui import render_project_manager
from section_boundary_ui import render_section_safely
from session_presence_ui import render_session_presence_page
from upload_history_ui import render_period_history, render_upload_page


def render_page_without_periods(
    page: str,
    projects: pd.DataFrame,
    *,
    project_id: str,
    project_name: str,
    role: str,
    is_admin: bool,
    work_dir: str,
    current_project_settings: dict[str, Any],
    demo_read_only: bool,
    show_error_details: bool,
) -> bool:
    """Страницы, которым не нужны данные периодов: проекты, сессии, журнал,
    загрузка, история периодов, автозагрузка. True — страница нарисована
    (или без проекта показана подсказка), дальше идти не нужно."""
    if page in ("Проекты", "Настройки проекта"):
        render_section_safely(
            page,
            render_project_manager,
            projects,
            is_admin=is_admin,
            role=role,
            current_project_id=project_id,
            _details=show_error_details,
        )
        if page == "Проекты" and is_admin:
            st.divider()
            render_section_safely(
                "Резервные копии", render_backups_block, _details=show_error_details
            )
        return True
    if page == "Сессии":
        render_section_safely(
            "Сессии",
            render_session_presence_page,
            is_admin=is_admin,
            _details=show_error_details,
        )
        return True
    if page == "Журнал":
        render_section_safely(
            "Журнал",
            render_audit_page,
            projects,
            is_admin=is_admin,
            _details=show_error_details,
        )
        return True
    if not project_id:
        st.info("Введите код доступа к проекту или войдите как владелец платформы.")
        return True
    if page == "Загрузка файла":
        render_section_safely(
            "Загрузка файла",
            render_upload_page,
            project_id,
            role,
            work_dir,
            current_project_settings,
            read_only=demo_read_only,
            _details=show_error_details,
        )
        return True
    if page == "История периодов":
        render_section_safely(
            "История периодов",
            render_period_history,
            project_id,
            role,
            read_only=demo_read_only,
            work_dir=work_dir,
            _details=show_error_details,
        )
        return True
    if page == "Автозагрузка":
        render_section_safely(
            "Автозагрузка",
            render_ingest_admin_page,
            project_id,
            project_name,
            work_dir,
            role=role,
            read_only=demo_read_only,
            is_admin=is_admin,
            _details=show_error_details,
        )
        return True
    return False
