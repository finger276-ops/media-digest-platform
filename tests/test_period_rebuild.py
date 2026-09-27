# -*- coding: utf-8 -*-
"""Пересборка периода из сохранённого исходного файла.

Старый период пересобирается текущим конвейером из файла в хранилище.
Главный риск — ручные правки: у выгрузок без разметки сюжетов ID инфоповода —
порядковый номер кластера (e_00012), и после пересборки номера перетасуются.
Правка, записанная на «e_00012», прилипла бы к чужому инфоповоду. Поэтому
старые инфоповоды сопоставляются с новыми по составу сообщений, а правки без
пары откладываются в архив.

«Старый алгоритм» здесь имитируется перестановкой номеров инфоповодов в
сохранённых таблицах: правки записаны на старые номера, пересборка
восстанавливает настоящие — и правки обязаны уехать вслед за содержимым.

Мутационные проверки (что ломает какой тест):
- match_events без порога совпадения -> «инфоповод с другим составом не
  сопоставлен» краснеет;
- plan_manual_rows не переписывает ключи (всё unchanged) -> «правка названия
  уехала за содержимым» краснеет;
- plan_manual_rows без проверки занятых ключей -> «два правки на один
  инфоповод: вторая отложена» краснеет;
- apply_manual_plan пишет новые ключи до удаления старых -> «цепочка
  переименований без потерь» краснеет;
- save_processed_tables не удаляет устаревшие строки -> «лишних строк не
  осталось» краснеет;
- правки без пары не откладываются, а остаются -> «правка без пары отложена в
  архив» краснеет;
- save_processed_tables сбрасывает статус (status="active") -> «статус
  «скрыт» сохранён» краснеет;
- save_processed_tables сначала удаляет строки (keep_old_rows_until_written
  не работает) -> «сбой записи: старые строки на месте» краснеет;
- rebuild_period не сохраняет uploaded_at -> «дата загрузки не сдвинулась»
  краснеет.
"""

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from loadtest_pipeline import build_raw_export  # noqa: E402
from services import period_rebuild as rb  # noqa: E402
from services.ingest import ingest_file_bytes  # noqa: E402
from services.metrics_compute import SOURCE_METRICS_COLUMN  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Сопоставление инфоповодов по составу сообщений")
old = {"P__e_1": {"a", "b", "c", "d"}, "P__e_2": {"e", "f", "g"}, "P__e_3": {"x", "y"}}
new = {"P__e_1": {"e", "f", "g"}, "P__e_2": {"a", "b", "c"}, "P__e_9": {"q", "r", "x"}}
mapping = rb.match_events(old, new)
check("перенумерованные инфоповоды найдены по содержимому", mapping.get("P__e_1") == "P__e_2" and mapping.get("P__e_2") == "P__e_1", str(mapping))
check("инфоповод с другим составом не сопоставлен", "P__e_3" not in mapping, str(mapping))
check("тот же ID при равном совпадении предпочтительнее", rb.match_events({"P__e_5": {"a"}}, {"P__e_4": {"a"}, "P__e_5": {"a"}}) == {"P__e_5": "P__e_5"})

print("2. План переноса ручных правок")


def manual(rows):
    return pd.DataFrame([{"table_name": t, "row_key": k, "payload": p} for t, k, p in rows])


plan = rb.plan_manual_rows(
    manual(
        [
            ("event_edits", "event_edit::P__e_1", {"event_id": "P__e_1", "title": "Первый"}),
            ("event_edits", "event_edit::P__e_2", {"event_id": "P__e_2", "title": "Второй"}),
            ("event_edits", "event_edit::P__e_3", {"event_id": "P__e_3", "title": "Без пары"}),
            ("event_edits", "event_edit::Q__e_1", {"event_id": "Q__e_1", "title": "Другой период"}),
            ("message_hidden", "message_hidden::m1", {"message_id": "m1"}),
            ("event_merges", "event_merge::P__e_1", {"source_event_id": "P__e_1", "target_event_id": "Q__e_7"}),
            ("message_moves", "message_move::m5", {"message_id": "m5", "target_event_id": "P__e_2"}),
            ("message_irrelevant", "message_irrelevant::P__e_1::m6", {"event_id": "P__e_1", "message_id": "m6"}),
            ("manual_events", "manual_event::x", {"event_id": "manual_x", "title": "Ручной"}),
        ]
    ),
    "P",
    mapping,
)
rewrites = {old_key: (new_key, payload) for old_key, new_key, _t, payload in plan.rewrite}
check(
    "обмен ключами между двумя правками без потерь",
    rewrites.get("event_edit::P__e_1", ("",))[0] == "event_edit::P__e_2"
    and rewrites.get("event_edit::P__e_2", ("",))[0] == "event_edit::P__e_1",
    str(rewrites),
)
check("ID в теле правки тоже переписан", rewrites.get("event_edit::P__e_1", ("", {}))[1].get("event_id") == "P__e_2")
check("правка без пары отложена", [k for k, *_ in plan.orphan] == ["event_edit::P__e_3"], str(plan.orphan))
check("правки другого периода и сообщений не тронуты", "event_edit::Q__e_1" not in rewrites and "message_hidden::m1" not in rewrites)
check("склейка с инфоповодом другого периода: источник переписан", rewrites.get("event_merge::P__e_1", ("", {}))[1].get("target_event_id") == "Q__e_7")
check("перенос сообщения: цель переписана", rewrites.get("message_move::m5", ("", {}))[1].get("target_event_id") == "P__e_1")
check("«не относится»: ключ переписан", rewrites.get("message_irrelevant::P__e_1::m6", ("",))[0] == "message_irrelevant::P__e_2::m6")

