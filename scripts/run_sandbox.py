# -*- coding: utf-8 -*-
"""Локальная песочница: настоящее приложение на выдуманных данных.

Позволяет визуально проверять правки интерфейса на живом Streamlit —
меняете файл в src/, страница сама перерисовывается при сохранении. Не
трогает прод-базу, ничего не коммитит и не требует отката: правки живут в
рабочей копии, пока вы не решите, что оставить.

Запуск:
    .venv\\Scripts\\streamlit.exe run scripts\\run_sandbox.py

Данные — тот же поддельный клиент Supabase, что и в тестах
(tests/fake_supabase.py): один демо-проект «Песочница», два периода,
сообщения с тегами, инфоповоды, иерархия тегов (три тир-1 ветки — ровно
структура со скриншота живой платформы: Бренды/Темы/Форматы). Владелец
платформы открывается автоматически: PLATFORM_ADMIN_PASSWORD намеренно не
задан, а is_platform_admin() (src/app.py) в этом случае даёт доступ без
пароля — то же поведение, что и в проде без настроенного пароля, но здесь
это просто удобно для песочницы, а не то, что попросили не трогать
(fail-open остаётся как есть, это только использование его, не правка).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (ROOT, SRC, ROOT / "tests"):
    p = str(_p)
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["SUPABASE_URL"] = "https://sandbox.local"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "sandbox-key"
os.environ.pop("PLATFORM_ADMIN_PASSWORD", None)

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

# Подменить клиент нужно ДО импорта app/services.* — часть модулей делает
# `from platform_store import get_supabase_client` и захватывает ссылку на
# функцию в момент своего первого импорта (см. коммит про tag_hierarchy_store
# и tests/tag_tier_app.py — та же ловушка, тот же порядок исправления).
CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

PROJECT_ID = "sandbox"
PERIOD_ID = "p_2026_04"
PERIOD_ID_2 = "p_2026_05"
now = datetime.now(timezone.utc).isoformat()

CLIENT.db["platform_projects"] = [
    {
        "project_id": PROJECT_ID,
        "project_name": "Песочница",
        "status": "active",
        "settings": {},
        "created_at": now,
        "updated_at": now,
    }
]
CLIENT.db["platform_periods"] = [
    {
        "project_id": PROJECT_ID,
        "period_id": PERIOD_ID,
        "period_name": "24.04.2026–30.04.2026",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
        "source_filename": "demo1.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    },
    {
        "project_id": PROJECT_ID,
        "period_id": PERIOD_ID_2,
        "period_name": "01.05.2026–07.05.2026",
        "date_from": "2026-05-01",
        "date_to": "2026-05-07",
        "source_filename": "demo2.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    },
]

# --- иерархия тегов: структура со скриншота (Бренды/Темы/Форматы) --------
CLIENT.db["platform_tag_hierarchies"] = [
    {
        "project_id": PROJECT_ID,
        "structure": [
            {"tag": "Бренды", "tier": 1, "parent": ""},
            {"tag": "Кнауф Инсулейшн", "tier": 2, "parent": "Бренды"},
            {"tag": "Кнауф Норд", "tier": 2, "parent": "Бренды"},
            {"tag": "Тисма", "tier": 2, "parent": "Бренды"},
            {"tag": "ТЕХНОНИКОЛЬ", "tier": 2, "parent": "Бренды"},
            {"tag": "Темы", "tier": 1, "parent": ""},
            {"tag": "Цены", "tier": 2, "parent": "Темы"},
            {"tag": "Качество", "tier": 2, "parent": "Темы"},
            {"tag": "Доставка", "tier": 2, "parent": "Темы"},
            {"tag": "Гарантия", "tier": 3, "parent": "Доставка"},
            {"tag": "Форматы", "tier": 1, "parent": ""},
            {"tag": "Отзыв", "tier": 2, "parent": "Форматы"},
            {"tag": "Новость", "tier": 2, "parent": "Форматы"},
        ],
        "source_filename": "tags_demo.xlsx",
        "tags_count": 13,
        "max_tier": 3,
    }
]

_THEMES = [
    ("позитив", "Запуск завода", "Кнауф Инсулейшн|Темы|Новость"),
    ("негатив", "Жалобы на монтаж", "Кнауф Норд|Качество|Отзыв"),
    ("нейтрал", "Отраслевая статистика", "ТЕХНОНИКОЛЬ|Темы|Новость"),
    ("негатив", "Задержка доставки", "Тисма|Доставка|Гарантия|Отзыв"),
    ("позитив", "Акция на утеплитель", "Кнауф Инсулейшн|Цены|Новость"),
]


_PERIOD_START = {
    PERIOD_ID: datetime(2026, 4, 24, 10, 0, 0),
    PERIOD_ID_2: datetime(2026, 5, 1, 10, 0, 0),
}


def _message_row(index, sentiment, views, audience, engagement, theme, tags, period_id):
    # Сообщения разнесены по всем семи дням периода (не одной датой на весь
    # период), чтобы в песочнице было видно дневную разбивку динамики -
    # именно то, что попросили показать на графиках вместо одной точки на
    # период целиком.
    day = _PERIOD_START[period_id] + timedelta(days=index % 7, hours=index % 6)
    payload = {
        "message_id": f"{period_id}_m{index}",
        "period_id": period_id,
        "date": day.strftime("%d.%m.%Y"),
        "datetime": day.isoformat(),
        "sentiment": sentiment,
        "views": views,
        "audience": audience,
        "engagement": engagement,
        "likes": engagement,
        "comments": 0,
        "reposts": 0,
        "text_clean": f"Сообщение {index} про {theme}",
        "message_link": f"https://example.com/{index}",
        "platform": "vk.com",
        "chat_title": f"Канал {index % 5}",
        "author": f"user{index}",
        "tags": tags,
        "event_title": theme,
    }
    return {
        "project_id": PROJECT_ID,
        "period_id": period_id,
        "table_name": "messages",
        "row_id": payload["message_id"],
        "payload": payload,
    }


rows = []
for period_id, count in ((PERIOD_ID, 60), (PERIOD_ID_2, 40)):
    for i in range(count):
        sentiment, theme, tags = _THEMES[i % len(_THEMES)]
        rows.append(
            _message_row(
                i,
                sentiment,
                views=8_000 * (i % 7 + 1),
                audience=4_000,
                engagement=90 + i % 30,
                theme=theme,
                tags=tags,
                period_id=period_id,
            )
        )

    for i, (sentiment, theme, _) in enumerate(_THEMES):
        rows.append(
            {
                "project_id": PROJECT_ID,
                "period_id": period_id,
                "table_name": "events",
                "row_id": f"{period_id}_e{i}",
                "payload": {
                    "event_id": f"{period_id}_e{i}",
                    "period_id": period_id,
                    "event_title": theme,
                    "event_summary": f"Инфоповод про {theme}",
                    "message_count": 12,
                    "negative_count": 12 if sentiment == "негатив" else 0,
                    "chat_count": 4,
                    "importance_score": 20 - i,
                    "start_date": "2026-04-24" if period_id == PERIOD_ID else "2026-05-01",
                    "end_date": "2026-04-30" if period_id == PERIOD_ID else "2026-05-07",
                    "main_tags": theme,
                },
            }
        )

CLIENT.db["platform_table_rows"] = rows

try:
    from src.app import main
except ModuleNotFoundError:
    from app import main

if __name__ == "__main__":
    main()
