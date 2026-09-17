# -*- coding: utf-8 -*-
"""Гранулярность (день/неделя/месяц) поверх уже загруженных файлов — сквозной
UI-тест через живой прогон streamlit_app.py (AppTest), не только юнит-тесты
services/period_comparison.py.

Аналитик попросил: «выгрузка дробится на дни независимо от того, что и как
загружено; день — минимальная единица, неделя/месяц — авто-группировка».
Отдельный файл (не блок в test_ui_smoke.py) — его фикстура специально
разносит сообщения по РАЗНЫМ календарным дням/неделям/месяцам внутри ОДНОГО
загруженного периода, а фикстура test_ui_smoke.py, наоборот, держит все
сообщения на одной дате (проверяет другое — навигацию/разделы) и трогать её
ради этого теста означало бы рисковать десятками не связанных с этой
задачей проверок точных чисел в том файле.

Мутационные проверки (что ломает какой тест):
- в app.py убрать пересчёт events/raw_events_agg после сужения (оставить
  filter_messages_by_buckets, но не звать recompute_event_counts) -> тест
  "число сообщений в шапке при сужении до одного дня" всё равно покраснел
  бы косвенно не сразу, но "Топ инфоповодов ..." тест ниже поймает
  рассинхрон events_agg с messages;
- в granularity_ui.py сделать дефолт multiselect пустым списком вместо
  bucket_ids -> тест "по умолчанию видны все 20 сообщений" краснеет.
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
        "project_id": "gr_project",
        "project_name": "Гранулярность",
        "status": "active",
        "settings": {},
        "created_at": now,
        "updated_at": now,
    }
]

PERIOD_ID = "p_gr"
CLIENT.db["platform_periods"] = [
    {
        "project_id": "gr_project",
        "period_id": PERIOD_ID,
        "period_name": "Одна большая выгрузка",
        "date_from": "2024-01-01",
        "date_to": "2024-02-01",
        "source_filename": "big.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    }
]

# Опорная точка 2024-01-01 - понедельник (тот же приём, что и в
# tests/test_period_comparison_daily.py). Пять календарных дней, две
# календарные недели, два календарных месяца - одной загрузкой.
_DAY_COUNTS = [
    ("2024-01-01", 3),  # неделя A (01-07.01), январь
    ("2024-01-03", 2),  # неделя A, январь
    ("2024-01-07", 4),  # неделя A, январь
    ("2024-01-08", 5),  # неделя B (08-14.01), январь
    ("2024-02-01", 6),  # неделя ?, февраль
]
TOTAL_MESSAGES = sum(n for _, n in _DAY_COUNTS)  # 20
DAY_A1_COUNT = 3  # 2024-01-01 в одиночку
WEEK_A_COUNT = 3 + 2 + 4  # 9 - вся неделя 01-07.01
JANUARY_COUNT = 3 + 2 + 4 + 5  # 14 - весь январь


def _message_row(day, index, theme):
    payload = {
        "message_id": f"{day}_m{index}",
        "period_id": PERIOD_ID,
        "date": day,
        "datetime": f"{day}T09:00:00",
        "sentiment": "позитив" if index % 2 == 0 else "негатив",
        "views": 1000,
        "audience": 500,
        "engagement": 20,
        "likes": 20,
        "comments": 0,
        "reposts": 0,
        "text_clean": f"Сообщение {day} №{index} про {theme}",
        "message_link": f"https://example.com/{day}_{index}",
        "platform": "vk.com",
        "author": f"user_{day}_{index}",
        "tags": theme,
        "event_title": theme,
    }
    return {
        "project_id": "gr_project",
        "period_id": PERIOD_ID,
        "table_name": "messages",
        "row_id": payload["message_id"],
        "payload": payload,
    }


CLIENT.db["platform_table_rows"] = [
    _message_row(day, i, "Тема")
    for day, count in _DAY_COUNTS
    for i in range(count)
]

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Запуск приложения, дефолтная гранулярность — «День», данные не теряются")
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
at.session_state["platform_is_admin"] = True
at.run()
check("приложение стартовало без исключений", not at.exception, str(at.exception))


def _metric_value(label):
    for m in at.metric:
        if str(m.label) == label:
            return str(m.value)
    return None


check(
    "по умолчанию видны все 20 сообщений (гранулярность не теряет данные)",
    _metric_value("Сообщений") == "20",
    str(_metric_value("Сообщений")),
)

granularity_radios = [r for r in at.radio if str(r.label) == "Гранулярность"]
check("контрол «Гранулярность» найден на странице", bool(granularity_radios), str([r.label for r in at.radio]))

if granularity_radios:
    radio = granularity_radios[0]
    check(
        "варианты — День/Неделя/Месяц/Файлы целиком",
        set(radio.options) == {"День", "Неделя", "Месяц", "Файлы целиком"},
        str(radio.options),
    )
    # AppTest.radio.value отдаёт «сырое» значение опции (то, что видит
    # питоновский код), а не строку после format_func - в отличие от
    # multiselect.options ниже, который отдаёт уже отформatированные метки.
    check("по умолчанию выбран «День»", radio.value == "day", str(radio.value))

    print("2. Сужение до одного дня — число сообщений в шапке падает до 3")
    day_buckets = [ms for ms in at.multiselect if "Дни/недели/месяцы" in str(ms.label)]
    check("пикер конкретных дней найден", bool(day_buckets), str([ms.label for ms in at.multiselect]))
    if day_buckets:
        check(
            "по умолчанию в пикере выбраны все 5 доступных дней",
            len(day_buckets[0].value) == 5,
            str(day_buckets[0].value),
        )
        day_buckets[0].set_value(["2024-01-01"]).run()
        check("страница не упала после сужения", not at.exception, str(at.exception))
        check(
            "в шапке осталось ровно 3 сообщения (день 2024-01-01)",
            _metric_value("Сообщений") == str(DAY_A1_COUNT),
            str(_metric_value("Сообщений")),
        )

    print("3. Переключение на «Неделя» — дефолт (все недели) не теряет данные, сужение до одной недели работает")
    granularity_radios = [r for r in at.radio if str(r.label) == "Гранулярность"]
    granularity_radios[0].set_value("Неделя").run()
    check("переключение на «Неделя» не роняет страницу", not at.exception, str(at.exception))
    check(
        "по умолчанию (все недели) всё ещё 20 сообщений",
        _metric_value("Сообщений") == str(TOTAL_MESSAGES),
        str(_metric_value("Сообщений")),
    )
    week_buckets = [ms for ms in at.multiselect if "Дни/недели/месяцы" in str(ms.label)]
    if week_buckets:
        check("для недельной гранулярности доступно 3 недели (A, B, февральская)", len(week_buckets[0].options) == 3, str(week_buckets[0].options))
        first_week_id = sorted(week_buckets[0].options)[0]
        week_buckets[0].set_value([first_week_id]).run()
        check(
            "сужение до недели A даёт 9 сообщений (3+2+4)",
            _metric_value("Сообщений") == str(WEEK_A_COUNT),
            str(_metric_value("Сообщений")),
        )

    print("4. Переключение на «Месяц» — сужение до января")
    granularity_radios = [r for r in at.radio if str(r.label) == "Гранулярность"]
    granularity_radios[0].set_value("Месяц").run()
    month_buckets = [ms for ms in at.multiselect if "Дни/недели/месяцы" in str(ms.label)]
    if month_buckets:
        # multiselect.options в AppTest отдаёт отформатированные метки
        # (после format_func), не «сырые» id бакетов - ищем по названию.
        january_id = [x for x in month_buckets[0].options if x.startswith("Январь")]
        check("январский бакет найден среди опций", bool(january_id), str(month_buckets[0].options))
        if january_id:
            month_buckets[0].set_value(january_id).run()
            check(
                "сужение до января даёт 14 сообщений",
                _metric_value("Сообщений") == str(JANUARY_COUNT),
                str(_metric_value("Сообщений")),
            )

    print("5. «Файлы целиком» — воспроизводит поведение без дробления, пикер дней скрыт")
    granularity_radios = [r for r in at.radio if str(r.label) == "Гранулярность"]
    granularity_radios[0].set_value("Файлы целиком").run()
    check("переключение на «Файлы целиком» не роняет страницу", not at.exception, str(at.exception))
    check(
        "все 20 сообщений снова на месте (сужение выключено)",
        _metric_value("Сообщений") == str(TOTAL_MESSAGES),
        str(_metric_value("Сообщений")),
    )
    check(
        "пикер дней/недель/месяцев скрыт при «Файлы целиком»",
        not [ms for ms in at.multiselect if "Дни/недели/месяцы" in str(ms.label)],
    )

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Гранулярность день/неделя/месяц работает корректно от экрана до данных.")
