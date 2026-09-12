"""Дымовой тест интерфейса: приложение стартует и раздел «Автозагрузка» рисуется.

Supabase подменен поддельным клиентом, поэтому тест не ходит в сеть.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from datetime import datetime, timezone

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

now = datetime.now(timezone.utc).isoformat()
CLIENT.db["platform_projects"] = [
    {
        "project_id": "tn_project",
        "project_name": "ТЕХНОНИКОЛЬ",
        "status": "active",
        "settings": {},
        "created_at": now,
        "updated_at": now,
    }
]
PERIOD_ID = "p_2026_04"

CLIENT.db["platform_periods"] = [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "period_name": "24.04.2026–30.04.2026",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
        "source_filename": "week.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    }
]


def _message_row(index, sentiment, views, audience, engagement, theme):
    payload = {
        "message_id": f"m{index}",
        "period_id": PERIOD_ID,
        "date": "24.04.2026",
        "datetime": "2026-04-24T10:00:00",
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
        "author": f"user{index}",
        "tags": theme,
        "event_title": theme,
    }
    return {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "messages",
        "row_id": payload["message_id"],
        "payload": payload,
    }


_THEMES = [("позитив", "Запуск завода"), ("негатив", "Жалобы на монтаж"), ("нейтрал", "Отраслевая статистика")]
CLIENT.db["platform_table_rows"] = [
    _message_row(i, _THEMES[i % 3][0], 10_000 * (i + 1), 5_000, 120, _THEMES[i % 3][1])
    for i in range(12)
] + [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "events",
        "row_id": f"e{i}",
        "payload": {
            "event_id": f"e{i}",
            "period_id": PERIOD_ID,
            "event_title": theme,
            "event_summary": f"Инфоповод про {theme}",
            "message_count": 4,
            "negative_count": 4 if sentiment == "негатив" else 0,
            "chat_count": 2,
            "importance_score": 10 - i,
            "start_date": "2026-04-24",
            "end_date": "2026-04-30",
            "main_tags": theme,
        },
    }
    for i, (sentiment, theme) in enumerate(_THEMES)
]

CLIENT.db["platform_ingest_queue"] = [
    {
        "task_id": "ing_demo_1",
        "project_id": "tn_project",
        "source_key": "ba-weekly",
        "storage_path": "inbox/ba-weekly/week1.xlsx",
        "original_filename": "Выгрузка недели.xlsx",
        "file_sha256": "abc",
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
        "period_name": "",
        "error_message": "",
        "created_at": now,
        "finished_at": None,
    },
    {
        "task_id": "ing_demo_2",
        "project_id": "tn_project",
        "source_key": "ba-weekly",
        "storage_path": "inbox/ba-weekly/broken.xlsx",
        "original_filename": "broken.xlsx",
        "file_sha256": "def",
        "status": "error",
        "attempts": 3,
        "max_attempts": 3,
        "period_name": "",
        "error_message": "Не удалось прочитать файл выгрузки.",
        "created_at": now,
        "finished_at": now,
    },
]
CLIENT.db["platform_ingest_sources"] = [
    {
        "source_key": "ba-weekly",
        "project_id": "tn_project",
        "title": "Еженедельный отчет BA",
        "source_system": "brand_analytics",
        "params": {"similarity_threshold": 0.3},
        "is_active": True,
    }
]

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Запуск приложения от имени владельца платформы")
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=60)
at.session_state["platform_is_admin"] = True
at.run()
check("приложение стартовало без исключений", not at.exception, str(at.exception))
sidebar_radios = [r for r in at.sidebar.radio]
sections = sidebar_radios[0].options if sidebar_radios else []
check("раздел «Автозагрузка» есть в меню", "Автозагрузка" in sections, str(sections))

print("2. Открытие раздела «Автозагрузка»")
sidebar_radios[0].set_value("Автозагрузка").run()
check("раздел отрисовался без исключений", not at.exception, str(at.exception))

texts = [m.value for m in at.markdown] + [h.value for h in at.subheader] + [h.value for h in at.header]
check("заголовок раздела на месте", any("Автозагрузка" in str(t) for t in texts), str(texts)[:200])
check("блок очереди отрисован", any("Очередь автозагрузки" in str(t) for t in texts))
check("блок источников отрисован", any("Источники автозагрузки" in str(t) for t in texts))

metrics = {m.label: m.value for m in at.metric}
check("метрика «В очереди» = 1", any("В очереди" in k and v == "1" for k, v in metrics.items()), str(metrics))
check("метрика «Ошибка» = 1", any("Ошибка" in k and v == "1" for k, v in metrics.items()), str(metrics))

buttons = [b.label for b in at.button]
check("есть кнопка ручной обработки", "Обработать очередь сейчас" in buttons, str(buttons))

print("3. Раздел «Индексы бренда» на дашборде")
sidebar_radios = [r for r in at.sidebar.radio]
sidebar_radios[0].set_value("Дашборд").run()
check("дашборд открылся", not at.exception, str(at.exception))

section_radios = [r for r in at.radio if "Раздел аналитики" in str(r.label)]
if section_radios:
    options = section_radios[0].options
    check("«Индексы бренда» есть в разделах аналитики", "Индексы бренда" in options, str(options))
    section_radios[0].set_value("Индексы бренда").run()
    check("раздел отрисовался без исключений", not at.exception, str(at.exception))
    texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
    check("заголовок раздела на месте", any("Индексы бренда" in str(t) for t in texts))
    check("блок категорийной выгрузки на месте", any("Выгрузка по категории" in str(t) for t in texts))
    labels = {m.label: m.value for m in at.metric}
    check("карточка BPI отрисована", any("BPI" in str(k) for k in labels), str(list(labels)[:8]))
    check("карточка NSS отрисована", any(str(k) == "NSS" for k in labels), str(list(labels)[:8]))
    check(
        "SOV без выгрузки категории показывает прочерк",
        any(str(k) == "SOV" and v == "—" for k, v in labels.items()),
        str(labels),
    )
else:
    check("найден переключатель разделов аналитики", False, "радио «Раздел аналитики» не отрисовалось")

print("4. Страница ручной загрузки по-прежнему работает")
sidebar_radios = [r for r in at.sidebar.radio]
sidebar_radios[0].set_value("Загрузка файла").run()
check("страница загрузки без исключений", not at.exception, str(at.exception))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Дымовой тест интерфейса пройден.")
