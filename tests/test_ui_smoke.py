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

print("3. Страница ручной загрузки по-прежнему работает")
sidebar_radios = [r for r in at.sidebar.radio]
sidebar_radios[0].set_value("Загрузка файла").run()
check("страница загрузки без исключений", not at.exception, str(at.exception))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Дымовой тест интерфейса пройден.")
