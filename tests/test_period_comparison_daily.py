# -*- coding: utf-8 -*-
"""Динамика по календарным дням вместо периодов целиком.

Аналитик попросил: «грузится период с 1 по 7, но на графиках, динамике -
все это разделено по дням». У period_comparison.py раньше был только один
уровень детализации - загруженный период (build_period_name даёт "24.04.2026
-30.04.2026" одной точкой на графике), хотя у каждого сообщения уже есть
своя дата в колонке "datetime". daily_metrics_for_comparison бьёт выбранные
сообщения на календарные дни и отдаёт список в ТОЙ ЖЕ форме, что и старая
period_metrics_for_comparison, - поэтому build_comparison_metrics,
comparison_visual_rows и весь UI дальше работают без изменений.

Заодно: short_period_chart_label теперь убирает год из дат ("24.04.2026" ->
"24.04") - на оси графика он не нёс пользы и раньше не убирался нигде.

Мутационные проверки (что ломает какой тест):
- в daily_metrics_for_comparison убрать dropna(subset=["_day"]) -> сообщения
  без даты превратились бы в мусорный "NaT"-день, тест на число дней и на
  агрегаты первого дня краснеет;
- в build_comparison_metrics поменять порядок веток (сначала period, потом
  day) -> тест "granularity=day использует дни, а не периоды" краснеет,
  потому что тогда для двух реальных периодов с датами внутри вернулись бы
  периоды, а не дни;
- убрать откат на period_metrics_for_comparison при < 2 дней -> тест на
  фолбэк (сообщения без парсящейся даты, но 2 периода) краснеет вместо
  того, чтобы вернуть период-сравнение;
- в short_period_chart_label убрать regex год -> тест "год убран из подписи"
  краснеет.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import pandas as pd  # noqa: E402

from services.period_comparison import (  # noqa: E402
    build_comparison_metrics,
    daily_metrics_for_comparison,
    short_period_chart_label,
)

failures = []


def check(label, condition, detail=""):
    mark = "  ✓ " if condition else "  ✗ "
    print(mark + label + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def _messages_over_days() -> pd.DataFrame:
    # Один "период" с сообщениями за три разных календарных дня - ровно тот
    # случай, который раньше схлопывался в одну точку на графике.
    rows = []
    days = ["2026-04-24", "2026-04-25", "2026-04-26"]
    counts = [5, 3, 7]
    for day, n in zip(days, counts):
        for i in range(n):
            rows.append(
                {
                    "message_id": f"{day}_m{i}",
                    "datetime": f"{day}T{10 + i % 8}:00:00",
                    "sentiment": "негатив" if i % 3 == 0 else "позитив",
                    "views": 1000,
                    "audience": 500,
                    "engagement": 20,
                }
            )
    return pd.DataFrame(rows)


print("1. daily_metrics_for_comparison: считает по календарным дням")
daily = daily_metrics_for_comparison(_messages_over_days())
check("три дня с сообщениями -> три точки", len(daily) == 3, str(len(daily)))
check(
    "точки идут в хронологическом порядке",
    [d["period_id"] for d in daily] == ["2026-04-24", "2026-04-25", "2026-04-26"],
    str([d["period_id"] for d in daily]),
)
check(
    "подпись дня - без года (24.04, а не 2026-04-24)",
    daily[0]["label"] == "24.04",
    str(daily[0]["label"]),
)
check(
    "первый день посчитал ровно свои 5 сообщений, не все 15",
    daily[0]["messages"] == 5,
    str(daily[0]),
)
check(
    "третий день - 7 сообщений",
    daily[2]["messages"] == 7,
    str(daily[2]),
)
check(
    "доли тональности посчитаны (positive_share есть и в (0..1])",
    0 < daily[0]["positive_share"] <= 1,
    str(daily[0].get("positive_share")),
)

print("2. daily_metrics_for_comparison: меньше двух дней -> пусто (нечего сравнивать)")
one_day = pd.DataFrame(
    [
        {"message_id": "a", "datetime": "2026-04-24T10:00:00", "sentiment": "позитив", "views": 1, "audience": 1, "engagement": 1},
        {"message_id": "b", "datetime": "2026-04-24T11:00:00", "sentiment": "позитив", "views": 1, "audience": 1, "engagement": 1},
    ]
)
check("один день на все сообщения -> пустой список", daily_metrics_for_comparison(one_day) == [])

no_datetime = pd.DataFrame([{"message_id": "a", "sentiment": "позитив"}])
check(
    "нет колонки datetime -> пустой список, не исключение",
    daily_metrics_for_comparison(no_datetime) == [],
)

unparseable = pd.DataFrame(
    [
        {"message_id": "a", "datetime": "не дата", "sentiment": "позитив"},
        {"message_id": "b", "datetime": None, "sentiment": "позитив"},
    ]
)
check(
    "дата не распознана ни у одной строки -> пустой список, не падение",
    daily_metrics_for_comparison(unparseable) == [],
)

print("3. build_comparison_metrics(granularity='day'): дни, а не периоды, когда дни есть")
periods = pd.DataFrame(
    [{"period_id": "p1", "period_name": "п1", "date_from": "2026-04-24", "date_to": "2026-04-26"}]
)
messages = _messages_over_days()
messages["period_id"] = "p1"
aggregate = build_comparison_metrics(messages, periods, ["p1"], granularity="day")
check("агрегат построился (не None)", aggregate is not None)
if aggregate is not None:
    seq = aggregate["comparison_sequence"]
    check(
        "в последовательности - три ДНЯ (24.04/25.04/26.04), а не один период",
        [item["label"] for item in seq] == ["24.04", "25.04", "26.04"],
        str([item["label"] for item in seq]),
    )
    check(
        "current/previous - соседние дни, не первый/последний период",
        aggregate["comparison"]["current"]["label"] == "26.04"
        and aggregate["comparison"]["previous"]["label"] == "25.04",
        str((aggregate["comparison"]["previous"]["label"], aggregate["comparison"]["current"]["label"])),
    )

print("4. build_comparison_metrics(granularity='day'): откат на периоды, если дней меньше двух")
periods2 = pd.DataFrame(
    [
        {"period_id": "p1", "period_name": "Период 1", "date_from": "2026-04-24", "date_to": "2026-04-24"},
        {"period_id": "p2", "period_name": "Период 2", "date_from": "2026-05-01", "date_to": "2026-05-01"},
    ]
)
messages2 = pd.DataFrame(
    [
        {"message_id": "a", "period_id": "p1", "datetime": None, "sentiment": "позитив", "views": 1, "audience": 1, "engagement": 1},
        {"message_id": "b", "period_id": "p2", "datetime": None, "sentiment": "позитив", "views": 1, "audience": 1, "engagement": 1},
    ]
)
aggregate2 = build_comparison_metrics(messages2, periods2, ["p1", "p2"], granularity="day")
check("без распознаваемых дат - агрегат всё равно строится (откат на периоды)", aggregate2 is not None)
if aggregate2 is not None:
    seq2 = aggregate2["comparison_sequence"]
    check(
        "откат вернул именно периоды (по period_id p1/p2), а не пустоту",
        {item["period_id"] for item in seq2} == {"p1", "p2"},
        str([item.get("period_id") for item in seq2]),
    )

print("5. build_comparison_metrics по умолчанию (без granularity) не трогает старое поведение")
aggregate3 = build_comparison_metrics(messages, periods, ["p1"])
check(
    "период всего один -> без явного granularity='day' сравнивать нечего (как раньше)",
    aggregate3 is None,
    str(aggregate3),
)

print("6. short_period_chart_label: год убирается из дат, произвольные названия не трогаются")
check(
    "диапазон дат теряет оба года",
    short_period_chart_label("24.04.2026–30.04.2026") == "24.04–30.04",
    short_period_chart_label("24.04.2026–30.04.2026"),
)
check(
    "одиночная дата теряет год",
    short_period_chart_label("24.04.2026") == "24.04",
    short_period_chart_label("24.04.2026"),
)
check(
    "произвольное название периода не меняется (не похоже на дату)",
    short_period_chart_label("Апрельская волна") == "Апрельская волна",
    short_period_chart_label("Апрельская волна"),
)
check(
    "пустая подпись -> запасное 'Период', не падение",
    short_period_chart_label("") == "Период",
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Дневная разбивка динамики работает корректно.")
