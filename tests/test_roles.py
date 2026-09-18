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

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Роли и клиентский вид работают.")
