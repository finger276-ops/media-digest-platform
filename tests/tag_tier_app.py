# -*- coding: utf-8 -*-
"""Мини-приложение для проверки render_tier_analytics_block через AppTest.

Изолирует блок от остального дашборда: раньше он нигде не тестировался
(ни разу не было ни одного теста на compute_tier_aggregates/render_tier_
analytics_block), хотя это единственное место, где считается покрытие
структурой тегов и own/subtree агрегаты.

Не запускается сам по себе: подмену Supabase-клиента (FakeClient) делает
вызывающий тест (test_tag_tier_analytics.py) ДО того, как в процессе
впервые импортируется что-либо из этого файла — см. комментарий у
CLIENT = store.get_supabase_client() ниже.
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

import pandas as pd  # noqa: E402

import platform_store as store  # noqa: E402

# Клиент подменяется вызывающим тестом (test_tag_tier_analytics.py) ДО того,
# как что-либо в процессе первый раз импортирует services.tag_hierarchy_store
# — тот модуль делает `from platform_store import get_supabase_client`,
# захватывая ссылку на функцию РОВНО в момент своего первого импорта; более
# поздняя переподмена в отдельном FakeClient здесь эту ссылку уже не достала
# бы (она получила бы свой второй, никак не связанный поддельный клиент).
# store.get_supabase_client() — живой атрибут модуля, а не снятая копия,
# поэтому здесь он всегда актуален.
CLIENT = store.get_supabase_client()

from tag_tier_analytics_ui import render_tier_analytics_block  # noqa: E402

PROJECT_ID = "proj-tier"

# Структура: два Тир-1 (Продукция, Конкуренты), у «Продукции» два ребёнка
# (Кнауф Норд, Тисма) — ровно пример из докстринга модуля: сообщение с
# обоими тегами должно посчитаться в поддереве «Продукции» ОДИН раз.
#
# Третья ветка («Форматы» → «Отзыв» → «Развёрнутый») — не для чисел, а для
# проверки самого способа навигации: «Отзыв» сам НЕ верхнего уровня (тир 2),
# но у него есть свой ребёнок («Развёрнутый», тир 3) — ровно тот случай,
# который вживую проверялся в песочнице на «Доставка»/«Гарантия». Список
# веток должен предложить «Отзыв» наравне с «Продукция»/«Конкуренты», а не
# только тир-1 — иначе аналитик не доберётся до третьего уровня вообще.
CLIENT.db["platform_tag_hierarchies"] = [
    {
        "project_id": PROJECT_ID,
        "structure": [
            {"tag": "Продукция", "tier": 1, "parent": ""},
            {"tag": "Кнауф Норд", "tier": 2, "parent": "Продукция"},
            {"tag": "Тисма", "tier": 2, "parent": "Продукция"},
            {"tag": "Конкуренты", "tier": 1, "parent": ""},
            {"tag": "ТЕХНОНИКОЛЬ", "tier": 2, "parent": "Конкуренты"},
            {"tag": "Форматы", "tier": 1, "parent": ""},
            {"tag": "Отзыв", "tier": 2, "parent": "Форматы"},
            {"tag": "Развёрнутый", "tier": 3, "parent": "Отзыв"},
        ],
        "source_filename": "tags.xlsx",
        "tags_count": 8,
        "max_tier": 3,
    }
]


def _messages() -> pd.DataFrame:
    rows = []
    # 4 сообщения с «Кнауф Норд», из них 1 — ещё и с «Тисма» (та же тема
    # продукции с двух тегов сразу — не должно задвоиться в поддереве).
    for i in range(4):
        tags = "Кнауф Норд|Тисма" if i == 0 else "Кнауф Норд"
        rows.append({"message_id": f"m{i}", "tags": tags})
    # 2 сообщения только с «Тисма».
    for i in range(2):
        rows.append({"message_id": f"mt{i}", "tags": "Тисма"})
    # 3 сообщения с «ТЕХНОНИКОЛЬ» (ветка «Конкуренты»).
    for i in range(3):
        rows.append({"message_id": f"mc{i}", "tags": "ТЕХНОНИКОЛЬ"})
    # 1 сообщение с тегом вне структуры — покрытие должно это учесть.
    rows.append({"message_id": "mx", "tags": "Погода"})
    # 2 сообщения с тегом третьего уровня («Форматы» → «Отзыв» → «Развёрнутый»).
    for i in range(2):
        rows.append({"message_id": f"mr{i}", "tags": "Развёрнутый"})
    return pd.DataFrame(rows)


render_tier_analytics_block(_messages(), PROJECT_ID)
