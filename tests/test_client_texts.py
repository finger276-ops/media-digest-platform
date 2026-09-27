# -*- coding: utf-8 -*-
"""Клиент не видит технических текстов платформы (задача 108).

Клиент — это и «Пользователь», и «Аналитик»: код аналитика выдают сотрудникам
заказчика. Поэтому внутреннее устройство платформы видит только владелец:
номер версии, коды форматов («mediologia_excel») и статусов («active»),
внутренние ID периодов и проектов, провайдер и модель ИИ, настройку n8n с
адресами базы, трассировки ошибок, код акцентного цвета.

Второй слой — рабочие заметки аналитика: «инфоповоды требуют проверки
аналитика», «похожих заголовков не найдено», «сводный слой для презентации
заказчику», «загрузите систему тегов». Их видит аналитик в аналитическом виде
и не видит заказчик, в том числе в клиентском предпросмотре.

Проверка идёт по отрисованной странице, а не по коду: проект на Медиалогии
проходит настоящую загрузку, затем каждый раздел открывается под каждой ролью,
и весь видимый текст — подписи, таблицы, варианты выпадающих списков —
сверяется со списком запрещённого. Чтобы тест не прошёл просто потому, что
страница пустая, у владельца те же сведения обязаны быть на месте.

Мутационные проверки (что ломает какой тест):
- вернуть подпись версии вне if is_admin -> «версия: не видна» краснеет у
  пользователя и аналитика;
- показывать форму входа владельца и при открытом проекте -> «форма входа
  владельца: не видна» краснеет;
- в render_period_history не переводить статус (убрать map(status_title)) ->
  «статус: не видна» краснеет у аналитика в «Истории периодов»;
- в render_period_history всегда добавлять period_id -> «ID периода: не
  видна» краснеет;
- в render_ingest_admin_page показывать render_n8n_hint_block всем -> «n8n и
  адреса базы: не видна» краснеет;
- _task_error_text возвращает текст как есть -> «трассировка: не видна»
  краснеет у аналитика в «Автозагрузке»;
- в render_saved_ai_text показывать модель всем (show_model=True) -> «модель
  ИИ: не видна» краснеет;
- в render_events снова звать render_assembly_notice без if can_edit ->
  «требуют проверки аналитика: не видна» краснеет у пользователя;
- в render_client_insights убрать if analyst_view -> «сводный слой для
  презентации: не видна» краснеет;
- в messages_ui убрать is_residual_title -> «Инфоповод: Без сюжета: не
  видна» краснеет;
- вернуть в подпись брендирования код цвета -> «код цвета: не видна»
  краснеет;
- ai_error_text возвращает str(exc) всем -> «ошибка модели без адреса и
  AI_TIMEOUT» краснеет;
- вернуть зрителю подпись «Вид дашборда: клиентский» -> «вид дашборда:
  клиентский: не видна» краснеет;
- в render_tag_statistics писать про Brand Analytics всегда -> «теги из
  Brand Analytics на Медиалогии: не видна» краснеет;
- в render_tier_analytics_block убрать if analyst_view -> «загрузите систему
  тегов: не видна» краснеет;
- в render_project_manager всегда добавлять project_id -> «ID проекта: не
  видна» краснеет;
- в summarize_import вернуть код формата -> «сводка загрузки: формат
  подписью» краснеет;
- убрать if can_edit у render_title_merge_report -> «похожих заголовков не
  найдено: не видна» краснеет.
"""

import io
import os
import re
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
os.environ["AI_PROVIDER"] = "off"

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from ai_summary_ui import AI_ERROR_GENERIC, ai_error_text  # noqa: E402
from app import APP_VERSION  # noqa: E402
from ingest_admin_ui import _task_error_text  # noqa: E402
from services.ai_provider import ERROR_RATE, AIError  # noqa: E402
from services.ai_summary import KIND_BRAND, KIND_RISKS, ai_text_storage_key  # noqa: E402
from services.import_report import summarize_import  # noqa: E402
from services.ingest import ingest_file_bytes  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


NOW = datetime.now(timezone.utc).isoformat()
PROJECT = "taxi_project"
WORK_DIR = tempfile.mkdtemp(prefix="client_texts_")

CLIENT.db["platform_projects"] = [
    {
        "project_id": PROJECT,
        "project_name": "Такси Плюс",
        "status": "active",
        "viewer_code_hash": store.hash_code("viewer-code"),
        "editor_code_hash": store.hash_code("editor-code"),
        "settings": {"topic_profile": "universal"},
        "created_at": NOW,
        "updated_at": NOW,
    }
]
CLIENT.db["platform_periods"] = []
CLIENT.db["platform_table_rows"] = []
CLIENT.db["platform_manual_edits"] = []
CLIENT.db["platform_sessions"] = []

