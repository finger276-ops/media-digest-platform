# -*- coding: utf-8 -*-
"""Вход по email: код из письма, список доступа, запомненный вход.

До этого в платформе не было личностей: вход по общему коду проекта или
паролю владельца, всё в сессии вкладки. Теперь человек входит по адресу и
коду из письма (Supabase Auth), видит только проекты из своего списка
доступа, владельца узнают по PLATFORM_OWNER_EMAILS, а вход запоминается
в браузере на 14 дней.

Что здесь стережётся:
- код уходит только адресам из списка доступа и владельцам, а ответ на
  экране для чужого адреса тот же, что для своего (нельзя перебором узнать,
  чьи адреса есть в платформе);
- пять неверных кодов закрывают код, и дальше в Supabase не ходят вовсе;
- доступ сверяется на каждой перерисовке: снятый доступ и снятый владелец
  закрываются в уже открытой вкладке;
- в cookie и в базе нет токенов Supabase, в базе — только хеш ключа;
- «Выйти» отзывает ключ на сервере;
- без пароля владельца, но с адресами владельцев режим владельца больше не
  открыт всем; без адресов — прежнее поведение (песочница на нём держится);
- выдавать доступ может только владелец платформы.

Мутационные проверки (что ломает какой тест):
- request_login_code шлёт код любому адресу (убрать login_allowed) ->
  «на чужой адрес письмо не уходит» краснеет;
- счётчик отправки ведётся только для своих адресов -> «повтор для чужого
  адреса отвечает так же, как для своего» краснеет;
- verify_login_code не проверяет failed_attempts -> «после пяти ошибок
  верный код не принят» и «в Supabase больше не ходим» краснеют;
- _render_member_access берёт роль из сессии, а не из списка доступа ->
  «в другом проекте роль своя — аналитик» краснеет;
- is_platform_admin не снимает режим владельца, выданный по email ->
  «убранный из владельцев теряет режим» краснеет;
- login_sessions хранит ключ открытым текстом -> «в базе нет самого ключа»
  краснеет;
- logout не отзывает ключ -> «после выхода ключ не впускает» краснеет;
- убрать ветку «владельцы заданы адресами» в is_platform_admin -> «без
  пароля, но с адресами владельцев аноним не владелец» краснеет;
- render_project_members показывает форму аналитику -> «аналитику форма
  выдачи не показана» краснеет.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"
os.environ["EMAIL_LOGIN_ENABLED"] = "true"
os.environ["PLATFORM_OWNER_EMAILS"] = "Boss@Agency.ru; second@agency.ru"

from fake_supabase import AuthApiError, FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import auth_ui  # noqa: E402
import services.email_login as email_login  # noqa: E402
import services.login_sessions as login_sessions  # noqa: E402

email_login._auth_client = lambda: CLIENT
REPORTED = []
email_login.report_failure = lambda where, *a, **k: REPORTED.append((where, a, k))

NOW = datetime.now(timezone.utc)
ALPHA, BETA, HIDDEN = "p_alpha", "p_beta", "p_hidden"
ANNA, BORIS = "anna@client.ru", "boris@client.ru"
STRANGER = "stranger@nowhere.ru"


def project(project_id, name, status="active"):
    return {
        "project_id": project_id,
        "project_name": name,
        "status": status,
        "viewer_code_hash": store.hash_code(f"{project_id}-viewer"),
        "editor_code_hash": store.hash_code(f"{project_id}-editor"),
        "settings": {},
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


CLIENT.db["platform_projects"] = [
    project(ALPHA, "Альфа"),
    project(BETA, "Бета"),
    project(HIDDEN, "Скрытый", status="hidden"),
]
CLIENT.db["platform_periods"] = []

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def reset_login_state():
    """Счётчики и письма — с чистого листа; доступы не трогаются."""
    CLIENT.db["platform_auth_throttle"] = []
    CLIENT.auth.codes.clear()
    CLIENT.auth.sent.clear()
    CLIENT.auth.verify_calls.clear()
    CLIENT.auth.expired.clear()
    CLIENT.auth.fail_send = None
    st.cache_data.clear()


def age_throttle(minutes):
    """Сдвинуть последнюю отправку в прошлое — вместо ожидания минуты."""
    past = (NOW - timedelta(minutes=minutes)).isoformat()
    for row in CLIENT.db.get("platform_auth_throttle", []):
        row["last_sent_at"] = past


def members(project_id=None):
    rows = CLIENT.db.get("platform_project_members", [])
    return [r for r in rows if project_id is None or r["project_id"] == project_id]


print("1. Адреса и роли: нормализация и запреты")
check("адрес приводится к нижнему регистру", store.normalize_email("  Anna@Client.RU ") == ANNA)
check("не адрес — пустая строка", store.normalize_email("anna@") == "" and store.normalize_email("") == "")
check("роль владельца через список доступа не выдаётся", bool(store.member_problem(ANNA, "owner")))
check("пользователь и аналитик выдаются", store.member_problem(ANNA, "viewer") is None and store.member_problem(ANNA, "editor") is None)
check(
    "владельцы из секрета читаются через ; и в любом регистре",
    email_login.owner_emails() == {"boss@agency.ru", "second@agency.ru"},
    str(email_login.owner_emails()),
)

print("2. Список доступа: выдача, смена роли, закрытие")
store.save_project_member(ALPHA, "ANNA@client.ru", "viewer")
store.save_project_member(ALPHA, ANNA, "viewer")
check("повторная выдача не создаёт вторую строку", len(members(ALPHA)) == 1, str(members(ALPHA)))
check("адрес записан в нижнем регистре", members(ALPHA)[0]["user_email"] == ANNA)
store.save_project_member(BETA, ANNA, "editor")
store.save_project_member(HIDDEN, BORIS, "editor")
check(
    "у Анны два проекта с разными ролями",
    sorted((m["project_id"], m["role"]) for m in store.list_memberships(ANNA)) == [(ALPHA, "viewer"), (BETA, "editor")],
    str(store.list_memberships(ANNA)),
)
check("доступ только к скрытому проекту — не доступ", not store.email_has_access(BORIS))
store.save_project_member(ALPHA, "temp@client.ru", "viewer")
store.remove_project_member(ALPHA, "TEMP@client.ru")
check("закрытый доступ удалён", not [m for m in members(ALPHA) if m["user_email"] == "temp@client.ru"])
try:
    store.save_project_member(ALPHA, "boss@agency.ru", "owner")
    check("роль owner отклонена при записи", False)
except ValueError:
    check("роль owner отклонена при записи", True)

print("3. Отправка кода: только своим, ответ одинаковый")
reset_login_state()
step = email_login.request_login_code(" Anna@Client.ru ")
check("своему адресу код отправлен", step.ok and CLIENT.auth.sent == [ANNA], f"{step} / {CLIENT.auth.sent}")
check("пользователь Supabase заведён подтверждённым", CLIENT.auth.users.get(ANNA, {}).get("confirmed") is True)
sent_before = len(CLIENT.auth.sent)
stranger = email_login.request_login_code(STRANGER)
check("на чужой адрес письмо не уходит", len(CLIENT.auth.sent) == sent_before, str(CLIENT.auth.sent))
check("ответ для чужого адреса тот же, что для своего", stranger.ok and stranger.message == step.message)
throttle_keys = [r["email_hash"] for r in CLIENT.db["platform_auth_throttle"]]
check(
    "в счётчиках нет адресов открытым текстом",
    all("@" not in k for k in throttle_keys) and len(throttle_keys) == 2,
    str(throttle_keys),
)
again_own = email_login.request_login_code(ANNA)
again_stranger = email_login.request_login_code(STRANGER)
check("повтор через секунду не отправляется", not again_own.ok and "через" in again_own.message, again_own.message)
check(
    "повтор для чужого адреса отвечает так же, как для своего",
    (again_stranger.ok, again_stranger.message.split(" через")[0]) == (again_own.ok, again_own.message.split(" через")[0]),
    f"{again_stranger} / {again_own}",
)
age_throttle(2)
check("через минуту код отправляется снова", email_login.request_login_code(ANNA).ok)
check("существующий пользователь Supabase не мешает отправке", CLIENT.auth.sent.count(ANNA) == 2)
for _ in range(3):
    age_throttle(2)
    email_login.request_login_code(ANNA)
age_throttle(2)
capped = email_login.request_login_code(ANNA)
check("шестой код за час не отправляется", not capped.ok and "за последний час" in capped.message, capped.message)
check("владельцу код уходит без списка доступа", email_login.request_login_code("boss@agency.ru").ok and "boss@agency.ru" in CLIENT.auth.sent)
check("адресу только со скрытым проектом код не уходит", email_login.request_login_code(BORIS).ok and BORIS not in CLIENT.auth.sent)

reset_login_state()
REPORTED.clear()
CLIENT.auth.fail_send = AuthApiError("Email rate limit exceeded", 429, "over_email_send_rate_limit")
limited = email_login.request_login_code(ANNA)
check("лимит почты Supabase объяснён словами", not limited.ok and limited.message == email_login.MAIL_LIMIT_MESSAGE, limited.message)
CLIENT.auth.fail_send = RuntimeError(f"SMTP refused for {ANNA}")
failed = email_login.request_login_code(ANNA)
check("сбой отправки — отказ, а не молчание", not failed.ok and failed.message == email_login.SEND_FAILED_MESSAGE)
check(
    "в уведомление о сбое адрес не попадает",
    REPORTED and all(ANNA not in repr(r) for r in REPORTED),
    str(REPORTED),
)
CLIENT.auth.fail_send = None
check("после сбоя можно повторить сразу", email_login.request_login_code(ANNA).ok)

print("4. Проверка кода: верный, неверный, пять ошибок")
reset_login_state()
email_login.request_login_code(ANNA)
code = CLIENT.auth.codes[ANNA]
check("код не из цифр отклонён без Supabase", not email_login.verify_login_code(ANNA, "12ab56").ok and not CLIENT.auth.verify_calls)
ok = email_login.verify_login_code(ANNA.upper(), f"{code[:3]} {code[3:]}")
check("верный код с пробелом принят", ok.ok and ok.email == ANNA, str(ok))
check("код одноразовый", not email_login.verify_login_code(ANNA, code).ok)

reset_login_state()
email_login.request_login_code(ANNA)
code = CLIENT.auth.codes[ANNA]
results = [email_login.verify_login_code(ANNA, "999999") for _ in range(5)]
check("неверный код объяснён", results[0].message == email_login.WRONG_CODE_MESSAGE, results[0].message)
check("пятая ошибка закрывает код", results[-1].message == email_login.LOCKED_MESSAGE, results[-1].message)
calls = len(CLIENT.auth.verify_calls)
locked = email_login.verify_login_code(ANNA, code)
check("после пяти ошибок верный код не принят", not locked.ok and locked.message == email_login.LOCKED_MESSAGE)
check("и в Supabase больше не ходим", len(CLIENT.auth.verify_calls) == calls)
age_throttle(2)
email_login.request_login_code(ANNA)
check("новый код снимает блокировку", email_login.verify_login_code(ANNA, CLIENT.auth.codes[ANNA]).ok)

reset_login_state()
check("чужой адрес — тот же ответ, в Supabase не ходим",
      email_login.verify_login_code(STRANGER, "100001").message == email_login.WRONG_CODE_MESSAGE and not CLIENT.auth.verify_calls)


class _NetworkDown:
    class auth:
        @staticmethod
        def verify_otp(params):
            raise ConnectionError("network is down")


email_login.request_login_code(ANNA)
email_login._auth_client = lambda: _NetworkDown
REPORTED.clear()
down = email_login.verify_login_code(ANNA, "100001")
email_login._auth_client = lambda: CLIENT
check("сбой сети — не неверный код", down.message == email_login.SEND_FAILED_MESSAGE and REPORTED, str(down))
anna_row = email_login._throttle_row(ANNA)
check("сбой сети не списывает попытку", int(anna_row.get("failed_attempts") or 0) == 0, str(anna_row))

print("5. Запомненный вход: ключ, срок, отзыв")
token = login_sessions.create_session(ANNA)
rows = CLIENT.db["platform_auth_sessions"]
check("в базе нет самого ключа", all(token not in str(r) for r in rows) and len(rows) == 1, str(rows))
check("ключ возвращает адрес", login_sessions.resolve_session(token) == ANNA)
check("чужой ключ не впускает", login_sessions.resolve_session("guess") == "")
login_sessions.revoke_session(token)
check("отозванный ключ не впускает", login_sessions.resolve_session(token) == "")
expired = login_sessions.create_session(ANNA)
for row in CLIENT.db["platform_auth_sessions"]:
    if row["token_hash"] == login_sessions._hash(expired):
        row["expires_at"] = (NOW - timedelta(minutes=1)).isoformat()
check("просроченный ключ не впускает", login_sessions.resolve_session(expired) == "")
login_sessions.create_session(BORIS)
check(
    "просроченные записи убираются при следующем входе",
    all(r["token_hash"] != login_sessions._hash(expired) for r in CLIENT.db["platform_auth_sessions"]),
)


# --- приложение целиком ---------------------------------------------------------

def app(**state):
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    return at


def sidebar_labels(at):
    return [str(b.label) for b in at.sidebar.button if b.label]


def sidebar_texts(at):
    out = []
    for kind in ("success", "info", "warning", "error", "caption", "markdown"):
        out.extend(str(e.value) for e in getattr(at.sidebar, kind))
    return out


def cookie_scripts(at):
    return [str(e.proto.body) for e in at.get("html")]


def login_via_form(email, *, code=None):
    """Пройти форму: адрес → «Получить код» → код из письма → «Войти»."""
    at = app()
    at.text_input(key="login_email").input(email)
    at.button(key="login_send_code").click()
    at.run()
    if at.exception:
        return at
    if code is None:
        code = CLIENT.auth.codes.get(store.normalize_email(email), "000000")
    at.text_input(key="login_code").input(code)
    at.button(key="login_verify").click()
    at.run()
    return at


print("6. Форма входа")
reset_login_state()
fresh = app()
check("приложение открылось", not fresh.exception, str(fresh.exception))
check("форма входа по email показана", bool(fresh.text_input(key="login_email")) and "Получить код" in sidebar_labels(fresh))
check("вход кодом проекта остался", any(t.key == "project_access_code" for t in fresh.sidebar.text_input))
os.environ["EMAIL_LOGIN_ENABLED"] = "false"
off = app()
check("выключенный вход по email не показывается", not any(t.key == "login_email" for t in off.sidebar.text_input))
os.environ["EMAIL_LOGIN_ENABLED"] = "true"

print("7. Вход по коду из письма и выбор проекта")
reset_login_state()
anna = login_via_form(ANNA)
check("вход состоялся без ошибок", not anna.exception, str(anna.exception))
check("адрес в сессии", anna.session_state["platform_user_email"] == ANNA)
check("видно, кто вошёл", any(ANNA in t for t in sidebar_texts(anna)), str(sidebar_texts(anna)))
selector = anna.selectbox(key="platform_member_project")
check("выбор из двух проектов Анны", list(selector.options) == ["Альфа · Пользователь", "Бета · Аналитик"], str(selector.options))
check("роль первого проекта — из списка доступа", anna.session_state["platform_project_role"] == "viewer")
check("пользователю не показана загрузка файла", "Загрузка файла" not in sidebar_labels(anna))
check(
    "cookie запомненного входа записывается",
    any("mdp_login=" in s and "Max-Age=1209600" in s for s in cookie_scripts(anna)),
    str(cookie_scripts(anna)),
)
token = anna.session_state["platform_login_token"]
check("ключ запомненного входа действует", login_sessions.resolve_session(token) == ANNA)
anna.selectbox(key="platform_member_project").select(BETA).run()
check("в другом проекте роль своя — аналитик", anna.session_state["platform_project_role"] == "editor")
check("аналитику загрузка файла доступна", "Загрузка файла" in sidebar_labels(anna))
check("вошедшему по email вход по паролю не показан", not any(e.label == "Вход владельца платформы" for e in anna.sidebar.expander))

store.remove_project_member(BETA, ANNA)
st.cache_data.clear()
anna.run()
check("снятый доступ закрывает проект в открытой вкладке", anna.session_state["platform_project_id"] == ALPHA, str(anna.session_state["platform_project_id"]))
check("роль после снятия — пользователь", anna.session_state["platform_project_role"] == "viewer")
store.save_project_member(BETA, ANNA, "editor")
st.cache_data.clear()

print("8. Неверный код в форме")
reset_login_state()
wrong = login_via_form(ANNA, code="000000")
check("неверный код — ошибка словами", any(email_login.WRONG_CODE_MESSAGE == e.value for e in wrong.sidebar.error), str([e.value for e in wrong.sidebar.error]))
check("не вошёл", not wrong.session_state["platform_user_email"] if "platform_user_email" in wrong.session_state else True)

print("9. Владелец по адресу")
reset_login_state()
boss = login_via_form("boss@agency.ru")
check("владелец вошёл", not boss.exception and boss.session_state["platform_is_admin"] is True, str(boss.exception))
check("владельцу доступны «Проекты»", "Проекты" in sidebar_labels(boss), str(sidebar_labels(boss)))
os.environ["PLATFORM_OWNER_EMAILS"] = "second@agency.ru"
boss.run()
check("убранный из владельцев теряет режим владельца", boss.session_state["platform_is_admin"] is False and "Проекты" not in sidebar_labels(boss))
os.environ["PLATFORM_OWNER_EMAILS"] = "Boss@Agency.ru; second@agency.ru"

print("10. Выход")
reset_login_state()
anna = login_via_form(ANNA)
token = anna.session_state["platform_login_token"]
anna.button(key="login_logout").click().run()
check("после выхода адреса в сессии нет", "platform_user_email" not in anna.session_state)
check("после выхода проект закрыт", "platform_project_id" not in anna.session_state)
check("после выхода ключ не впускает", login_sessions.resolve_session(token) == "")
check("cookie стирается", any("Max-Age=0" in s for s in cookie_scripts(anna)), str(cookie_scripts(anna)))
check("после выхода снова форма входа", any(t.key == "login_email" for t in anna.sidebar.text_input))

print("11. Вход без письма по запомненному ключу")
reset_login_state()
token = login_sessions.create_session(ANNA)
original_reader = auth_ui.read_login_cookie
auth_ui.read_login_cookie = lambda: token
restored = app()
check("ключ из cookie впускает без кода", restored.session_state["platform_user_email"] == ANNA and not CLIENT.auth.sent)
check("и открывает проект из списка доступа", restored.session_state["platform_project_id"] in {ALPHA, BETA})
login_sessions.revoke_session(token)
revoked = app()
check("отозванный ключ не впускает", "platform_user_email" not in revoked.session_state)
check("а его cookie стирается", any("Max-Age=0" in s for s in cookie_scripts(revoked)), str(cookie_scripts(revoked)))
auth_ui.read_login_cookie = original_reader

print("12. Режим владельца без пароля")
del os.environ["PLATFORM_ADMIN_PASSWORD"]
anon = app()
check("без пароля, но с адресами владельцев аноним не владелец", "Проекты" not in sidebar_labels(anon), str(sidebar_labels(anon)))
os.environ["EMAIL_LOGIN_ENABLED"] = "false"
sandbox = app()
check("без входа по email — прежний режим для песочницы", "Проекты" in sidebar_labels(sandbox), str(sidebar_labels(sandbox)))
os.environ["EMAIL_LOGIN_ENABLED"] = "true"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

print("13. Аналитик по email заводит проект и сразу в нём")
reset_login_state()
anna = login_via_form(ANNA)
anna.selectbox(key="platform_member_project").select(BETA).run()
anna.session_state["platform_nav_page"] = "Настройки проекта"
anna.run()
anna.text_input(key="new_project_name").input("Гамма")
next(b for b in anna.button if b.label == "Создать проект").click()
anna.run()
created = [p for p in CLIENT.db["platform_projects"] if p["project_name"] == "Гамма"]
check("проект создан", len(created) == 1, str(anna.exception))
gamma = created[0]["project_id"] if created else ""
check(
    "создатель получил доступ аналитика",
    [(m["user_email"], m["role"]) for m in members(gamma)] == [(ANNA, "editor")],
    str(members(gamma)),
)
check("новый проект появился в списке", "Гамма · Аналитик" in list(anna.selectbox(key="platform_member_project").options))

print("14. Блок «Доступ по email»: владелец меняет, аналитик смотрит")


def members_block(project_id, can_edit):
    at = AppTest.from_file(str(REPO / "tests" / "members_app.py"), default_timeout=30)
    at.session_state["members_project_id"] = project_id
    at.session_state["members_can_edit"] = can_edit
    at.run()
    return at


st.cache_data.clear()
viewer_block = members_block(ALPHA, False)
check("аналитику список виден", not viewer_block.exception and any(ANNA in str(df.value) for df in viewer_block.dataframe))
check("аналитику форма выдачи не показана", not viewer_block.button, str([b.label for b in viewer_block.button]))
owner_block = members_block(ALPHA, True)
owner_block.text_input(key=f"member_email_{ALPHA}").input("New.Person@Client.ru")
owner_block.selectbox(key=f"member_role_{ALPHA}").select("editor")
owner_block.button(key=f"member_add_{ALPHA}").click().run()
check(
    "владелец выдал доступ, адрес нормализован",
    ("new.person@client.ru", "editor") in [(m["user_email"], m["role"]) for m in members(ALPHA)],
    str(members(ALPHA)),
)
check("подтверждение пережило перерисовку", any("Доступ выдан" in s.value for s in owner_block.success))
owner_block.text_input(key=f"member_email_{ALPHA}").input("не адрес")
owner_block.button(key=f"member_add_{ALPHA}").click().run()
check("не адрес — ошибка словами", any("адрес целиком" in e.value for e in owner_block.error))
owner_block.selectbox(key=f"member_target_{ALPHA}").select("new.person@client.ru")
owner_block.selectbox(key=f"member_action_{ALPHA}").select("__remove__")
owner_block.button(key=f"member_apply_{ALPHA}").click().run()
check("владелец закрыл доступ", "new.person@client.ru" not in [m["user_email"] for m in members(ALPHA)])

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Вход по email: код только своим, пять ошибок закрывают код, доступ сверяется на лету, выход отзывает ключ.")
