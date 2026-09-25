# -*- coding: utf-8 -*-
"""Права на страницах: каждая страница закрывает сама, а не надеется на меню.

Разведка перед входом по email нашла несколько мест, где права держались
только на том, что пункта нет в боковом меню или что до кнопки «не дойдут»:

- «Автозагрузка» и «Сессии» не проверяли роль вовсе, «Загрузка файла» и
  «История периодов» не проверяли демо;
- «Обработать очередь сейчас» брала задачи всех проектов, а в очереди были
  видны файлы и ошибки чужих заказчиков (проверка захвата — в
  tests/test_ingest_queue.py, раздел 15);
- демо-режим и сброс счётчика ИИ в карточке проекта были доступны аналитику;
- демо-гость с кодом аналитика видел трейсбеки упавших разделов;
- скрытый проект оставался открытым во вкладке, где в него уже вошли, а у
  демо-проекта заодно пропадал режим «только чтение»;
- render_project_manager без аргументов получал права владельца.

Мутационные проверки (что ломает какой тест):
- убрать проверку в render_ingest_admin_page -> «Автозагрузка: пользователю
  закрыта» и «демо-аналитику закрыта» краснеют;
- убрать проверку в render_session_presence_page -> «Сессии: аналитику
  закрыты» краснеет;
- в render_upload_page / render_period_history снова проверять только роль ->
  «демо-аналитику загрузка закрыта» / «история закрыта» краснеют;
- показывать демо-блок карточки всем (убрать if is_admin) -> «аналитику
  демо-блок не показан» краснеет;
- при сохранении карточки аналитиком писать demo_mode из формы, а не как было
  -> не ловится: без показанного блока взять значение неоткуда, поэтому это
  стережёт «сохранение аналитиком не трогает демо»;
- _visible_orphans без фильтра -> «аналитику не видны задачи беты» краснеет;
- can_see_error_details без read_only -> «демо-аналитику трейсбеков нет»
  краснеет;
- убрать проверку project_row.empty в render_project_access -> «скрытый проект
  закрывается в открытой вкладке» краснеет;
- вернуть права по умолчанию is_admin=True -> «без аргументов прав нет»
  краснеет.
"""

import os
import sys
from datetime import datetime, timezone
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

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from services import ingest_queue as queue  # noqa: E402
from services.roles import can_see_error_details  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()


def project(project_id, name, *, status="active", settings=None):
    return {
        "project_id": project_id,
        "project_name": name,
        "status": status,
        "viewer_code_hash": store.hash_code(f"{project_id}-viewer"),
        "editor_code_hash": store.hash_code(f"{project_id}-editor"),
        "settings": settings or {},
        "created_at": NOW,
        "updated_at": NOW,
    }


CLIENT.db["platform_projects"] = [
    project("alpha", "Альфа"),
    project("beta", "Бета"),
    project("gamma", "Гамма-демо", settings={"demo_mode": True, "demo_ai_runs": 7}),
    project("hidden", "Скрытый", status="hidden"),
]
CLIENT.db["platform_periods"] = []
CLIENT.db["platform_sessions"] = []

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def page(name, *, role="none", read_only=False, is_admin=False, project_id="alpha"):
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "tests" / "pages_app.py"), default_timeout=60)
    at.session_state["page_name"] = name
    at.session_state["page_role"] = role
    at.session_state["page_read_only"] = read_only
    at.session_state["page_is_admin"] = is_admin
    at.session_state["page_project_id"] = project_id
    at.run()
    return at


def buttons(at):
    return [str(b.label) for b in at.button]


def infos(at):
    return [str(i.value) for i in at.info]


print("1. «Автозагрузка» проверяет роль и демо сама")
viewer = page("ingest", role="viewer")
check("Автозагрузка: пользователю закрыта", not viewer.exception and "Обработать очередь сейчас" not in buttons(viewer), str(buttons(viewer)))
demo_editor = page("ingest", role="editor", read_only=True)
check("Автозагрузка: демо-аналитику закрыта", "Обработать очередь сейчас" not in buttons(demo_editor))
check("и сказано почему — демо", any("демо" in i for i in infos(demo_editor)), str(infos(demo_editor)))
editor = page("ingest", role="editor")
check("Автозагрузка: аналитику открыта", not editor.exception and "Обработать очередь сейчас" in buttons(editor), str(editor.exception))
owner = page("ingest", role="owner", is_admin=True)
check("Автозагрузка: владельцу открыта", "Обработать очередь сейчас" in buttons(owner))

print("2. В очереди раздела — только свои задачи")
CLIENT.db["platform_ingest_queue"] = []
CLIENT.db["platform_ingest_sources"] = []
queue.upsert_source(source_key="alpha-src", project_id="alpha")
queue.upsert_source(source_key="beta-src", project_id="beta")
for name, key in (("alpha-file", "alpha-src"), ("beta-file", "beta-src"), ("typo-file", "alpha-scr")):
    queue.enqueue_task(
        storage_path=f"inbox/{name}.xlsx",
        original_filename=f"{name}.xlsx",
        file_sha256=f"hash-{name}",
        source_key=key,
    )