TEXTS = [
    "Водители жалуются на новый тариф и комиссию",
    "Отличная поездка, вежливый водитель, спасибо",
    "Приложение не работает второй день, сбой",
    "Обычная поездка до аэропорта, ничего особенного",
]


def medialogia_export(start_day: int, rows: int = 24) -> bytes:
    base = datetime(2026, 4, start_day, 9, 0)
    data = []
    for i in range(rows):
        data.append(
            {
                "Время публикации": (base + pd.Timedelta(hours=i * 3)).strftime("%d.%m.%Y %H:%M"),
                "Кто пишет": f"Автор {i % 7}",
                "Где пишет": ["Такси чат", "vk.com/taxi", "Отзовик"][i % 3],
                "Текст": f"{TEXTS[i % len(TEXTS)]} #{start_day}-{i}",
                "Ссылка": f"https://example.com/m/{start_day}/{i}",
                "Тональность": ["Негативная", "Позитивная", "Нейтральная"][i % 3],
                "Аудитория блога": str(1000 + i * 10),
                "Вовлеченность": str(5 + i),
                "Теги": ["Тарифы", "Водители", "Приложение"][i % 3],
            }
        )
    buf = io.BytesIO()
    pd.DataFrame(data).to_excel(buf, index=False)
    return buf.getvalue()


# Два периода через настоящую загрузку: сообщения несут
# source_system=mediologia_excel, а ID периодов — вида 03_04_2026_…_хеш.
PERIOD_IDS = []
for day in (3, 13):
    result = ingest_file_bytes(
        medialogia_export(day),
        project_id=PROJECT,
        source_filename=f"medialogia_{day}.xlsx",
        source_system="auto",
        work_dir=WORK_DIR,
    )
    PERIOD_IDS.append(str(result["period_id"]))
hidden = ingest_file_bytes(
    medialogia_export(1, rows=6),
    project_id=PROJECT,
    source_filename="old.xlsx",
    source_system="mediologia_excel",
    work_dir=WORK_DIR,
)
store.update_period_metadata(PROJECT, hidden["period_id"], status="hidden")

for kind in (KIND_RISKS, KIND_BRAND):
    store.save_manual(
        PROJECT,
        "ai_texts",
        ai_text_storage_key(kind, PERIOD_IDS),
        {
            "text": "Риск: жалобы водителей на тариф.",
            "kind": kind,
            "period_ids": PERIOD_IDS,
            "provider": "yandex",
            "model": "yandexgpt/latest",
            "created_at": "2026-04-05T10:11:12+00:00",
        },
    )

# Автозагрузка: источник проекта и задача, упавшая с трассировкой.
CLIENT.db["platform_ingest_sources"] = [
    {
        "source_key": "taxi-weekly",
        "project_id": PROJECT,
        "title": "Еженедельная выгрузка",
        "source_system": "mediologia_excel",
        "params": {},
        "is_active": True,
        "created_at": NOW,
    }
]
CLIENT.db["platform_ingest_queue"] = [
    {
        "task_id": "ing_broken",
        "project_id": PROJECT,
        "source_key": "taxi-weekly",
        "storage_path": f"inbox/{PROJECT}/broken.xlsx",
        "original_filename": "broken.xlsx",
        "file_sha256": "abc",
        "status": "error",
        "attempts": 3,
        "max_attempts": 3,
        "period_name": "",
        "error_message": (
            "connection refused 10.0.0.5:5432\nTraceback (most recent call last):\n"
            '  File "/mount/src/platform/src/platform_store.py", line 1'
        ),
        "created_at": NOW,
        "finished_at": NOW,
    }
]

SECTIONS = ["Обзор", "Индексы бренда", "Теги", "Инфоповоды", "Отзывы", "Источники", "Сообщения", "Динамика", "Отчёт"]
DATA_PAGES = ["Загрузка файла", "История периодов", "Автозагрузка", "Настройки проекта"]

# Значения виджетов (выбранный вариант) — это код, а человек видит подпись
# из options; поэтому value у выпадающих списков не собираем.
TEXT_ATTRS = ("label", "body", "help", "placeholder", "caption")
VALUE_TYPES = {"Markdown", "Caption", "Info", "Warning", "Error", "Success", "Subheader", "Header", "Title", "Code", "TextArea", "Exception", "Expander"}


