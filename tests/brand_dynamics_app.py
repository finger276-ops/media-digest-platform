# -*- coding: utf-8 -*-
"""Мини-приложение для проверки динамики индексов бренда через AppTest.

Не запускается само по себе: поддельный Supabase-клиент с данными проекта
подставляет вызывающий тест (test_brand_metrics_periods.py). Сообщения
выбранного периода готовятся так же, как в дашборде, — через
prepare_period_messages, а остальные периоды раздел догружает сам, когда
аналитик отмечает «Учесть все периоды проекта».
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from brand_metrics_ui import render_metrics_dynamics  # noqa: E402
from services.brand_metrics import merge_settings  # noqa: E402
from services.dashboard_data import prepare_period_messages  # noqa: E402

PROJECT_ID = "proj-dyn"
PERIODS = pd.DataFrame(
    [
        {"period_id": "p1", "period_name": "Неделя 1", "date_from": "2026-04-01"},
        {"period_id": "p2", "period_name": "Неделя 2", "date_from": "2026-04-08"},
    ]
)

BRAND_MAP = {"own": ["Бренд А"], "competitors": ["Бренд Б"]}

render_metrics_dynamics(
    PROJECT_ID,
    prepare_period_messages(PROJECT_ID, ["p1"]),
    PERIODS,
    ["p1"],
    BRAND_MAP,
    merge_settings(None),
)
