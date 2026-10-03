# -*- coding: utf-8 -*-
"""Журнал ручных правок: кто, когда и что поменял.

Мутационные проверки (что ломает какой тест):
- cached_store.save_manual не пишет в журнал -> «правка названия в журнале»
  краснеет;
- save_manual не читает прежнее значение -> «было → стало в сводке»
  краснеет;
- delete_manual пишет в журнал и несуществующую правку -> «удаление
  несуществующей правки в журнал не попадает» краснеет;
- record без перехвата ошибок -> «журнал недоступен — правка всё равно
  сохранена» краснеет;
- current_actor без владельца -> «владелец записан владельцем» краснеет;
- audit_reason не добавляется к сводке -> «правки пересборки помечены»
  краснеет;
- render_audit_page без проверки владельца -> «не владельцу журнал закрыт»
  краснеет;
- load_log без досортировки по id -> «при равном времени выше запись с
  большим id» краснеет.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services import audit_log  # noqa: E402
from services import cached_store  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


NOW = datetime.now(timezone.utc).isoformat()
CLIENT.db["platform_projects"] = [
    {"project_id": "p", "project_name": "Такси", "status": "active", "settings": {}, "created_at": NOW, "updated_at": NOW},
    {"project_id": "q", "project_name": "Кнауф", "status": "active", "settings": {}, "created_at": NOW, "updated_at": NOW},
]
CLIENT.db["platform_manual_rows"] = []
CLIENT.db["platform_audit_log"] = []


def log():
    return CLIENT.db["platform_audit_log"]


print("1. Сводка словами")
check("переименование", audit_log.summarize("save", "event_edits", {"title": "Старое"}, {"title": "Новое"})
      == "Правка инфоповода: название: «Старое» → «Новое»")
check("новое описание", "описание: «Про тарифы»" in audit_log.summarize("save", "event_edits", {}, {"description": "Про тарифы"}))
check("скрытие инфоповода", "инфоповод скрыт" in audit_log.summarize("save", "event_edits", {}, {"hidden": True}))
check("отмена объединения", audit_log.summarize("delete", "event_merges", {}, None) == "Объединение инфоповодов отменено")
check("незнакомая таблица не теряется", "brand_metric_notes" in audit_log.summarize("save", "brand_metric_notes", {}, {}))

print("2. Правки попадают в журнал")
cached_store.save_manual("p", "event_edits", "event_edit::p1__e_1", {"event_id": "p1__e_1", "title": "Тарифы"})
cached_store.save_manual("p", "event_edits", "event_edit::p1__e_1", {"event_id": "p1__e_1", "title": "Новый тариф"})
check("правка названия в журнале", len(log()) == 2 and log()[1]["action"] == "save", str(log()))
check("было → стало в сводке", log()[1]["summary"] == "Правка инфоповода: название: «Тарифы» → «Новый тариф»", log()[1]["summary"])
check("прежнее и новое значение сохранены", log()[1]["before"] == {"event_id": "p1__e_1", "title": "Тарифы"}
      and log()[1]["after"]["title"] == "Новый тариф")
check("проект и ключ записаны", log()[1]["project_id"] == "p" and log()[1]["row_key"] == "event_edit::p1__e_1")
cached_store.delete_manual("p", "event_edit::p1__e_1")
check("отмена правки в журнале с таблицей и прежним значением",
      log()[-1]["action"] == "delete" and log()[-1]["summary"] == "Правка инфоповода отменена"
      and log()[-1]["before"]["title"] == "Новый тариф", str(log()[-1]))
count = len(log())
cached_store.delete_manual("p", "event_edit::нет_такой")
check("удаление несуществующей правки в журнал не попадает", len(log()) == count)
try:
    cached_store.save_manual("p", "event_edits", "event_edit::p1__e_2", {"title": "x"}, expected_updated_at="2020-01-01")
except store.ManualEditConflict:
    pass
check("конфликт версий — ни правки, ни записи в журнале", len(log()) == count)

print("3. Сбой журнала не ломает правку")
original_table = CLIENT.table


def no_audit_table(name):
    if name == "platform_audit_log":
        raise RuntimeError('relation "platform_audit_log" does not exist')
    return original_table(name)


CLIENT.table = no_audit_table
try:
    cached_store.save_manual("p", "message_hidden", "message_hidden::m1", {"message_id": "m1"})
    saved = any(r["row_key"] == "message_hidden::m1" for r in CLIENT.db["platform_manual_rows"])
    check("журнал недоступен — правка всё равно сохранена", saved)
except Exception as exc:  # noqa: BLE001
    check("журнал недоступен — правка всё равно сохранена", False, repr(exc))
finally:
    CLIENT.table = original_table

print("4. Причина: пересборка периода")
with audit_log.audit_reason("Пересборка периода"):
    cached_store.save_manual("p", "event_edits", "event_edit::p1__e_9", {"title": "Перенесено"})
check("правки пересборки помечены", log()[-1]["summary"].startswith("Пересборка периода: "), log()[-1]["summary"])
cached_store.save_manual("p", "event_edits", "event_edit::p1__e_10", {"title": "Обычная"})
check("после блока причина снята", not log()[-1]["summary"].startswith("Пересборка"), log()[-1]["summary"])

print("5. Автор правки из сессии приложения")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402


def edit_app():
    import streamlit as st

    from services import cached_store

    cached_store.save_manual("p", "event_edits", "event_edit::p1__e_20", {"title": st.session_state["title"]})


for role_state, title in (({"platform_project_role": "editor"}, "от аналитика"), ({"platform_is_admin": True}, "от владельца")):
    at = AppTest.from_function(edit_app, default_timeout=60)
    at.session_state["title"] = title
    at.session_state["_presence_session_id"] = "0123456789abcdef"
    for key, value in role_state.items():
        at.session_state[key] = value
    at.run()
last = [entry for entry in log() if entry["row_key"] == "event_edit::p1__e_20"]
check("аналитик записан аналитиком", len(last) == 2 and last[0]["actor_role"] == "editor", str([e["actor_role"] for e in last]))
check("владелец записан владельцем", len(last) == 2 and last[1]["actor_role"] == "owner", str([e["actor_role"] for e in last]))
check("сессия записана коротким ID", last and last[0]["actor_session"] == "0123456789ab", str(last[0]["actor_session"] if last else ""))
check("подпись автора", audit_log.actor_title("editor", "0123456789ab") == "Аналитик · сессия 012345")

print("6. Страница «Журнал»")
cached_store.save_manual("q", "manual_events", "manual_event::x", {"title": "Ручной инфоповод Кнауфа"})
st.cache_data.clear()
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
at.session_state["platform_is_admin"] = True
at.session_state["platform_nav_page"] = "Журнал"
at.run()
check("страница открылась", not at.exception, str(at.exception))
check("пункт «Журнал» в меню владельца", "Журнал" in [str(b.label) for b in at.sidebar.button])
table = next((el.value for el in at.dataframe if "Что" in el.value.columns), None)
check("в журнале видны правки обоих проектов", table is not None and {"Такси", "Кнауф"} <= set(table["Проект"]),
      str(table.head().to_dict("records")) if table is not None else "таблицы нет")
check("новые правки сверху", table is not None and table.iloc[0]["Проект"] == "Кнауф")
check("автор подписан по-человечески", table is not None and any("Аналитик · сессия" in who for who in table["Кто"]))
[s for s in at.selectbox if str(s.label) == "Проект"][0].set_value("p").run()
table = next((el.value for el in at.dataframe if "Что" in el.value.columns), None)
check("фильтр по проекту", table is not None and set(table["Проект"]) == {"Такси"})

from audit_ui import render_audit_page  # noqa: E402


def viewer_app():
    import pandas as pd

    from audit_ui import render_audit_page

    render_audit_page(pd.DataFrame(), is_admin=False)


at = AppTest.from_function(viewer_app, default_timeout=60)
at.run()
check("не владельцу журнал закрыт", any("владельцу платформы" in str(i.value) for i in at.info) and not at.dataframe)

st.cache_data.clear()
CLIENT.table = no_audit_table
try:
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
    at.session_state["platform_is_admin"] = True
    at.session_state["platform_nav_page"] = "Журнал"
    at.run()
    check("нет таблицы — предупреждение и подсказка про миграцию владельцу",
          any("недоступен" in str(w.value) for w in at.warning) and any("0007" in str(c.value) for c in at.caption),
          str([str(w.value) for w in at.warning]))
finally:
    CLIENT.table = original_table

print("7. Одинаковое время записи")
# Две правки с одним created_at — обычное дело на Windows, где часы тикают раз
# в 1–15 мс. Postgres отдаёт такие строки в произвольном порядке, и load_log
# досортировывает по id. Фейк при равенстве сам ставит выше позже вставленную
# строку, поэтому id здесь идут против порядка вставки: без второго ключа выше
# окажется «раньше», и проверка покраснеет.
SAME_TIME = "2026-01-01T00:00:00+00:00"
log().extend([
    {"id": 2, "project_id": "tie", "created_at": SAME_TIME, "summary": "позже"},
    {"id": 1, "project_id": "tie", "created_at": SAME_TIME, "summary": "раньше"},
])
tied = audit_log.load_log("tie")
check("при равном времени выше запись с большим id", list(tied["summary"]) == ["позже", "раньше"], str(list(tied["summary"])))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Журнал правок пишет, кто, когда и что поменял.")
