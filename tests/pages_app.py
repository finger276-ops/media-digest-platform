"""Мини-приложение: одна страница платформы с заданными правами.

Запускается через AppTest из tests/test_page_access.py. Страницы вызываются
напрямую, в обход меню: тест проверяет, что каждая страница сама закрывает
то, что не положено, а не надеется, что её пункта нет в боковой панели.

Параметры — через session_state: page_name, page_role, page_read_only,
page_is_admin, page_project_id. Для карточки проекта (page_name="card")
выбор строки в таблице подменяется: AppTest не умеет выбирать строку, а
карточка открывается только так.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import streamlit as st  # noqa: E402

from ingest_admin_ui import render_ingest_admin_page  # noqa: E402
from project_admin_ui import render_project_manager  # noqa: E402
from services.cached_store import list_projects  # noqa: E402
from session_presence_ui import render_session_presence_page  # noqa: E402
from upload_history_ui import render_period_history, render_upload_page  # noqa: E402

page = st.session_state.get("page_name")
role = str(st.session_state.get("page_role") or "none")
read_only = bool(st.session_state.get("page_read_only"))
is_admin = bool(st.session_state.get("page_is_admin"))
project_id = str(st.session_state.get("page_project_id") or "")
work_dir = str(st.session_state.get("page_work_dir") or "/tmp")

if page == "ingest":
    render_ingest_admin_page(
        project_id, "Проект", work_dir, role=role, read_only=read_only, is_admin=is_admin
    )
elif page == "sessions":
    render_session_presence_page(is_admin=is_admin)
elif page == "upload":
    render_upload_page(project_id, role, work_dir, {}, read_only=read_only)
elif page == "history":
    render_period_history(project_id, role, read_only=read_only)
elif page == "manager_default":
    render_project_manager(list_projects(include_inactive=True))
elif page == "card":
    projects = list_projects(include_inactive=True)
    # Владельцу таблица показывает все проекты, аналитику — только его.
    ids = projects["project_id"].astype(str).tolist()
    row = ids.index(project_id) if is_admin else 0
    real_dataframe = st.dataframe
    st.dataframe = lambda *a, **k: SimpleNamespace(selection={"rows": [row]})
    try:
        render_project_manager(
            projects, is_admin=is_admin, role=role, current_project_id=project_id
        )
    finally:
        st.dataframe = real_dataframe
