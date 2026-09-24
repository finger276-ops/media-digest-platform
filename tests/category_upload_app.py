# -*- coding: utf-8 -*-
"""Мини-приложение для проверки блока «Выгрузка по категории» через AppTest.

Не запускается само по себе: поддельный Supabase-клиент подставляет
вызывающий тест (test_category_store.py). Права задаются через
session_state["can_edit"], чтобы один файл проверял и редактора, и зрителя.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from brand_metrics_ui import render_category_upload  # noqa: E402

PROJECT_ID = "proj-cat"
PERIODS = pd.DataFrame(
    [
        {"period_id": "p1", "period_name": "Август"},
        {"period_id": "p2", "period_name": "Сентябрь"},
    ]
)

render_category_upload(
    PROJECT_ID,
    PERIODS,
    ["p1", "p2"],
    bool(st.session_state.get("can_edit", True)),
)