def collect(node, out):
    kind = type(node).__name__
    for attr in TEXT_ATTRS + (("value",) if kind in VALUE_TYPES else ()):
        try:
            value = getattr(node, attr, None)
        except Exception:  # noqa: BLE001 — у элемента нет такого поля
            value = None
        if value is None or callable(value):
            continue
        if isinstance(value, pd.DataFrame):
            out.append(" | ".join(map(str, value.columns)))
            out.extend(" | ".join(row) for row in value.astype(str).values.tolist())
            continue
        out.append(str(value))
    if kind == "Dataframe":
        try:
            frame = node.value
            out.append(" | ".join(map(str, frame.columns)))
            out.extend(" | ".join(row) for row in frame.astype(str).values.tolist())
        except Exception:  # noqa: BLE001
            pass
    try:
        options = getattr(node, "options", None)
        if options and not callable(options):
            out.append(" | ".join(map(str, options)))
    except Exception:  # noqa: BLE001
        pass
    children = getattr(node, "children", None)
    if isinstance(children, dict):
        for child in children.values():
            collect(child, out)


def open_page(page, *, role=None, view=None, owner=False):
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
    if owner:
        at.session_state["platform_is_admin"] = True
    elif role:
        at.session_state["platform_project_id"] = PROJECT
        at.session_state["platform_project_role"] = role
    if page:
        at.session_state["platform_nav_page"] = page
    if view:
        at.session_state["dashboard_view_mode"] = view
    at.session_state[f"period_select_{PROJECT}"] = list(PERIOD_IDS)
    at.run()
    texts = []
    collect(at.sidebar, texts)
    collect(at.main, texts)
    texts.extend(str(e.value) for e in at.exception)
    return at, "\n".join(texts)


# Техническое: видит только владелец платформы.
PERIOD_ID_RE = r"\d{2}_\d{2}_\d{4}_\d{2}_\d{2}_\d{4}_[0-9a-f]{6,}"
TECHNICAL = [
    ("версия", re.escape(APP_VERSION)),
    ("код формата", r"mediologia|brand_analytics"),
    ("статус", r"\b(active|hidden|archived)\b"),
    ("ID периода", PERIOD_ID_RE),
    ("ID проекта", re.escape(PROJECT)),
    ("модель ИИ", r"yandexgpt|\byandex\b"),
    ("код цвета", r"#[0-9a-fA-F]{6}\b"),
    ("трассировка", r"Traceback|\.py\"?, line \d|10\.0\.0\.5"),
    ("n8n и адреса базы", r"n8n|SUPABASE_URL|SERVICE_ROLE|platform_[a-z_]+"),
    ("хранилище Supabase", r"Supabase|Storage"),
]
# Рабочие заметки аналитика: заказчик их не видит, в том числе в предпросмотре.
ANALYST_NOTES = [
    ("требуют проверки аналитика", r"требуют проверки аналитика"),
    ("похожих заголовков не найдено", r"Похожих заголовков не найдено"),
    ("сводный слой для презентации", r"Сводный слой для презентации"),
    ("загрузите систему тегов", r"загрузите систему тегов"),
    ("форма входа владельца", r"Вход владельца платформы|Пароль владельца"),
    ("вид дашборда: клиентский", r"Вид дашборда: клиентский"),
]
RUBRICS = (
    r"Проблемы, жалобы|жалобы и негативный опыт|Цены, стоимость|Качество продукта или услуги|"
    r"Наличие, поставки|Монтаж, применение|Документы, сертификаты|Безопасность и пожарные|"
    r"Экология и энергоэффективность|Конкуренты и сравнение|Поддержка и клиентский сервис|"
    r"Общие обсуждения|Прочие обсуждения|Без тега"
)
EVERYONE = [
    ("Инфоповод: Без сюжета", r"Инфоповод:\*\* Без сюжета"),
    ("теги из Brand Analytics на Медиалогии", r"системных колонок Brand Analytics"),
    # Рубрики платформы писались под одного заказчика — у других это чужой
    # список тегов (tests/test_display_tags.py проверяет фильтр по отдельности).
    ("рубрики платформы в тегах", RUBRICS),
]


def assert_absent(where, text, patterns):
    for label, pattern in patterns:
        found = re.search(pattern, text)
        check(f"{where}: {label}: не видна", found is None, found.group(0) if found else "")


