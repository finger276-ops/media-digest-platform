# -*- coding: utf-8 -*-
"""Сохранённые виды: срез и фильтры ленты под именем, открытие по ссылке.

Мутационные проверки (что ломает какой тест):
- вид не запоминает срез -> «вид по ссылке: срез на месте» краснеет;
- вид не запоминает фильтры ленты -> «вид по ссылке: фильтры ленты на
  месте» краснеет;
- apply_view не сбрасывает фильтры, которых нет в виде -> «вид сбрасывает
  чужие фильтры» краснеет;
- ссылка применяется до меню не всегда (без раздела) -> «вид по ссылке
  открывает свой раздел» краснеет;
- выбор вида в списке не применяет его -> «выбор в списке применяет вид»
  краснеет;
- «Сохранить вид» доступен зрителю -> «зритель видит виды, но не
  сохраняет» краснеет;
- журнал без имени вида -> «сохранение вида — в журнале правок» краснеет.
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

PROJECT = "views"
NOW = datetime.now(timezone.utc).isoformat()
CLIENT.db["platform_projects"] = [{"project_id": PROJECT, "project_name": "Виды", "status": "active", "settings": {},
                                   "created_at": NOW, "updated_at": NOW}]
CLIENT.db["platform_periods"] = [{"project_id": PROJECT, "period_id": "p1", "period_name": "Апрель",
                                  "date_from": "2026-04-01", "date_to": "2026-04-07", "source_filename": "f.xlsx",
                                  "status": "active", "manifest": {}, "uploaded_at": NOW}]
CLIENT.db["platform_manual_rows"] = []
CLIENT.db["platform_audit_log"] = []
rows = []
for i in range(12):
    payload = {"message_id": f"m{i}", "period_id": "p1", "date": "2026-04-02", "datetime": f"2026-04-02T1{i % 10}:00:00",
               "platform": "vk.com" if i % 2 == 0 else "t.me", "chat_title": "Сообщество", "author": f"a{i}",
               "text_clean": f"Сообщение {i}", "message_link": f"https://example.com/{i}",
               "sentiment": "негатив" if i % 3 == 0 else "нейтрал",
               "tags": "Технониколь" if i < 8 else "Кнауф", "event_title": "", "views": 1, "audience": 1, "engagement": i}
    rows.append({"project_id": PROJECT, "period_id": "p1", "table_name": "messages", "row_id": payload["message_id"],
                 "payload": payload})
CLIENT.db["platform_table_rows"] = rows

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from saved_views_ui import describe_view, view_id  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def app(page="Сообщения", role="editor", state=None, view=None):
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
    at.session_state["platform_project_id"] = PROJECT
    at.session_state["platform_project_role"] = role
    at.session_state["platform_nav_page"] = page
    at.session_state[f"period_select_{PROJECT}"] = ["p1"]
    for key, value in (state or {}).items():
        at.session_state[key] = value
    if view:
        at.query_params["view"] = view
    at.run()
    return at


def multiselect(at, label):
    return next((m for m in at.multiselect if str(m.label) == label), None)


def shown_ids(at):
    return sorted(int(str(m.value).split()[-1]) for m in at.markdown
                  if str(m.value).startswith("Сообщение ") and str(m.value).split()[-1].isdigit())


print("1. Сохранить вид")
at = app(state={f"tag_slice::{PROJECT}": ["Технониколь"], f"messages_platform_filter_{PROJECT}": ["vk.com"],
                f"messages_tone_filter_{PROJECT}": ["Негатив"], "messages_block_mode": "Вся лента"})
check("страница открылась", not at.exception, str(at.exception))
check("выбор вида на месте", any(str(s.label) == "Сохранённый вид" for s in at.selectbox))
name_input = next((t for t in at.text_input if str(t.label) == "Название вида"), None)
name_input.input("ТН · негатив · vk.com").run()
next(b for b in at.button if str(b.label) == "Сохранить этот вид").click().run()
VIEW = view_id("ТН · негатив · vk.com")
saved = {row["row_key"]: row["payload"] for row in CLIENT.db["platform_manual_rows"]}
view = saved.get(f"saved_view::{VIEW}")
check("вид сохранён в правках проекта", view is not None and view.get("name") == "ТН · негатив · vk.com"
      and view.get("page") == "Сообщения", str(list(saved)))
check("в вид вошли срез и фильтры", view is not None and view["state"].get("slice") == ["Технониколь"]
      and view["state"].get("platforms") == ["vk.com"] and view["state"].get("tones") == ["Негатив"],
      str(view.get("state") if view else None))
check("ссылка на вид — в адресе", at.query_params.get("view") == [VIEW] or at.query_params.get("view") == VIEW,
      str(dict(at.query_params)))
check("после сохранения вид выбран", next(s for s in at.selectbox if str(s.label) == "Сохранённый вид").value == VIEW)
check("сохранение вида — в журнале правок",
      any(entry.get("summary") == "Вид сохранён: «ТН · негатив · vk.com»" for entry in CLIENT.db["platform_audit_log"]),
      str([e.get("summary") for e in CLIENT.db["platform_audit_log"]]))
check("описание вида", describe_view(view) == "Сообщения · срез: Технониколь · площадка: vk.com · тональность: негатив",
      describe_view(view))

print("2. Открыть вид по ссылке")
# Хвост прошлого просмотра — поиск, которого в виде нет: вид его сбрасывает.
at = app(page="Обзор", role="viewer", view=VIEW, state={"full_feed_search": "Сообщение 0"})
check("по ссылке открылось без ошибок", not at.exception, str(at.exception))
check("вид по ссылке открывает свой раздел", at.session_state["platform_nav_page"] == "Сообщения")
check("вид по ссылке: срез на месте", list(multiselect(at, "Срез по тегам").value) == ["Технониколь"]
      if multiselect(at, "Срез по тегам") else False)
check("вид по ссылке: фильтры ленты на месте", multiselect(at, "Площадка") is not None
      and list(multiselect(at, "Площадка").value) == ["vk.com"] and list(multiselect(at, "Тональность").value) == ["Негатив"])
search = next((t for t in at.text_input if str(t.label) == "Поиск по всей ленте"), None)
check("вид сбрасывает чужие фильтры", search is not None and search.value == "", str(search.value if search else None))
check("лента — по виду", shown_ids(at) == [0, 6], str(shown_ids(at)))
check("зритель видит виды, но не сохраняет", any(str(s.label) == "Сохранённый вид" for s in at.selectbox)
      and not any(str(t.label) == "Название вида" for t in at.text_input))

at = app(page="Обзор", role="viewer", view="нет_такого")
check("неизвестный вид в ссылке — без ошибки", not at.exception and at.session_state["platform_nav_page"] == "Обзор")

print("3. Выбрать вид в списке")
at = app(page="Обзор", role="viewer")
next(s for s in at.selectbox if str(s.label) == "Сохранённый вид").set_value(VIEW).run()
check("выбор в списке применяет вид", at.session_state["platform_nav_page"] == "Сообщения"
      and multiselect(at, "Площадка") is not None and list(multiselect(at, "Площадка").value) == ["vk.com"],
      str(at.session_state["platform_nav_page"]))
next(s for s in at.selectbox if str(s.label) == "Сохранённый вид").set_value("").run()
check("«не выбран» убирает вид из ссылки", "view" not in at.query_params, str(dict(at.query_params)))

print("4. Удалить вид")
at = app(role="editor", view=VIEW)
next(b for b in at.button if str(b.label).startswith("Удалить вид")).click().run()
check("вид удалён", f"saved_view::{VIEW}" not in {row["row_key"] for row in CLIENT.db["platform_manual_rows"]})
check("после удаления вида нет в списке", not at.exception
      and VIEW not in list(next(s for s in at.selectbox if str(s.label) == "Сохранённый вид").options))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Сохранённые виды сохраняются, открываются по ссылке и из списка.")