collide = rb.plan_manual_rows(
    manual(
        [
            ("event_edits", "event_edit::P__e_1", {"event_id": "P__e_1", "title": "А"}),
            ("event_edits", "event_edit::P__e_2", {"event_id": "P__e_2", "title": "Б"}),
        ]
    ),
    "P",
    {"P__e_1": "P__e_5", "P__e_2": "P__e_5"},
)
check(
    "две правки на один инфоповод: вторая отложена",
    len(collide.rewrite) == 1 and len(collide.orphan) == 1,
    f"{collide.rewrite} | {collide.orphan}",
)
same = rb.plan_manual_rows(
    manual([("event_merges", "event_merge::P__e_1", {"source_event_id": "P__e_1", "target_event_id": "P__e_2"})]),
    "P",
    {"P__e_1": "P__e_5", "P__e_2": "P__e_5"},
)
check("склейка двух инфоповодов, собравшихся в один, отложена", len(same.orphan) == 1 and not same.rewrite)

print("3. Пересборка настоящего периода")
NOW = datetime.now(timezone.utc).isoformat()
PROJECT = "rebuild"
CLIENT.db["platform_projects"] = [
    {"project_id": PROJECT, "project_name": "Пересборка", "status": "active", "settings": {},
     "created_at": NOW, "updated_at": NOW}
]
CLIENT.db["platform_periods"] = []
CLIENT.db["platform_table_rows"] = []
CLIENT.db["platform_manual_rows"] = []
WORK_DIR = tempfile.mkdtemp(prefix="rebuild_")

raw = build_raw_export(messages=400, stories=12, chats=40, authors=150, days=5, seed=7, branch="algo")
result = ingest_file_bytes(raw.to_csv(index=False).encode("utf-8"), project_id=PROJECT,
                           source_filename="april.csv", source_system="generic", work_dir=WORK_DIR)
PID = str(result["period_id"])
true_members = rb.event_members(
    store.load_table(PROJECT, [PID], "event_discussions"),
    store.load_table(PROJECT, [PID], "discussion_messages"),
    PID,
)
real = sorted((eid for eid in true_members if not eid.endswith("e_residual")), key=lambda e: -len(true_members[e]))
check("в периоде есть кластерные инфоповоды", len(real) >= 4, str(real))
A, B, C = real[0], real[1], real[2]

# «Старый алгоритм»: номера инфоповодов переставлены по кругу.
plain = [eid.split("__", 1)[1] for eid in real]
shift = {plain[i]: plain[(i + 1) % len(plain)] for i in range(len(plain))}
for row in CLIENT.db["platform_table_rows"]:
    if row["period_id"] == PID and row["table_name"] in ("events", "event_discussions"):
        payload = row["payload"]
        payload["event_id"] = shift.get(payload["event_id"], payload["event_id"])
    if row["period_id"] == PID and row["table_name"] == "messages":
        row["payload"].pop(SOURCE_METRICS_COLUMN, None)


def old_id(eid):
    return f"{PID}__{shift[eid.split('__', 1)[1]]}"