print("1. Пользователь: все разделы аналитики")
for section in SECTIONS:
    at, text = open_page(section, role="viewer")
    check(f"пользователь/{section}: открылся без исключений", not at.exception, str(at.exception)[:200])
    assert_absent(f"пользователь/{section}", text, TECHNICAL + ANALYST_NOTES + EVERYONE)

print("2. Аналитик в клиентском предпросмотре: то же, что у заказчика")
for section in SECTIONS:
    at, text = open_page(section, role="editor", view="client")
    check(f"предпросмотр/{section}: открылся без исключений", not at.exception, str(at.exception)[:200])
    notes = [n for n in ANALYST_NOTES if n[0] != "вид дашборда: клиентский"]
    assert_absent(f"предпросмотр/{section}", text, TECHNICAL + notes + EVERYONE)

print("3. Аналитик в рабочем виде: техники нет, рабочие заметки есть")
analyst_texts = {}
for section in SECTIONS:
    at, text = open_page(section, role="editor", view="analyst")
    analyst_texts[section] = text
    check(f"аналитик/{section}: открылся без исключений", not at.exception, str(at.exception)[:200])
    assert_absent(f"аналитик/{section}", text, TECHNICAL + EVERYONE)
check(
    "аналитик видит, что инфоповоды надо проверить",
    "требуют проверки аналитика" in analyst_texts["Инфоповоды"],
    analyst_texts["Инфоповоды"][:300],
)
check(
    "аналитик видит подсказку про систему тегов",
    "загрузите систему тегов" in analyst_texts["Теги"],
)

print("4. Аналитик на страницах данных: коды и ID — подписями, без устройства платформы")
page_texts = {}
for page in DATA_PAGES:
    at, text = open_page(page, role="editor")
    page_texts[page] = text
    check(f"аналитик/{page}: открылся без исключений", not at.exception, str(at.exception)[:200])
    assert_absent(f"аналитик/{page}", text, TECHNICAL)
check("статусы названы по-русски", "Активен" in page_texts["История периодов"] and "Скрыт" in page_texts["История периодов"])
check("форматы названы по-русски", "Медиалогия Excel" in page_texts["Загрузка файла"])
check(
    "аналитику вместо трассировки — понятный текст",
    "Технический сбой при обработке" in page_texts["Автозагрузка"],
    page_texts["Автозагрузка"][-400:],
)

print("5. Владелец видит всё, что спрятано от клиента")
_, owner_overview = open_page("Обзор", owner=True)
check("версия на месте", APP_VERSION in owner_overview)
check("модель ИИ на месте", "yandexgpt/latest" in owner_overview, owner_overview[:300])
_, owner_history = open_page("История периодов", owner=True)
check("ID периода на месте", re.search(PERIOD_ID_RE, owner_history) is not None)
_, owner_ingest = open_page("Автозагрузка", owner=True)
check("настройка n8n на месте", "Что настроить в n8n" in owner_ingest)
check("трассировка упавшей задачи на месте", "Traceback" in owner_ingest)
_, owner_settings = open_page("Настройки проекта", owner=True)
check("ID проекта на месте", PROJECT in owner_settings)

print("6. Форма входа владельца: только пока никто не вошёл")
_, anonymous = open_page(None)
check("без входа форма владельца видна", "Вход владельца платформы" in anonymous, anonymous[:300])

print("7. Тексты, которые собираются не на странице")
summary = summarize_import({"rows": 60, "source_columns": 11, "recognized": list(range(11)), "detected_system": "mediologia_excel"})
check("сводка загрузки: формат подписью", "Медиалогия Excel" in summary and "mediologia" not in summary, summary)
raw = AIError("Не удалось обратиться к модели: HTTPSConnectionPool(host='llm.api.cloud.yandex.net') Read timed out. Увеличьте AI_TIMEOUT.")
check("ошибка модели без адреса и AI_TIMEOUT", ai_error_text(raw, owner=False) == AI_ERROR_GENERIC, ai_error_text(raw, owner=False))
check("владелец видит исходный текст ошибки модели", "llm.api.cloud.yandex.net" in ai_error_text(raw, owner=True))
rate = AIError("YandexGPT: слишком много запросов подряд. Подождите минуту.", kind=ERROR_RATE)
check("понятная ошибка лимита видна всем как есть", ai_error_text(rate, owner=False) == str(rate))
check("ошибка без трассировки видна аналитику как есть", _task_error_text("Ключ источника не заведён.", is_admin=False) == "Ключ источника не заведён.")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Технические тексты видит только владелец платформы.")
