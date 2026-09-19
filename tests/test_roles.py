"""Роли и доступ: что видит зритель, редактор и владелец платформы.

Модель ролей до сих пор не была покрыта ничем: вся изоляция страниц владельца
держится на том, что пункт не попал в боковое меню, и любая правка меню
молча становилась дырой в правах.

Отдельно проверяется клиентский вид. Раньше он прятал ровно две настройки в
поповере «Вид» и одно слово в подписи, поэтому владелец справедливо не видел
разницы при переключении. Теперь это предпросмотр кабинета заказчика, и тест
следит, чтобы правка из разделов действительно исчезала.
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

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

PROJECT_ID = "demo_project"
PERIOD_ID = "p_2026_04"
now = datetime.now(timezone.utc).isoformat()

# Значения, которых нет на карточке проекта: их пишет поповер «⚙️ Вид».
# Именно они терялись при сохранении карточки.
SAVED_MERGE = 0.75
SAVED_BLOCKS = ["metrics", "comparison"]

CLIENT.db["platform_projects"] = [
    {
        "project_id": PROJECT_ID,
        "project_name": "Ромашка",
        "status": "active",
        "viewer_code_hash": store.hash_code("viewer123"),
        "editor_code_hash": store.hash_code("editor123"),
        "settings": {
            "dashboard_view_settings": {
                "default_view_mode": "analyst",
                "start_section": "Обзор",
                "client_hide_technical": True,
                "event_title_merge": SAVED_MERGE,
                "main_visible_blocks": SAVED_BLOCKS,
            }
        },
        "created_at": now,
        "updated_at": now,
    }
]
CLIENT.db["platform_periods"] = [
    {
        "project_id": PROJECT_ID,
        "period_id": PERIOD_ID,
        "period_name": "24.04.2026–30.04.2026",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
        "source_filename": "april.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    }
]

START = datetime(2026, 4, 24, 10, 0, 0)
THEMES = [
    ("позитив", "Запуск новой линейки"),
    ("негатив", "Жалобы на сроки доставки"),
    ("нейтрал", "Обзор рынка за квартал"),
]
rows = []
for i in range(24):
    sentiment, theme = THEMES[i % len(THEMES)]
    day = START + timedelta(days=i % 5, hours=i % 6)
    rows.append(
        {
            "project_id": PROJECT_ID,
            "period_id": PERIOD_ID,
            "table_name": "messages",
            "row_id": f"m{i}",
            "payload": {
                "message_id": f"m{i}",
                "period_id": PERIOD_ID,
                "date": day.strftime("%d.%m.%Y"),
                "datetime": day.isoformat(),
                "sentiment": sentiment,
                "views": 9000 + i * 100,
                "audience": 4000,
                "engagement": 70 + i,
                "likes": 70 + i,
                "comments": 0,
                "reposts": 0,
                "text_clean": f"Сообщение про {theme.lower()}",
                "message_link": f"https://example.com/{i}",
                "platform": "vk.com",
                "chat_title": f"Канал {i % 4}",
                "author": f"id{100 + i}",
                "tags": "Ромашка|Темы|Новость",
                "event_title": theme,
            },
        }
    )
for i, (sentiment, theme) in enumerate(THEMES):
    rows.append(
        {
            "project_id": PROJECT_ID,
            "period_id": PERIOD_ID,
            "table_name": "events",
            "row_id": f"e{i}",
            "payload": {
                "event_id": f"e{i}",
                "period_id": PERIOD_ID,
                "event_title": theme,
                "event_summary": f"Инфоповод: {theme}",
                "message_count": 8,
                "negative_count": 8 if sentiment == "негатив" else 0,
                "chat_count": 3,
                "importance_score": 10 - i,
                "start_date": "2026-04-24",
                "end_date": "2026-04-30",
                "main_tags": theme,
            },
        }
    )
CLIENT.db["platform_table_rows"] = rows

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(
        ("  ✓ " if condition else "  ✗ ")
        + label
        + (f" — {detail}" if detail and not condition else "")
    )
    if not condition:
        failures.append(label)


def open_as(role, *, section="Обзор", view_mode=None):
    """Запустить приложение под нужной ролью.

    Роль кладётся в session_state ровно так, как это делает вход по коду
    (project_admin_ui.render_project_access), а признак владельца — так, как
    его ставит вход по паролю платформы.
    """
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
    if role == "owner":
        at.session_state["platform_is_admin"] = True
    else:
        at.session_state["platform_project_id"] = PROJECT_ID
        at.session_state["platform_project_role"] = role
    at.session_state["platform_nav_page"] = section
    if view_mode:
        at.session_state["dashboard_view_mode"] = view_mode
    at.run()
    return at


def labels(at):
    """Все подписи на экране: кнопки, экспандеры, заголовки, радио."""
    out = []
    for item in list(at.button) + list(at.expander) + list(at.subheader) + list(at.header):
        value = getattr(item, "label", None) or getattr(item, "value", None)
        if value:
            out.append(str(value))
    for radio in at.radio:
        if radio.label:
            out.append(str(radio.label))
    return out


def sidebar_buttons(at):
    return [str(b.label) for b in at.sidebar.button if b.label]


print("1. Зритель: аналитика видна, работа с данными и платформа — нет")
viewer = open_as("viewer")
check("приложение открылось", not viewer.exception, str(viewer.exception))
viewer_side = sidebar_buttons(viewer)
for section in ["Обзор", "Индексы бренда", "Теги", "Инфоповоды", "Отзывы", "Сообщения", "Динамика", "Отчёт"]:
    check(f"зрителю доступен раздел «{section}»", section in viewer_side, str(viewer_side))
for closed in ["Загрузка файла", "История периодов", "Автозагрузка", "Проекты", "Сессии"]:
    check(f"зрителю НЕ показан «{closed}»", closed not in viewer_side, str(viewer_side))
check(
    "зрителю не дают переключать вид дашборда",
    not [r for r in viewer.sidebar.radio if str(r.label) == "Вид дашборда"],
    str([str(r.label) for r in viewer.sidebar.radio]),
)

print("2. Редактор: добавляется работа с данными, платформа остаётся закрытой")
editor = open_as("editor")
check("приложение открылось", not editor.exception, str(editor.exception))
editor_side = sidebar_buttons(editor)
for opened in ["Загрузка файла", "История периодов", "Автозагрузка"]:
    check(f"редактору доступен «{opened}»", opened in editor_side, str(editor_side))
for closed in ["Проекты", "Сессии"]:
    check(f"редактору НЕ показан «{closed}»", closed not in editor_side, str(editor_side))
check(
    "редактору доступен переключатель вида",
    bool([r for r in editor.sidebar.radio if str(r.label) == "Вид дашборда"]),
    str([str(r.label) for r in editor.sidebar.radio]),
)

print("3. Владелец платформы: открыт раздел «Платформа»")
owner = open_as("owner")
check("приложение открылось", not owner.exception, str(owner.exception))
owner_side = sidebar_buttons(owner)
for opened in ["Проекты", "Сессии"]:
    check(f"владельцу доступен «{opened}»", opened in owner_side, str(owner_side))

print("4. Правка инфоповодов: только начиная с редактора")
viewer_events = open_as("viewer", section="Инфоповоды")
editor_events = open_as("editor", section="Инфоповоды")
check(
    "зритель не может создать инфоповод вручную",
    "Создать инфоповод вручную" not in labels(viewer_events),
    str(labels(viewer_events)),
)
check(
    "редактор может создать инфоповод вручную",
    "Создать инфоповод вручную" in labels(editor_events),
    str(labels(editor_events)),
)

print("5. Правка саммари: только начиная с редактора")
viewer_report = open_as("viewer", section="Отчёт")
editor_report = open_as("editor", section="Отчёт")
check(
    "зритель не может править саммари",
    "Редактировать саммари" not in labels(viewer_report),
    str(labels(viewer_report)),
)
check(
    "редактор может править саммари",
    "Редактировать саммари" in labels(editor_report),
    str(labels(editor_report)),
)
check(
    "выгрузка саммари доступна и зрителю",
    "Выгрузить саммари" in labels(viewer_report),
    str(labels(viewer_report)),
)

print("6. Клиентский вид — предпросмотр кабинета заказчика, а не смена оформления")
owner_analyst = open_as("owner", section="Инфоповоды", view_mode="analyst")
owner_client = open_as("owner", section="Инфоповоды", view_mode="client")
check(
    "в аналитическом виде владелец правит инфоповоды",
    "Создать инфоповод вручную" in labels(owner_analyst),
    str(labels(owner_analyst)),
)
check(
    "в клиентском виде правка инфоповодов скрыта",
    "Создать инфоповод вручную" not in labels(owner_client),
    str(labels(owner_client)),
)
owner_report_analyst = open_as("owner", section="Отчёт", view_mode="analyst")
owner_report_client = open_as("owner", section="Отчёт", view_mode="client")
check(
    "в аналитическом виде владелец правит саммари",
    "Редактировать саммари" in labels(owner_report_analyst),
    str(labels(owner_report_analyst)),
)
check(
    "в клиентском виде правка саммари скрыта",
    "Редактировать саммари" not in labels(owner_report_client),
    str(labels(owner_report_client)),
)
# Панель ИИ висит не на роли, а на признаке владельца в сессии, поэтому
# понижения роли для неё мало — нужен отдельный флаг предпросмотра.
check(
    "в клиентском виде скрыта панель «Тексты от ИИ»",
    "Тексты от ИИ" not in labels(owner_report_client),
    str(labels(owner_report_client)),
)
check(
    "выгрузка саммари в клиентском виде остаётся: её видит и заказчик",
    "Выгрузить саммари" in labels(owner_report_client),
    str(labels(owner_report_client)),
)
check(
    "из предпросмотра можно выйти: переключатель вида на месте",
    bool([r for r in owner_report_client.sidebar.radio if str(r.label) == "Вид дашборда"]),
    str([str(r.label) for r in owner_report_client.sidebar.radio]),
)

print("7. Сохранение карточки проекта не затирает настройки из «⚙️ Вид»")
# Карточка проекта рисуется только после выбора строки в таблице, а выбор в
# AppTest не проставить, поэтому слияние проверяется на самой функции —
# карточка вызывает ровно её.
from services.project_settings import merged_dashboard_view_settings  # noqa: E402

card_updates = {
    "default_view_mode": "client",
    "start_section": "Отчёт",
    "comparison_visible_charts": [],
    "client_hide_technical": False,
}
stored = merged_dashboard_view_settings(
    CLIENT.db["platform_projects"][0]["settings"], card_updates
)
check(
    "порог склейки заголовков пережил сохранение карточки",
    float(stored.get("event_title_merge") or 0) == SAVED_MERGE,
    str(stored),
)
check(
    "набор блоков шапки пережил сохранение карточки",
    list(stored.get("main_visible_blocks") or []) == SAVED_BLOCKS,
    str(stored),
)
check(
    "настройки самой карточки при этом записались",
    stored.get("start_section") == "Отчёт" and stored.get("client_hide_technical") is False,
    str(stored),
)
check(
    "проект без сохранённых настроек не падает",
    merged_dashboard_view_settings({}, card_updates)["start_section"] == "Отчёт",
    "",
)
check(
    "настройки вообще отсутствуют — тоже не падает",
    merged_dashboard_view_settings(None, {})== {},
    "",
)

print("8. Демо-проект: видно как аналитику, менять нельзя ничего")
from services.project_settings import (  # noqa: E402
    DEMO_AI_LIMIT,
    demo_ai_runs_left,
    demo_ai_runs_used,
    is_demo_project,
)


import streamlit as st  # noqa: E402


def set_demo(enabled, runs_used=None):
    """Переключить демо-режим прямо в поддельной базе.

    Кеш сбрасывается целиком, а не через clear_platform_caches: тот работает
    бампом версии, а версия живёт в session_state, и у каждого нового AppTest
    она начинается заново — приложение увидело бы настройки первого запуска.
    """
    settings = CLIENT.db["platform_projects"][0]["settings"]
    settings["demo_mode"] = enabled
    if runs_used is not None:
        settings["demo_ai_runs"] = runs_used
    st.cache_data.clear()


check("обычный проект демо-режимом не считается", not is_demo_project({}))
check("мусор в флаге — не демо", not is_demo_project({"demo_mode": "да"}))
check("явное True — демо", is_demo_project({"demo_mode": True}))
# Счётчик не сбрасывается, поэтому его чтение обязано быть устойчивым:
# демо-проект живёт долго, и мусор в настройках не должен открывать лимит заново.
check("счётчик запусков: мусор считается нулём", demo_ai_runs_used({"demo_ai_runs": "три"}) == 0)
check("счётчик запусков: отрицательное считается нулём", demo_ai_runs_used({"demo_ai_runs": -5}) == 0)
check(
    "остаток лимита не уходит в минус",
    demo_ai_runs_left({"demo_ai_runs": DEMO_AI_LIMIT + 7}) == 0,
)
check(
    "у нетронутого демо доступен весь лимит",
    demo_ai_runs_left({}) == DEMO_AI_LIMIT,
)

set_demo(True)
demo = open_as("editor", section="Инфоповоды")
check("демо-проект открылся", not demo.exception, str(demo.exception))
demo_side = sidebar_buttons(demo)
for closed in ["Загрузка файла", "История периодов", "Автозагрузка"]:
    check(f"в демо закрыта «{closed}»", closed not in demo_side, str(demo_side))
demo_labels = labels(demo)
# Блоки остаются на виду: демо для того и нужно, чтобы показать возможности.
check(
    "блок ручного создания инфоповода в демо виден",
    "Создать инфоповод вручную" in demo_labels,
    str(demo_labels),
)
create_buttons = [b for b in demo.button if str(b.label) == "Создать инфоповод"]
check("кнопка создания найдена", bool(create_buttons), str([str(b.label) for b in demo.button]))
check(
    "кнопка создания в демо выключена",
    bool(create_buttons) and create_buttons[0].disabled,
    str([(str(b.label), b.disabled) for b in demo.button]),
)

demo_report = open_as("editor", section="Отчёт")
check("раздел «Отчёт» в демо открылся", not demo_report.exception, str(demo_report.exception))
demo_report_labels = labels(demo_report)
check(
    "правка саммари в демо видна, но заперта",
    "Редактировать саммари" in demo_report_labels,
    str(demo_report_labels),
)
save_summary = [b for b in demo_report.button if str(b.label) == "Сохранить саммари"]
check(
    "кнопка сохранения саммари в демо выключена",
    bool(save_summary) and save_summary[0].disabled,
    str([(str(b.label), b.disabled) for b in demo_report.button]),
)
check(
    "выгрузка отчёта в демо остаётся",
    "Выгрузить саммари" in demo_report_labels,
    str(demo_report_labels),
)

print("9. Доступ к ИИ в демо")
# Провайдер в тестах не настроен, поэтому панель выходит на сообщении «не
# настроена» и до кнопок генерации не доходит — проверять их выключенность
# здесь было бы самообманом. Проверяем то, что реально ново: демо открывает
# саму панель, не спрашивая настройку ai_access, которая по умолчанию
# разрешает генерацию только владельцу. Арифметика лимита проверена выше на
# demo_ai_runs_left/used.
set_demo(True)
demo_ai = open_as("editor", section="Отчёт")
check(
    "в демо панель «Тексты от ИИ» открыта редактору без настройки ai_access",
    "Тексты от ИИ" in labels(demo_ai),
    str(labels(demo_ai)),
)
set_demo(False)
plain_ai = open_as("editor", section="Отчёт")
check(
    "вне демо той же роли панель ИИ закрыта",
    "Тексты от ИИ" not in labels(plain_ai),
    str(labels(plain_ai)),
)

print("10. Владельца платформы демо-режим не ограничивает")
set_demo(True)
demo_owner = open_as("owner", section="Инфоповоды")
owner_create = [b for b in demo_owner.button if str(b.label) == "Создать инфоповод"]
check(
    "владелец в демо-проекте по-прежнему правит",
    bool(owner_create) and not owner_create[0].disabled,
    str([(str(b.label), b.disabled) for b in demo_owner.button]),
)
check(
    "владельцу в демо доступна загрузка файлов",
    "Загрузка файла" in sidebar_buttons(demo_owner),
    str(sidebar_buttons(demo_owner)),
)
set_demo(False)

print("11. Аналитик настраивает свой проект, но не чужие и не удаляет")
# Второй проект нужен, чтобы проверить главное: аналитик не должен видеть
# чужие проекты и их коды доступа.
OTHER_ID = "other_project"
if not any(p["project_id"] == OTHER_ID for p in CLIENT.db["platform_projects"]):
    CLIENT.db["platform_projects"].append(
        {
            "project_id": OTHER_ID,
            "project_name": "Чужой проект",
            "status": "active",
            "viewer_code_hash": store.hash_code("other-viewer"),
            "editor_code_hash": store.hash_code("other-editor"),
            "settings": {},
            "created_at": now,
            "updated_at": now,
        }
    )
st.cache_data.clear()

analyst_settings = open_as("editor", section="Настройки проекта")
check("«Настройки проекта» открылись аналитику", not analyst_settings.exception, str(analyst_settings.exception))
check(
    "пункт «Настройки проекта» есть в меню редактора",
    "Настройки проекта" in sidebar_buttons(analyst_settings),
    str(sidebar_buttons(analyst_settings)),
)
check(
    "зрителю «Настройки проекта» не показывают",
    "Настройки проекта" not in sidebar_buttons(open_as("viewer")),
    str(sidebar_buttons(open_as("viewer"))),
)
check(
    "аналитик может создать проект",
    "Создать проект" in labels(analyst_settings),
    str(labels(analyst_settings)),
)


def listed_projects(at):
    """Названия проектов из таблицы «Существующие проекты»."""
    names = []
    for frame in at.dataframe:
        data = frame.value
        if data is not None and "Проект" in getattr(data, "columns", []):
            names.extend(str(x) for x in data["Проект"].tolist())
    return names


analyst_list = listed_projects(analyst_settings)
check(
    "аналитик видит только свой проект",
    analyst_list == ["Ромашка"],
    str(analyst_list),
)
owner_list = listed_projects(open_as("owner", section="Проекты"))
check(
    "владелец платформы видит оба проекта",
    set(owner_list) == {"Ромашка", "Чужой проект"},
    str(owner_list),
)

print("12. Страница проектов проверяет роль сама, а не полагается на меню")
# Раньше единственной защитой было отсутствие пункта в меню: правка навигации
# сразу становилась дырой в правах на страницу с кодами доступа и удалением.
from project_admin_ui import render_project_manager  # noqa: E402
import inspect  # noqa: E402

signature = inspect.signature(render_project_manager)
check(
    "у страницы есть параметры роли",
    {"is_admin", "role", "current_project_id"} <= set(signature.parameters),
    str(list(signature.parameters)),
)
source = inspect.getsource(render_project_manager)
check(
    "роль проверяется внутри страницы",
    "role_rank(role)" in source,
    source[:200],
)
# Карточка проекта рисуется только после выбора строки в таблице, а выбор в
# AppTest не проставить — поведенчески опасную зону здесь не достать. Поэтому
# проверяется само ограждение: удаление и список проектов гейтятся is_admin.
# Проверка слабее поведенческой и заявлена именно такой, а не выдаётся за неё.
danger = source[source.index("Опасная зона") - 600 : source.index("Опасная зона")]
check(
    "удаление проекта огорожено проверкой владельца платформы",
    "if not is_admin:" in danger and "return" in danger,
    danger[-200:],
)

print("13. Подписи ролей отдельно от идентификаторов")
from services.roles import ROLE_TITLES, role_rank, role_title  # noqa: E402

# Идентификаторы лежат в сохранённых настройках, сессиях и записях присутствия.
# Переименование ради вывески сломало бы уже выданные доступы, поэтому здесь
# проверяется, что менялись именно подписи.
check("идентификатор зрителя не переименован", role_rank("viewer") == 1)
check("идентификатор редактора не переименован", role_rank("editor") == 2)
check("идентификатор владельца не переименован", role_rank("owner") == 3)
check("зритель показывается как «Пользователь»", role_title("viewer") == "Пользователь")
check("редактор показывается как «Аналитик»", role_title("editor") == "Аналитик")
check("владелец подписан полностью", role_title("owner") == "Владелец платформы")
check("незнакомая роль показывается как есть", role_title("хз") == "хз")
check("пустая роль не падает", role_title(None) == "")

from session_presence_ui import ROLE_LABELS  # noqa: E402

check(
    "таблица сессий берёт подписи оттуда же, а не свои",
    ROLE_LABELS is ROLE_TITLES,
    str(ROLE_LABELS),
)

analyst_side = open_as("editor")
sidebar_text = " ".join(
    str(m.value) for m in analyst_side.sidebar.markdown
) + " ".join(str(s.value) for s in analyst_side.sidebar.success)
check(
    "в сайдбаре роль подписана по-человечески, а не идентификатором",
    "editor" not in sidebar_text,
    sidebar_text[:200],
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Роли, клиентский вид, демо-режим, настройки проекта и подписи работают.")
