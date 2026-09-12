"""Дымовой тест интерфейса: боковая навигация, компактная шапка и разделы.

Supabase подменен поддельным клиентом, поэтому тест не ходит в сеть.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from datetime import datetime, timezone

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

now = datetime.now(timezone.utc).isoformat()
CLIENT.db["platform_projects"] = [
    {
        "project_id": "tn_project",
        "project_name": "ТЕХНОНИКОЛЬ",
        "status": "active",
        "settings": {},
        "created_at": now,
        "updated_at": now,
    }
]
PERIOD_ID = "p_2026_04"

CLIENT.db["platform_periods"] = [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "period_name": "24.04.2026–30.04.2026",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
        "source_filename": "week.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    }
]


def _message_row(index, sentiment, views, audience, engagement, theme):
    payload = {
        "message_id": f"m{index}",
        "period_id": PERIOD_ID,
        "date": "24.04.2026",
        "datetime": "2026-04-24T10:00:00",
        "sentiment": sentiment,
        "views": views,
        "audience": audience,
        "engagement": engagement,
        "likes": engagement,
        "comments": 0,
        "reposts": 0,
        "text_clean": f"Сообщение {index} про {theme}",
        "message_link": f"https://example.com/{index}",
        "platform": "vk.com",
        "author": f"user{index}",
        "tags": theme,
        "event_title": theme,
    }
    return {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "messages",
        "row_id": payload["message_id"],
        "payload": payload,
    }


_THEMES = [("позитив", "Запуск завода"), ("негатив", "Жалобы на монтаж"), ("нейтрал", "Отраслевая статистика")]
CLIENT.db["platform_table_rows"] = [
    _message_row(i, _THEMES[i % 3][0], 10_000 * (i + 1), 5_000, 120, _THEMES[i % 3][1])
    for i in range(12)
] + [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "events",
        "row_id": f"e{i}",
        "payload": {
            "event_id": f"e{i}",
            "period_id": PERIOD_ID,
            "event_title": theme,
            "event_summary": f"Инфоповод про {theme}",
            "message_count": 4,
            "negative_count": 4 if sentiment == "негатив" else 0,
            "chat_count": 2,
            "importance_score": 10 - i,
            "start_date": "2026-04-24",
            "end_date": "2026-04-30",
            "main_tags": theme,
        },
    }
    for i, (sentiment, theme) in enumerate(_THEMES)
]

# Две формулировки одного сюжета: Brand Analytics переименовывает сюжеты между
# периодами, и платформа должна собирать их в один инфоповод.
_SAME_STORY = [
    "ТЕХНОНИКОЛЬ запустила линию по производству кровельных материалов в Рязани",
    "Запуск линии кровельных материалов ТЕХНОНИКОЛЬ в Рязани",
]
CLIENT.db["platform_table_rows"] += [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "events",
        "row_id": f"e_story{i}",
        "payload": {
            "event_id": f"e_story{i}",
            "period_id": PERIOD_ID,
            "event_title": title,
            "event_summary": title,
            "message_count": 6 - i,
            "negative_count": 0,
            "chat_count": 2,
            "importance_score": 20 - i,
            "start_date": "2026-04-24",
            "end_date": "2026-04-30",
            "main_tags": "Производство",
        },
    }
    for i, title in enumerate(_SAME_STORY)
]

CLIENT.db["platform_ingest_queue"] = [
    {
        "task_id": "ing_demo_1",
        "project_id": "tn_project",
        "source_key": "ba-weekly",
        "storage_path": "inbox/ba-weekly/week1.xlsx",
        "original_filename": "Выгрузка недели.xlsx",
        "file_sha256": "abc",
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
        "period_name": "",
        "error_message": "",
        "created_at": now,
        "finished_at": None,
    },
    {
        "task_id": "ing_demo_2",
        "project_id": "tn_project",
        "source_key": "ba-weekly",
        "storage_path": "inbox/ba-weekly/broken.xlsx",
        "original_filename": "broken.xlsx",
        "file_sha256": "def",
        "status": "error",
        "attempts": 3,
        "max_attempts": 3,
        "period_name": "",
        "error_message": "Не удалось прочитать файл выгрузки.",
        "created_at": now,
        "finished_at": now,
    },
]
CLIENT.db["platform_ingest_sources"] = [
    {
        "source_key": "ba-weekly",
        "project_id": "tn_project",
        "title": "Еженедельный отчет BA",
        "source_system": "brand_analytics",
        "params": {"similarity_threshold": 0.3},
        "is_active": True,
    }
]

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Запуск приложения от имени владельца платформы")
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
at.session_state["platform_is_admin"] = True
at.run()
check("приложение стартовало без исключений", not at.exception, str(at.exception))

nav = {str(b.label): b for b in at.sidebar.button}
check("меню разделов в боковой панели", "Индексы бренда" in nav and "Инфоповоды" in nav, str(list(nav)))
check("работа с данными в том же меню", "Загрузка файла" in nav and "Автозагрузка" in nav, str(list(nav)))
check("раздел «Отчёт» появился", "Отчёт" in nav, str(list(nav)))
check("старого радио «Раздел» больше нет", not any("Раздел" == str(r.label) for r in at.sidebar.radio))


def open_section(name):
    """Нажать пункт меню и дождаться перерисовки."""
    button = {str(b.label): b for b in at.sidebar.button}[name]
    button.click().run()


print("2. Главный экран: шапка и метрики без лишних заголовков")
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader] + [h.value for h in at.header]
joined = " ".join(str(t) for t in texts)
check("имя проекта в шапке", "ТЕХНОНИКОЛЬ" in joined)
check("заголовок приложения ушёл со страницы", "Платформа дайджестов" not in joined, joined[:160])
check("подзаголовка «Период и основные метрики» больше нет", "Период и основные метрики" not in joined)
metric_labels = [str(m.label) for m in at.metric]
check("полоса метрик на месте", "Сообщений" in metric_labels, str(metric_labels[:6]))
view_controls = [str(c.label) for c in at.checkbox]
check(
    "настройки вида спрятаны в панель «Вид», а не в поток страницы",
    any("Метрики периода" in label for label in view_controls),
    str(view_controls),
)
check(
    "настройки графиков не висят на главной",
    not any("Показывать графики" in str(m.label) for m in at.multiselect),
    str([m.label for m in at.multiselect]),
)

print("3. Переход в «Индексы бренда» одним кликом")
open_section("Индексы бренда")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
check("заголовок раздела на месте", any("Индексы бренда" in str(t) for t in texts))
labels = {str(m.label): m.value for m in at.metric}
check("карточка BPI с расшифровкой названия", any("BPI · Индекс восприятия" in k for k in labels), str(list(labels))[:200])
check("карточка NSS с названием", any(k.startswith("NSS · ") for k in labels), str(list(labels))[:200])
check("саммари не примешивается к разделу", not any("Саммари периода" in str(t) for t in texts))

print("3.5. Раздел «Инфоповоды»: склейка похожих заголовков видна аналитику")
open_section("Инфоповоды")
check("раздел открылся без исключений", not at.exception, str(at.exception))
expanders = [str(e.label) for e in at.expander]
check(
    "блок со склейкой заголовков на месте",
    any("Склеено похожих заголовков" in label for label in expanders),
    str(expanders),
)
view_mode = [r for r in at.sidebar.radio if str(r.label) == "Вид дашборда"]
if view_mode:
    view_mode[0].set_value("analyst").run()
merge_control = [str(s.label) for s in at.selectbox]
check(
    "в аналитическом виде есть переключатель силы склейки",
    any("Склейка похожих заголовков" in label for label in merge_control),
    str(merge_control),
)
check("аналитический вид не уронил раздел", not at.exception, str(at.exception))

print("4. Раздел «Отчёт» держит саммари и выгрузки")
open_section("Отчёт")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
check("саммари переехало сюда", any("Саммари" in str(t) for t in texts), str(texts)[:200])

print("5. Раздел «Автозагрузка» по-прежнему работает")
open_section("Автозагрузка")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader] + [h.value for h in at.header]
check("очередь автозагрузки на месте", any("Очередь автозагрузки" in str(t) for t in texts))
metrics = {str(m.label): m.value for m in at.metric}
check("метрика «В очереди» = 1", any("В очереди" in k and v == "1" for k, v in metrics.items()), str(metrics))

print("6. Страница загрузки файла")
open_section("Загрузка файла")
check("страница загрузки без исключений", not at.exception, str(at.exception))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Дымовой тест интерфейса пройден.")
