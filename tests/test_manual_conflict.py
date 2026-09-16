"""Оптимистическая блокировка ручных правок.

Все правки аналитика — описания инфоповодов, переносы сообщений, выводы по
метрикам, саммари — хранятся upsert-перезаписью строки в platform_manual_rows.
Раньше два редактора молча теряли работу друг друга: побеждал тот, кто
сохранил последним. Теперь save_manual умеет условную запись: сохранение
проходит, только если строка в базе всё ещё той версии, которую видел
редактор; иначе — ManualEditConflict, и база не меняется.

Мутационные проверки (что ломает какой тест):
- убрать проверку версии (писать всегда) → падают тесты «устаревшая версия»,
  «строка уже создана», «строку удалили»;
- бросать конфликт всегда → падают тесты «совпадающая версия», «создание»;
- сравнивать updated_at как строки, а не как время → падает тест
  «Timestamp против ISO-строки»;
- не разбивать составной on_conflict в фейке → падает тест «ровно одна
  строка после двух сохранений» (upsert плодил бы дубли);
- писать до проверки → падают проверки «после конфликта payload прежний».
"""

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services import metric_notes  # noqa: E402
from services.cached_store import get_manual_version  # noqa: E402
from services.manual_moderation import manual_versions  # noqa: E402

PROJECT = "proj-1"
failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def tick():
    """Развести версии по времени.

    updated_at — это datetime.now, а его разрешение на Windows около 15 мс:
    две записи в одном тике получили бы одинаковую версию, и проверки
    «версия сдвинулась» стали бы лотереей.
    """
    time.sleep(0.02)


def db_rows(row_key):
    return [
        r
        for r in CLIENT.db.get("platform_manual_rows", [])
        if r.get("project_id") == PROJECT and r.get("row_key") == row_key
    ]


print("1. Безусловная запись (легаси-путь) работает как раньше")
v1 = store.save_manual(PROJECT, "event_edits", "event_edit::e1", {"title": "Первый"})
check("возвращён updated_at", isinstance(v1, str) and bool(v1))
check("строка создана", len(db_rows("event_edit::e1")) == 1)
tick()
v2 = store.save_manual(PROJECT, "event_edits", "event_edit::e1", {"title": "Второй"})
rows = db_rows("event_edit::e1")
check("после двух сохранений ровно одна строка", len(rows) == 1, f"строк: {len(rows)}")
check("payload заменён", rows[0]["payload"] == {"title": "Второй"})
check("версия сдвинулась", v2 != v1)

print("2. Условное создание: ожидание «строки нет»")
v3 = store.save_manual(
    PROJECT, "message_moves", "message_move::m1", {"target_event_id": "e9"},
    expected_updated_at=None,
)
check("создание при отсутствии строки проходит", len(db_rows("message_move::m1")) == 1)
try:
    store.save_manual(
        PROJECT, "message_moves", "message_move::m1", {"target_event_id": "e2"},
        expected_updated_at=None,
    )
    check("повторное создание даёт конфликт", False)
except store.ManualEditConflict:
    check("повторное создание даёт конфликт", True)
check(
    "после конфликта payload прежний",
    db_rows("message_move::m1")[0]["payload"] == {"target_event_id": "e9"},
)

print("3. Условная перезапись: совпадающая и устаревшая версия")
tick()
v4 = store.save_manual(
    PROJECT, "message_moves", "message_move::m1", {"target_event_id": "e5"},
    expected_updated_at=v3,
)
check(
    "совпадающая версия — запись проходит",
    db_rows("message_move::m1")[0]["payload"] == {"target_event_id": "e5"},
)
try:
    store.save_manual(
        PROJECT, "message_moves", "message_move::m1", {"target_event_id": "e7"},
        expected_updated_at=v3,
    )
    check("устаревшая версия — конфликт", False)
except store.ManualEditConflict:
    check("устаревшая версия — конфликт", True)
check(
    "после конфликта payload прежний (v4)",
    db_rows("message_move::m1")[0]["payload"] == {"target_event_id": "e5"},
)

print("4. Версия сравнивается как время, а не как строка")
# Снимок страницы отдаёт pd.Timestamp (list_manual делает to_datetime),
# база — ISO-строку. Это одна и та же версия.
v5 = store.save_manual(
    PROJECT, "message_moves", "message_move::m1", {"target_event_id": "e8"},
    expected_updated_at=pd.to_datetime(v4),
)
check(
    "Timestamp против ISO-строки — запись проходит",
    db_rows("message_move::m1")[0]["payload"] == {"target_event_id": "e8"},
)
check("same_manual_version: None и None", store.same_manual_version(None, None))
check(
    "same_manual_version: строка и Timestamp",
    store.same_manual_version(v5, pd.to_datetime(v5)),
)
check("same_manual_version: разные", not store.same_manual_version(v5, v4))
check("same_manual_version: есть и нет", not store.same_manual_version(v5, None))

print("5. Строку удалили — ожидание версии тоже конфликт")
store.delete_manual(PROJECT, "message_move::m1")
try:
    store.save_manual(
        PROJECT, "message_moves", "message_move::m1", {"target_event_id": "e1"},
        expected_updated_at=v5,
    )
    check("запись поверх удалённой строки — конфликт", False)
except store.ManualEditConflict:
    check("запись поверх удалённой строки — конфликт", True)
check("удалённая строка не воскресла", len(db_rows("message_move::m1")) == 0)

print("6. Версии из снимка страницы (manual_versions, get_manual_version)")
state = {"manual_df": store.list_manual(PROJECT)}
versions = manual_versions(state)
check("версия существующей строки в снимке", versions.get("event_edit::e1") is not None)
check("отсутствующая строка — None", versions.get("event_edit::нет") is None)
check(
    "версия снимка совпадает с базой",
    store.same_manual_version(v2, versions.get("event_edit::e1")),
)
check(
    "get_manual_version: существующая строка",
    store.same_manual_version(v2, get_manual_version(PROJECT, "event_edit::e1")),
)
check(
    "get_manual_version: отсутствующая строка",
    get_manual_version(PROJECT, "нет-такой") is None,
)

print("7. Выводы по метрикам: версии и условное сохранение")
metric_notes.save_note(PROJECT, ["p1"], "sov", "Доля растёт")
note_versions = metric_notes.load_note_versions(PROJECT, ["p1"])
check("версия вывода найдена", note_versions.get("sov") is not None)
# Второй редактор сохранил позже — первый пишет от устаревшей версии.
tick()
metric_notes.save_note(PROJECT, ["p1"], "sov", "Чужая правка")
try:
    metric_notes.save_note(
        PROJECT, ["p1"], "sov", "Затираю чужое",
        expected_updated_at=note_versions.get("sov"),
    )
    check("устаревший вывод — конфликт", False)
except store.ManualEditConflict:
    check("устаревший вывод — конфликт", True)
check(
    "чужой вывод не затёрт",
    metric_notes.load_notes(PROJECT, ["p1"]).get("sov") == "Чужая правка",
)
fresh = metric_notes.load_note_versions(PROJECT, ["p1"]).get("sov")
metric_notes.save_note(
    PROJECT, ["p1"], "sov", "Осознанная правка", expected_updated_at=fresh
)
check(
    "свежая версия — вывод сохраняется",
    metric_notes.load_notes(PROJECT, ["p1"]).get("sov") == "Осознанная правка",
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Все проверки блокировки правок пройдены.")
