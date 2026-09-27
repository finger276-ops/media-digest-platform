# -*- coding: utf-8 -*-
"""Резервные копии: снимок, хранение, восстановление целиком и по проекту.

Главная проверка — полный круг: копия → база очищена → восстановление →
база та же до строки. Отдельно — восстановление одного проекта не трогает
другие, хранится ровно keep последних копий, отсутствующая таблица не рвёт
копию, а владелец видит, когда была последняя.

Мутационные проверки (что ломает какой тест):
- restore_archive без фильтра проекта -> «восстановлен только проект А»
  краснеет;
- restore_archive передаёт суррогатный id участников -> «id участника база
  выдаёт сама» краснеет;
- prune_backups сортирует от старых к новым -> «остались самые новые»
  краснеет;
- prune_backups без нижней границы keep -> «keep=0 — последняя копия
  остаётся» краснеет;
- dump_tables без перехвата -> «таблицы нет в базе — копия всё равно
  сделана» краснеет;
- restore_archive пишет при dry_run -> «пробный прогон ничего не пишет»
  краснеет;
- render_backups_block без проверки давности -> «копии больше двух суток —
  предупреждение» краснеет;
- журнал правок восстанавливается и в непустую таблицу -> «журнал правок не
  задваивается при повторном восстановлении» краснеет.
"""

import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services import backups  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


NOW = datetime(2026, 9, 27, 3, 17, tzinfo=timezone.utc)


def seed():
    CLIENT.db.clear()
    CLIENT.db["platform_projects"] = [
        {"project_id": "a", "project_name": "Альфа", "status": "active", "settings": {"x": 1}},
        {"project_id": "b", "project_name": "Бета", "status": "active", "settings": {}},
    ]
    CLIENT.db["platform_periods"] = [
        {"project_id": "a", "period_id": "a1", "period_name": "Апрель", "manifest": {"storage_path": "a/a1/f.xlsx"}},
        {"project_id": "b", "period_id": "b1", "period_name": "Май", "manifest": {}},
    ]
    CLIENT.db["platform_table_rows"] = [
        {"project_id": pid, "period_id": f"{pid}1", "table_name": "messages", "row_id": f"m{i}",
         "payload": {"message_id": f"m{i}", "text_clean": f"Сообщение {i} «кавычки» и ё"}}
        for pid in ("a", "b") for i in range(450)
    ]
    CLIENT.db["platform_manual_rows"] = [
        {"project_id": "a", "table_name": "event_edits", "row_key": "event_edit::a1__e_1",
         "payload": {"title": "Ручное"}, "updated_at": "2026-09-01T00:00:00+00:00"},
    ]
    CLIENT.db["platform_project_members"] = [
        {"id": 7, "project_id": "a", "user_email": "analyst@example.com", "role": "editor"},
    ]
    CLIENT.db["platform_tag_hierarchies"] = [{"project_id": "a", "structure": [{"tag": "Бренды", "tier": 1}]}]
    CLIENT.db["platform_ingest_sources"] = [{"source_key": "weekly", "project_id": "a", "title": "Отчёт"}]
    CLIENT.db["platform_ingest_queue"] = [{"task_id": "t1", "project_id": "a", "status": "done"}]
    CLIENT.db["platform_category_benchmarks"] = [{"project_id": "b", "period_id": "b1", "benchmarks": {"sov": 0.3}}]
    CLIENT.db["platform_audit_log"] = [
        {"id": 3, "project_id": "a", "created_at": "2026-09-01T00:00:00+00:00", "actor_role": "editor",
         "actor_session": "abc", "action": "save", "table_name": "event_edits", "row_key": "event_edit::a1__e_1",
         "summary": "Правка инфоповода", "before": None, "after": {"title": "Ручное"}},
    ]


def snapshot(tables=None):
    tables = tables or backups.BACKUP_TABLES
    return {t: sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in CLIENT.db.get(t, [])) for t in tables}


print("1. Копия: все таблицы, манифест, хранилище")
seed()
before = snapshot()
result = backups.make_backup(CLIENT, now=NOW)
check("копия в хранилище под именем с датой", result["path"] == "backups/platform_2026-09-27_031700.zip", result["path"])
check("файл действительно лежит в хранилище", result["path"] in CLIENT.files)
archive = zipfile.ZipFile(BytesIO(CLIENT.files[result["path"]]))
manifest = json.loads(archive.read("manifest.json"))
check("в манифесте все таблицы", set(manifest["tables"]) == set(backups.BACKUP_TABLES), str(manifest["tables"]))
check("строк данных — все 900, постранично", manifest["tables"]["platform_table_rows"] == 900, str(manifest["tables"]))
check("копия сжата", result["size"] < len(json.dumps(CLIENT.db["platform_table_rows"], ensure_ascii=False)) / 3)

print("2. Полный круг: база очищена и восстановлена")
for table in list(CLIENT.db):
    CLIENT.db[table] = []
counts = backups.restore_archive(CLIENT, CLIENT.files[result["path"]])
after = snapshot()
members = CLIENT.db["platform_project_members"]
check("id участника база выдаёт сама", members and "id" not in members[0], str(members))
for serial_table in ("platform_project_members", "platform_audit_log"):
    before[serial_table] = sorted(
        json.dumps({k: v for k, v in json.loads(r).items() if k != "id"}, sort_keys=True, ensure_ascii=False)
        for r in before[serial_table]
    )
check("база та же до строки", after == before, str({t: (len(before[t]), len(after[t])) for t in before if before[t] != after[t]}))
check("сводка по таблицам", counts["platform_table_rows"] == 900 and counts["platform_projects"] == 2, str(counts))

