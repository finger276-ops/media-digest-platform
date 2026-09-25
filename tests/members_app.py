"""Мини-приложение для блока «Доступ по email» карточки проекта.

Запускается через AppTest из tests/test_email_login.py. В настоящем
приложении блок стоит в карточке проекта, а карточка открывается выбором
строки в таблице — AppTest такой выбор сделать не умеет, поэтому блок
рисуется здесь напрямую.

Параметры — через session_state: members_project_id и members_can_edit.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import streamlit as st  # noqa: E402

from members_ui import render_project_members  # noqa: E402

render_project_members(
    str(st.session_state.get("members_project_id") or ""),
    can_edit=bool(st.session_state.get("members_can_edit")),
)