message_a = sorted(true_members[A])[0]
message_a2 = sorted(true_members[A])[1]
for table, key, payload in [
    ("event_edits", f"event_edit::{old_id(A)}", {"event_id": old_id(A), "title": "Ручное название A"}),
    # Цепочка: правка A лежит на ключе, который после пересборки займёт правка
    # B, — запись нового ключа раньше удаления старого стёрла бы одну из них.
    ("event_edits", f"event_edit::{old_id(B)}", {"event_id": old_id(B), "title": "Ручное название B"}),
    ("event_merges", f"event_merge::{old_id(B)}", {"source_event_id": old_id(B), "target_event_id": old_id(C)}),
    ("message_moves", f"message_move::{message_a}", {"message_id": message_a, "target_event_id": old_id(C)}),
    ("message_irrelevant", f"message_irrelevant::{old_id(A)}::{message_a2}", {"event_id": old_id(A), "message_id": message_a2}),
    ("event_edits", f"event_edit::{PID}__e_09999", {"event_id": f"{PID}__e_09999", "title": "Без пары"}),
    ("event_edits", "event_edit::other__e_00001", {"event_id": "other__e_00001", "title": "Другой период"}),
]:
    store.save_manual(PROJECT, table, key, payload)
# Строка, которой в новой сборке не будет: пересборка обязана её убрать.
CLIENT.db["platform_table_rows"].append(
    {"project_id": PROJECT, "period_id": PID, "table_name": "events", "row_id": "e_obsolete",
     "payload": {"event_id": "e_obsolete", "event_title": "Устаревший"}}
)
store.update_period_metadata(PROJECT, PID, period_name="Апрельская волна", status="hidden",
                             manifest_updates={"comment": "проверить тарифы"})
before = store.list_periods(PROJECT, include_inactive=True).iloc[0].to_dict()

summary = rb.rebuild_period(PROJECT, PID, work_dir=WORK_DIR)
after = store.list_periods(PROJECT, include_inactive=True).iloc[0].to_dict()
keys = {r["row_key"]: r for r in CLIENT.db["platform_manual_rows"]}

check("сводка: правки перенесены", summary["edits_moved"] >= 4, str(summary))
check("сводка: одна правка без пары", summary["edits_orphaned"] == 1, str(summary))
check(
    "правка названия уехала за содержимым",
    keys.get(f"event_edit::{A}", {}).get("payload", {}).get("title") == "Ручное название A",
    str([k for k in keys if k.startswith("event_edit")]),
)
check(
    "цепочка переименований без потерь",
    keys.get(f"event_edit::{B}", {}).get("payload", {}).get("title") == "Ручное название B",
    str({k: v["payload"].get("title") for k, v in keys.items() if k.startswith("event_edit")}),
)
check(
    "склейка уехала за содержимым",
    keys.get(f"event_merge::{B}", {}).get("payload", {}).get("target_event_id") == C,
    str([k for k in keys if k.startswith("event_merge")]),
)
check("перенос сообщения указывает на тот же инфоповод", keys.get(f"message_move::{message_a}", {}).get("payload", {}).get("target_event_id") == C)
check("«не относится» переписано", f"message_irrelevant::{A}::{message_a2}" in keys)
check("правка другого периода не тронута", "event_edit::other__e_00001" in keys)
orphans = [r for r in CLIENT.db["platform_manual_rows"] if r["table_name"] == rb.ORPHANS_TABLE]
check(
    "правка без пары отложена в архив",
    len(orphans) == 1 and orphans[0]["payload"]["original_row_key"] == f"event_edit::{PID}__e_09999"
    and f"event_edit::{PID}__e_09999" not in keys,
    str(orphans),
)

from services.dashboard_data import prepare_period_data  # noqa: E402

events, messages, _state = prepare_period_data(PROJECT, [PID])
titled = events[events["event_id"].astype(str) == A]
check("на экране название стоит у инфоповода с тем же составом", not titled.empty and titled.iloc[0]["event_title"] == "Ручное название A",
      str(titled[["event_id", "event_title"]].to_dict("records")) if not titled.empty else "нет строки")
check("название сохранено", after["period_name"] == "Апрельская волна")
check("статус «скрыт» сохранён", after["status"] == "hidden", str(after["status"]))
check("комментарий сохранён", after["manifest"].get("comment") == "проверить тарифы")
check("дата загрузки не сдвинулась", pd.Timestamp(after["uploaded_at"]) == pd.Timestamp(before["uploaded_at"]),
      f"{before['uploaded_at']} → {after['uploaded_at']}")
check("в манифесте отмечена пересборка", after["manifest"].get("rebuild_count") == 1 and after["manifest"].get("rebuilt_at"))
check("исходный файл тот же", after["manifest"].get("storage_path") == before["manifest"].get("storage_path"))
check("новые признаки алгоритма доехали до старого периода", SOURCE_METRICS_COLUMN in messages.columns
      and messages[SOURCE_METRICS_COLUMN].notna().all())