def queue_files(at):
    """Все ячейки всех таблиц раздела одной строкой (repr таблицы обрезается)."""
    cells = []
    for df in at.dataframe:
        cells.extend(str(v) for v in df.value.astype(str).to_numpy().ravel())
    return " ".join(cells)


editor = page("ingest", role="editor")
files = queue_files(editor)
check("аналитику видна своя задача без проекта", "alpha-file" in files, files[:300])
check("аналитику не видны задачи беты", "beta-file" not in files)
check("аналитику не видны ничьи задачи", "typo-file" not in files)
owner = page("ingest", role="owner", is_admin=True)
files = queue_files(owner)
check("владельцу видны своя и ничья, но не бета", "alpha-file" in files and "typo-file" in files and "beta-file" not in files, files[:300])

print("3. «Сессии» — только владельцу")
check("Сессии: аналитику закрыты", "Обновить" not in buttons(page("sessions", role="editor")))
check("Сессии: владельцу открыты", "Обновить" in buttons(page("sessions", is_admin=True)))

print("4. Загрузка и история проверяют демо")
upload_demo = page("upload", role="editor", read_only=True)
check("демо-аналитику загрузка закрыта", not upload_demo.file_uploader and any("демо" in i for i in infos(upload_demo)), str(infos(upload_demo)))
upload = page("upload", role="editor")
check("аналитику загрузка открыта", not upload.exception and bool(upload.file_uploader), str(upload.exception))
history_demo = page("history", role="editor", read_only=True)
check("демо-аналитику история закрыта", any("демо" in i for i in infos(history_demo)), str(infos(history_demo)))
history = page("history", role="editor")
check("аналитику история открыта", any("Периодов пока нет" in i for i in infos(history)), str(infos(history)))

print("5. Карточка проекта: демо-режим — решение владельца")
analyst_card = page("card", role="editor", project_id="gamma")
check("карточка открылась", not analyst_card.exception, str(analyst_card.exception))
check(
    "аналитику демо-блок не показан",
    not any(c.key == "demo_mode_gamma" for c in analyst_card.checkbox)
    and not any(e.label == "Демонстрационный проект" for e in analyst_card.expander),
)
analyst_card.button(key="save_project_gamma").click().run()
gamma = next(p for p in CLIENT.db["platform_projects"] if p["project_id"] == "gamma")
check(
    "сохранение аналитиком не трогает демо",
    gamma["settings"].get("demo_mode") is True and gamma["settings"].get("demo_ai_runs") == 7,
    str(gamma["settings"]),
)
owner_card = page("card", role="owner", is_admin=True, project_id="gamma")
check("владельцу демо-блок показан", any(c.key == "demo_mode_gamma" for c in owner_card.checkbox))
owner_card.checkbox(key="demo_ai_reset_gamma").check()
owner_card.button(key="save_project_gamma").click().run()
gamma = next(p for p in CLIENT.db["platform_projects"] if p["project_id"] == "gamma")
check("владелец по-прежнему обнуляет счётчик", gamma["settings"].get("demo_ai_runs") == 0, str(gamma["settings"]))

print("6. Права по умолчанию — никакие")
default = page("manager_default")
check(
    "без аргументов прав нет",
    not default.exception and any("доступно аналитику" in i for i in infos(default)),
    str(infos(default)),
)

print("7. Трейсбеки — только владельцу и аналитику, не в демо")
check("владельцу трейсбеки видны", can_see_error_details("owner", is_admin=True, read_only=False))
check("аналитику видны", can_see_error_details("editor", is_admin=False, read_only=False))
check("демо-аналитику трейсбеков нет", not can_see_error_details("editor", is_admin=False, read_only=True))
check("пользователю нет", not can_see_error_details("viewer", is_admin=False, read_only=False))

print("8. Скрытый проект закрывается и в открытой вкладке")


def app_as(project_id, role):
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
    at.session_state["platform_project_id"] = project_id
    at.session_state["platform_project_role"] = role
    at.run()
    return at


opened = app_as("hidden", "editor")
check("скрытый проект закрывается в открытой вкладке", not opened.exception and "platform_project_id" not in opened.session_state, str(opened.exception))
check("и сказано, что случилось", any("больше недоступен" in str(w.value) for w in opened.sidebar.warning))
side = [str(b.label) for b in opened.sidebar.button if b.label]
check("разделов записи не осталось", "Загрузка файла" not in side and "Автозагрузка" not in side, str(side))
alive = app_as("alpha", "editor")
check("действующий проект по-прежнему открыт", alive.session_state["platform_project_id"] == "alpha")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Каждая страница сама закрывает то, что не положено.")
