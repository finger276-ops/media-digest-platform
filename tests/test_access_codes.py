# -*- coding: utf-8 -*-
"""Коды доступа к проектам: один код — один проект.

Вход по коду перебирает проекты по алфавиту и раньше открывал первый, чей
хеш совпал. Уникальность кодов нигде не проверялась, поэтому одинаковый код
у двух проектов открывал заказчику чужой проект, а код аналитика одного
проекта мог сработать как код пользователя другого. Вторая дыра рядом:
project_id выводится из названия, и create_project писал его upsert'ом —
проект с тем же названием молча перезаписывал коды существующего. Аналитик
сам заводит проекты и чужих не видит, так что мог перехватить чужой проект,
не подозревая об этом.

Мутационные проверки (что ломает какой тест):
- access_code_problem перестаёт сверять коды с другими проектами -> тесты
  «занятый код … отклонён» краснеют;
- убрать проверку совпадения кода пользователя и кода аналитика -> тесты
  «одинаковые коды внутри проекта отклонены» краснеют;
- resolve_project_access снова отдаёт первое совпадение -> тесты «при
  дубле в базе вход закрыт» краснеют;
- create_project снова пишет upsert'ом без нового id при совпадении
  названия -> тест «проект с тем же названием не перезаписывает чужой»
  краснеет;
- убрать перехват AccessCodeError в форме создания проекта -> тест формы
  «ошибка показана словами, а не исключением» краснеет.
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
NOW = datetime.now(timezone.utc).isoformat()

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def project(project_id, name, viewer, editor, status="active"):
    return {
        "project_id": project_id,
        "project_name": name,
        "status": status,
        "viewer_code_hash": store.hash_code(viewer),
        "editor_code_hash": store.hash_code(editor),
        "settings": {},
        "created_at": NOW,
        "updated_at": NOW,
    }


def seed():
    CLIENT.db["platform_projects"] = [
        project("alpha", "Альфа", "alpha-viewer", "alpha-editor"),
        project("beta", "Бета", "beta-viewer", "beta-editor"),
        project("old", "Архивный", "old-viewer", "old-editor", status="archived"),
    ]


def rejected(call):
    """Текст AccessCodeError или None, если вызов прошёл."""
    try:
        call()
    except store.AccessCodeError as exc:
        return str(exc)
    return None


def project_count():
    return len(CLIENT.db["platform_projects"])


def row(project_id):
    return next(p for p in CLIENT.db["platform_projects"] if p["project_id"] == project_id)


print("1. Новый проект не может взять код, занятый другим проектом")
seed()
reason = rejected(lambda: store.create_project(
    project_name="Гамма", viewer_code="alpha-viewer", editor_code="gamma-editor"))
check("занятый код пользователя отклонён", reason is not None, str(reason))
check("ошибка объясняет причину по-русски", reason is not None and "занят другим проектом" in reason, str(reason))
check("отказ не создал проект", project_count() == 3, str(project_count()))

reason = rejected(lambda: store.create_project(
    project_name="Гамма", viewer_code="gamma-viewer", editor_code="beta-viewer"))
check("код аналитика, совпадающий с чужим кодом пользователя, отклонён", reason is not None, str(reason))

reason = rejected(lambda: store.create_project(
    project_name="Гамма", viewer_code="old-editor", editor_code="gamma-editor"))
check("коды архивного проекта тоже заняты — его могут вернуть в работу", reason is not None, str(reason))

print("2. Код пользователя и код аналитика проекта различаются")
seed()
reason = rejected(lambda: store.create_project(
    project_name="Гамма", viewer_code="same", editor_code="same"))
check("одинаковые коды внутри нового проекта отклонены", reason is not None, str(reason))
check("отказ не создал проект", project_count() == 3, str(project_count()))

print("3. Уникальные коды проходят, проект открывается своим кодом")
seed()
new_id = store.create_project(
    project_name="Гамма", viewer_code="gamma-viewer", editor_code="gamma-editor")
check("проект создан", project_count() == 4, str(project_count()))
check("код аналитика открывает новый проект", store.resolve_project_access("gamma-editor") == (new_id, "editor"),
      str(store.resolve_project_access("gamma-editor")))
check("код пользователя открывает новый проект", store.resolve_project_access("gamma-viewer") == (new_id, "viewer"),
      str(store.resolve_project_access("gamma-viewer")))
check("старые коды по-прежнему открывают свои проекты",
      store.resolve_project_access("alpha-editor") == ("alpha", "editor"),
      str(store.resolve_project_access("alpha-editor")))

print("4. Проект с тем же названием не перезаписывает чужой")
seed()
twin_id = store.make_project_id("Альфа")
CLIENT.db["platform_projects"].append(project(twin_id, "Альфа", "twin-viewer", "twin-editor"))
before = dict(row(twin_id))
created = store.create_project(
    project_name="Альфа", viewer_code="intruder-viewer", editor_code="intruder-editor")
check("новый проект получил свой id", created != twin_id, created)
check("коды существующего проекта не тронуты",
      row(twin_id)["editor_code_hash"] == before["editor_code_hash"]
      and row(twin_id)["viewer_code_hash"] == before["viewer_code_hash"])
check("старый код по-прежнему открывает свой проект",
      store.resolve_project_access("twin-editor") == (twin_id, "editor"),
      str(store.resolve_project_access("twin-editor")))
check("код нового проекта не открывает старый",
      store.resolve_project_access("intruder-editor") == (created, "editor"),
      str(store.resolve_project_access("intruder-editor")))

print("5. Смена кодов у существующего проекта")
seed()
reason = rejected(lambda: store.update_project("alpha", viewer_code="beta-editor"))
check("нельзя взять код другого проекта", reason is not None, str(reason))
check("отказ не изменил коды", row("alpha")["viewer_code_hash"] == store.hash_code("alpha-viewer"))

reason = rejected(lambda: store.update_project("alpha", viewer_code="alpha-editor"))
check("нельзя сделать код пользователя равным своему коду аналитика", reason is not None, str(reason))

reason = rejected(lambda: store.update_project("alpha", viewer_code="alpha-viewer"))
check("повторный ввод своего же кода не считается занятым", reason is None, str(reason))

reason = rejected(lambda: store.update_project("alpha", viewer_code="alpha-new"))
check("новый уникальный код сохраняется", reason is None and row("alpha")["viewer_code_hash"] == store.hash_code("alpha-new"),
      str(reason))

reason = rejected(lambda: store.update_project("alpha", description="Новое описание"))
check("сохранение без смены кодов не проверяет коды и проходит", reason is None, str(reason))

print("6. Дубли, уже лежащие в базе, закрывают вход, а не открывают первый проект")
seed()
CLIENT.db["platform_projects"].append(project("gamma", "Гамма", "alpha-viewer", "gamma-editor"))
check("код, общий у двух проектов, не открывает ни один",
      store.resolve_project_access("alpha-viewer") == (None, "none"),
      str(store.resolve_project_access("alpha-viewer")))
check("коды без дублей работают как прежде",
      store.resolve_project_access("beta-viewer") == ("beta", "viewer"),
      str(store.resolve_project_access("beta-viewer")))

seed()
CLIENT.db["platform_projects"].append(project("gamma", "Гамма", "beta-editor", "gamma-editor"))
check("код аналитика одного проекта не пускает пользователем в другой",
      store.resolve_project_access("beta-editor") == (None, "none"),
      str(store.resolve_project_access("beta-editor")))

seed()
CLIENT.db["platform_projects"].append(project("gamma", "Гамма", "old-viewer", "gamma-editor"))
check("совпадение с архивным проектом вход не закрывает — архивный и так закрыт",
      store.resolve_project_access("old-viewer") == ("gamma", "viewer"),
      str(store.resolve_project_access("old-viewer")))

print("7. Форма создания проекта показывает причину словами")
# Проверяется не только хранилище, но и то, что человек в форме увидит
# понятную ошибку, а не трейсбек, и что проект не появится.
from streamlit.testing.v1 import AppTest  # noqa: E402
import streamlit as st  # noqa: E402

seed()
st.cache_data.clear()


def open_projects_page():
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
    at.session_state["platform_is_admin"] = True
    at.session_state["platform_nav_page"] = "Проекты"
    at.run()
    return at


def create_via_form(name, viewer, editor):
    at = open_projects_page()
    at.text_input(key="new_project_name").input(name)
    at.text_input(key="new_viewer_code").input(viewer)
    at.text_input(key="new_editor_code").input(editor)
    next(b for b in at.button if b.label == "Создать проект").click()
    at.run()
    return at


form = create_via_form("Гамма", "alpha-viewer", "gamma-editor")
errors = [e.value for e in form.error]
check("ошибка показана словами, а не исключением", not form.exception, str(form.exception))
check("в форме причина — код занят", any("занят другим проектом" in e for e in errors), str(errors))
check("проект из формы не создан", project_count() == 3, str(project_count()))

st.cache_data.clear()
form = create_via_form("Гамма", "gamma-viewer", "gamma-editor")
check("с уникальными кодами форма создаёт проект", not form.exception and project_count() == 4,
      f"{form.exception} / {project_count()}")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Коды доступа уникальны, дубли закрывают вход, чужой проект не перезаписать.")