print("3. Восстановление одного проекта")
seed()
payload = backups.make_backup(CLIENT, now=NOW + timedelta(minutes=1))["bytes"]
CLIENT.db["platform_table_rows"] = [r for r in CLIENT.db["platform_table_rows"] if r["project_id"] != "a"]
CLIENT.db["platform_projects"] = [r for r in CLIENT.db["platform_projects"] if r["project_id"] != "a"]
CLIENT.db["platform_table_rows"][0]["payload"] = {"message_id": "m0", "text_clean": "изменено после копии"}
dry = backups.restore_archive(CLIENT, payload, project_id="a", dry_run=True)
check("пробный прогон ничего не пишет", not any(r["project_id"] == "a" for r in CLIENT.db["platform_projects"]))
check("пробный прогон считает строки проекта", dry["platform_table_rows"] == 450, str(dry))
backups.restore_archive(CLIENT, payload, project_id="a")
check("восстановлен только проект А", sum(r["project_id"] == "a" for r in CLIENT.db["platform_table_rows"]) == 450
      and CLIENT.db["platform_table_rows"][0]["payload"]["text_clean"] == "изменено после копии",
      str(CLIENT.db["platform_table_rows"][0]["payload"]))
check("проект А снова в списке", any(r["project_id"] == "a" for r in CLIENT.db["platform_projects"]))

audit_payload = backups.make_backup(CLIENT, now=NOW + timedelta(minutes=2), upload=False)["bytes"]
again = backups.restore_archive(CLIENT, audit_payload)
check("журнал правок не задваивается при повторном восстановлении",
      len(CLIENT.db["platform_audit_log"]) == 1 and again["platform_audit_log"] == 0, str(again.get("platform_audit_log")))

print("4. Хранятся только последние копии")
seed()
CLIENT.files.clear()
for day in range(1, 17):
    CLIENT.files[f"backups/platform_2026-09-{day:02d}_031700.zip"] = b"old"
CLIENT.files["backups/readme.txt"] = b"not a backup"
backups.make_backup(CLIENT, now=NOW, keep=14)
left = sorted(k for k in CLIENT.files if k.startswith("backups/platform_"))
check("осталось ровно 14", len(left) == 14, str(len(left)))
check("остались самые новые", left[-1].endswith("2026-09-27_031700.zip") and left[0].endswith("2026-09-04_031700.zip"), str(left[:2]))
check("чужие файлы папки не тронуты", "backups/readme.txt" in CLIENT.files)
backups.prune_backups(CLIENT, keep=0)
check("keep=0 — последняя копия остаётся", sum(k.startswith("backups/platform_") for k in CLIENT.files) == 1)

print("5. Сбои")
seed()
original_table = CLIENT.table


def table_without_benchmarks(name):
    if name == "platform_category_benchmarks":
        raise RuntimeError('relation "platform_category_benchmarks" does not exist')
    return original_table(name)


CLIENT.table = table_without_benchmarks
try:
    partial = backups.make_backup(CLIENT, now=NOW + timedelta(hours=1), upload=False)
    check("таблицы нет в базе — копия всё равно сделана", partial["skipped"] == ["platform_category_benchmarks"]
          and partial["tables"]["platform_projects"] == 2, str(partial["skipped"]))
except Exception as exc:  # noqa: BLE001
    check("таблицы нет в базе — копия всё равно сделана", False, repr(exc))
finally:
    CLIENT.table = original_table
try:
    backups.read_archive(b"not a zip")
    check("битый файл — понятная ошибка", False, "ошибки не было")
except backups.BackupError as exc:
    check("битый файл — понятная ошибка", "не похож на резервную копию" in str(exc), str(exc))

print("6. Командная строка")
import backup_platform  # noqa: E402

seed()
out = Path(tempfile.mkdtemp()) / "copy.zip"
code = backup_platform.main(["backup", "--no-upload", "--out", str(out)])
check("backup --out пишет файл", code == 0 and out.exists() and zipfile.is_zipfile(out))
for table in list(CLIENT.db):
    CLIENT.db[table] = []
code = backup_platform.main(["restore", "--file", str(out), "--yes"])
check("restore --file возвращает данные", code == 0 and len(CLIENT.db["platform_table_rows"]) == 900)

print("7. Блок владельца")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from backups_ui import backup_time  # noqa: E402

check("время копии читается из имени", backup_time("platform_2026-09-27_031700.zip") == NOW)
seed()
CLIENT.files.clear()


def owner_page():
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
    at.session_state["platform_is_admin"] = True
    at.session_state["platform_nav_page"] = "Проекты"
    at.run()
    return at


at = owner_page()
check("страница открылась", not at.exception, str(at.exception))
check("без копий — предупреждение", any("Резервных копий пока нет" in str(w.value) for w in at.warning))
[b for b in at.button if b.key == "backup_now"][0].click().run()
check("«Сделать копию сейчас» — копия в хранилище", any(k.startswith("backups/platform_") for k in CLIENT.files),
      str(list(CLIENT.files)))
check("сказано, что копия сделана", any("Копия сделана" in str(s.value) for s in at.success), str([str(s.value) for s in at.success]))
CLIENT.files.clear()
CLIENT.files["backups/platform_2026-01-01_031700.zip"] = b"old"
at = owner_page()
check("копии больше двух суток — предупреждение", any("больше двух суток" in str(w.value) for w in at.warning),
      str([str(w.value) for w in at.warning]))

st.cache_data.clear()
viewer = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
viewer.session_state["platform_project_id"] = "a"
viewer.session_state["platform_project_role"] = "editor"
viewer.session_state["platform_nav_page"] = "Настройки проекта"
viewer.run()
check("аналитику блок копий не показан", not any("Резервные копии" in str(h.value) for h in viewer.subheader))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Резервные копии делаются, хранятся и восстанавливаются.")