stored_events = [r for r in CLIENT.db["platform_table_rows"] if r["period_id"] == PID and r["table_name"] == "events"]
check("лишних строк не осталось", len(stored_events) == summary["events"], f"{len(stored_events)} vs {summary['events']}")

print("4. Когда пересобрать нельзя")
store.update_period_metadata(PROJECT, PID, manifest_updates={"storage_path": ""})
try:
    rb.rebuild_period(PROJECT, PID, work_dir=WORK_DIR)
    check("без сохранённого файла — понятная ошибка", False, "пересборка прошла")
except rb.RebuildError as exc:
    check("без сохранённого файла — понятная ошибка", "не сохранён" in str(exc), str(exc))
store.update_period_metadata(PROJECT, PID, manifest_updates={"storage_path": "nowhere/file.csv"})
try:
    rb.rebuild_period(PROJECT, PID, work_dir=WORK_DIR)
    check("файла нет в хранилище — понятная ошибка", False, "пересборка прошла")
except rb.RebuildError as exc:
    check("файла нет в хранилище — понятная ошибка", "не найден в хранилище" in str(exc), str(exc))
try:
    rb.rebuild_period(PROJECT, "нет_такого", work_dir=WORK_DIR)
    check("несуществующий период — понятная ошибка", False, "пересборка прошла")
except rb.RebuildError as exc:
    check("несуществующий период — понятная ошибка", "не найден" in str(exc), str(exc))
store.update_period_metadata(PROJECT, PID, manifest_updates={"storage_path": before["manifest"]["storage_path"]})

print("5. Сбой посреди записи не оставляет период пустым")
rows_before = [dict(r) for r in CLIENT.db["platform_table_rows"] if r["period_id"] == PID]
manifest_before = store.list_periods(PROJECT, include_inactive=True).iloc[0]["manifest"]
original_table = CLIENT.table


class FailingTable:
    def __init__(self, name):
        self._inner = original_table(name)
        self._name = name

    def upsert(self, payload, on_conflict=None):
        if self._name == "platform_table_rows":
            raise RuntimeError("обрыв связи")
        return self._inner.upsert(payload, on_conflict=on_conflict)

    def __getattr__(self, attr):
        return getattr(self._inner, attr)


CLIENT.table = lambda name: FailingTable(name)
try:
    rb.rebuild_period(PROJECT, PID, work_dir=WORK_DIR)
    check("сбой записи виден", False, "ошибки не было")
except RuntimeError:
    check("сбой записи виден", True)
finally:
    CLIENT.table = original_table
rows_after = [r for r in CLIENT.db["platform_table_rows"] if r["period_id"] == PID]
check("сбой записи: старые строки на месте", len(rows_after) == len(rows_before), f"{len(rows_before)} → {len(rows_after)}")
check("сбой записи: метаданные периода прежние",
      store.list_periods(PROJECT, include_inactive=True).iloc[0]["manifest"].get("rebuild_count") == manifest_before.get("rebuild_count"))

print("6. Кнопки на странице «История периодов»")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

CLIENT.db["platform_periods"].append(
    {"project_id": PROJECT, "period_id": "old_no_file", "period_name": "Без файла", "status": "active",
     "manifest": {}, "source_filename": "x.csv", "date_from": "2026-03-01", "date_to": "2026-03-07",
     "uploaded_at": "2026-03-08T00:00:00+00:00"}
)
st.cache_data.clear()
at = AppTest.from_file(str(REPO / "tests" / "pages_app.py"), default_timeout=180)
at.session_state["page_name"] = "history"
at.session_state["page_role"] = "editor"
at.session_state["page_project_id"] = PROJECT
at.session_state["page_work_dir"] = WORK_DIR
at.run()
check("страница открылась", not at.exception, str(at.exception))
button = [b for b in at.button if b.key == "rebuild_all_periods"]
check("кнопка «Пересобрать все» на месте и считает только периоды с файлом", bool(button) and "(1)" in str(button[0].label),
      str([b.label for b in at.button]))
check("сказано, сколько периодов без файла", any("Без сохранённого файла: 1" in str(c.value) for c in at.caption))
if button:
    button[0].click().run()
    successes = [str(s.value) for s in at.success]
    check("после нажатия — сводка пересборки", any("Апрельская волна" in s and "инфоповодов" in s for s in successes),
          str(successes) + str([str(e.value) for e in at.error]))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Пересборка периода сохраняет правки и метаданные.")
