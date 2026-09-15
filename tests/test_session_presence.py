"""Проверка живых сессий платформы (platform_sessions).

Supabase подменён поддельным клиентом, сети нет.

Главное свойство, которое здесь проверяется: heartbeat не должен затирать
started_at. Отметка активности идёт upsert'ом без этой колонки, поэтому
момент захода вкладки ставится один раз (default now() в БД) и дальше не
меняется — иначе «сколько активна сессия» всегда показывало бы ноль.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services.session_presence import (  # noqa: E402
    TABLE,
    cleanup_stale_sessions,
    list_recent_sessions,
    touch_session,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def rows():
    return CLIENT.db.setdefault(TABLE, [])


def iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


print("1. Первая отметка создаёт сессию")
CLIENT.db[TABLE] = []
touch_session("sess-1", project_id="tn_project", role="viewer")
check("строка сессии появилась", len(rows()) == 1, str(rows()))
check("проект записан", rows()[0].get("project_id") == "tn_project")
check("роль записана", rows()[0].get("role") == "viewer")
check("время последней активности проставлено", bool(rows()[0].get("last_seen_at")))

print("2. Повторный heartbeat не затирает момент захода")
# В боевой БД started_at ставит default now() при вставке; здесь проставляем
# его руками, чтобы проверить именно то, что upsert его не трогает.
rows()[0]["started_at"] = iso(10)
started_before = rows()[0]["started_at"]
# Сдвигаем отметку активности в прошлое: сравнивать два now() подряд нельзя —
# на Windows разрешение часов около миллисекунды, и они совпадают.
rows()[0]["last_seen_at"] = iso(10)
seen_before = rows()[0]["last_seen_at"]
touch_session("sess-1", project_id="tn_project", role="viewer")
check("новой строки не появилось", len(rows()) == 1, str(rows()))
check("started_at не изменился", rows()[0].get("started_at") == started_before, str(rows()[0]))
check("last_seen_at сдвинулся вперёд", rows()[0].get("last_seen_at") > seen_before, str(rows()[0]))

print("3. Смена проекта и роли пишется в ту же сессию")
touch_session("sess-1", project_id="other_project", role="editor")
check("та же строка, без дубля", len(rows()) == 1, str(rows()))
check("проект обновился", rows()[0].get("project_id") == "other_project")
check("роль обновилась", rows()[0].get("role") == "editor")
check("момент захода по-прежнему тот же", rows()[0].get("started_at") == started_before)

print("4. Разные вкладки — разные сессии")
touch_session("sess-2", project_id="tn_project", role="viewer")
check("две независимые сессии", len(rows()) == 2, str(rows()))

print("5. Мусорные входные данные игнорируются")
before = len(rows())
touch_session("", project_id="tn_project", role="viewer")
touch_session("sess-3", project_id="tn_project", role="хакер")
check("пустой session_id не создаёт строку", len(rows()) == before, str(rows()))
check("неизвестная роль не создаёт строку", all(r.get("role") != "хакер" for r in rows()))

print("6. Выборка активных за окно")
CLIENT.db[TABLE] = [
    {"session_id": "fresh", "project_id": "p1", "role": "viewer", "started_at": iso(20), "last_seen_at": iso(1)},
    {"session_id": "stale", "project_id": "p1", "role": "viewer", "started_at": iso(90), "last_seen_at": iso(60)},
]
recent = list_recent_sessions(window_seconds=30 * 60)
check("свежая сессия попала в выборку", "fresh" in set(recent["session_id"]), str(recent))
check("сессия старше окна отфильтрована", "stale" not in set(recent["session_id"]), str(recent))
check("временные колонки разобраны как даты", str(recent["last_seen_at"].dtype).startswith("datetime64"))

print("7. Очистка удаляет только давно неактивные")
CLIENT.db[TABLE] = [
    {"session_id": "keep", "project_id": "p1", "role": "viewer", "started_at": iso(30), "last_seen_at": iso(5)},
    {"session_id": "drop", "project_id": "p1", "role": "viewer", "started_at": iso(3000), "last_seen_at": iso(2000)},
]
cleanup_stale_sessions(older_than_seconds=24 * 3600)
remaining = {r["session_id"] for r in rows()}
check("активная сессия осталась", "keep" in remaining, str(remaining))
check("давно неактивная удалена", "drop" not in remaining, str(remaining))

print("8. Пустая таблица не ломает выборку")
CLIENT.db[TABLE] = []
empty = list_recent_sessions()
check("вернулся пустой DataFrame, а не исключение", empty.empty)
check("колонки на месте даже без данных", "last_seen_at" in empty.columns, str(list(empty.columns)))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки живых сессий пройдены.")
