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
    available_buckets,
    build_comparison_metrics,
    daily_metrics_for_comparison,
    filter_messages_by_buckets,
    monthly_metrics_for_comparison,
    period_row_label,
    selected_period_label,
    short_period_chart_label,
    unresolved_date_count,
    weekly_metrics_for_comparison,
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

print("7. selected_period_label/period_row_label: без года, без дублирования даты")
# Реальный баг с живой платформы: имя файла выгрузки саммари содержало дату
# ДВАЖДЫ с годом ("summary_Песочница_24.04.2026_30.04.2026_24.04.2026_
# 30.04.2026.pdf") - period_label для отчёта шёл через отдельную функцию
# (selected_period_label), которую при чистке подписей периодов не трогали;
# она не дедуплицировала "имя · дата", даже когда имя периода и так дата.
auto_named_periods = pd.DataFrame(
    [
        {
            "period_id": "p1",
            "period_name": "24.04.2026–30.04.2026",
            "date_from": "2026-04-24",
            "date_to": "2026-04-30",
        }
    ]
)
check(
    "период с одним периодом: дата не дублируется и без года (как раньше в имени файла выгрузки)",
    selected_period_label(auto_named_periods, ["p1"]) == "24.04–30.04",
    selected_period_label(auto_named_periods, ["p1"]),
)
custom_named_periods = pd.DataFrame(
    [
        {
            "period_id": "p1",
            "period_name": "Апрельская волна",
            "date_from": "2026-04-24",
            "date_to": "2026-04-30",
        }
    ]
)
check(
    "осмысленное название периода не теряется, дата рядом без года",
    selected_period_label(custom_named_periods, ["p1"]) == "Апрельская волна · 24.04–30.04",
    selected_period_label(custom_named_periods, ["p1"]),
)
check(
    # Этот label уходит и в "Последний период: ..." на PNG-инфографике, и в
    # текст карточки для ИИ (_comparison_block) - тот же баг, та же причина.
    "period_row_label (подпись для карточки сравнения) тоже без года и без дублирования",
    period_row_label(auto_named_periods.iloc[0]) == "24.04–30.04",
    period_row_label(auto_named_periods.iloc[0]),
)

print("8. weekly_/monthly_metrics_for_comparison: та же форма, что и daily_, но по неделям/месяцам")
# Опорная точка: 2024-01-01 - понедельник (исторический факт, не требует
# вычислений). Неделя A: 01.01 (пн) .. 07.01 (вс); неделя B: 08.01 (пн) ..
# 14.01 (вс) - ровно следующая неделя.
weekly_rows = []
for day, n in [("2024-01-01", 2), ("2024-01-03", 1), ("2024-01-07", 3), ("2024-01-08", 4), ("2024-01-10", 1)]:
    for i in range(n):
        weekly_rows.append(
            {
                "message_id": f"{day}_m{i}",
                "datetime": f"{day}T09:00:00",
                "sentiment": "позитив",
                "views": 100,
                "audience": 50,
                "engagement": 5,
            }
        )
weekly_messages = pd.DataFrame(weekly_rows)
weekly = weekly_metrics_for_comparison(weekly_messages)
check("две недели -> две точки (01-07.01 и 08-14.01 объединены в бакеты, не 5 дней)", len(weekly) == 2, str(len(weekly)))
if len(weekly) == 2:
    check(
        "подпись недели - диапазон пн-вс без года (01.01–07.01)",
        weekly[0]["label"] == "01.01–07.01",
        weekly[0]["label"],
    )
    check(
        "первая неделя объединила все сообщения 01/03/07.01 (2+1+3=6), не только последний день",
        weekly[0]["messages"] == 6,
        str(weekly[0]),
    )
    check(
        "вторая неделя - 08.01 (4) + 10.01 (1) = 5",
        weekly[1]["messages"] == 5,
        str(weekly[1]),
    )
    check(
        "первая неделя охватывает оба края (01.01 и 07.01 есть в данных) - не помечена неполной",
        weekly[0]["partial"] is False,
        str(weekly[0]),
    )
    check(
        "вторая неделя обрывается на 10.01, до 14.01 (вс) данных нет - помечена неполной",
        weekly[1]["partial"] is True and "неполная неделя" in weekly[1]["label"],
        weekly[1]["label"],
    )
    check(
        "подпись неполной недели называет фактический охват (08.01–10.01), а не весь календарь",
        weekly[1]["label"] == "08.01–10.01 (неполная неделя)",
        weekly[1]["label"],
    )

# Три недели подряд, у СРЕДНЕЙ нет сообщений по понедельникам (её край) - это
# реальное затишье внутри полной выборки, а не обрезанный край выборки.
# Средняя неделя не должна помечаться неполной только по краям недели самим
# по себе: помечать нужно только первую/последнюю неделю ВСЕЙ выборки.
three_week_rows = []
for day, n in [
    ("2024-01-01", 2),  # неделя A: пн-вс полностью
    ("2024-01-07", 2),
    ("2024-01-09", 3),  # неделя B (08-14.01): вт, без пн и вс
    ("2024-01-12", 1),
    ("2024-01-15", 4),  # неделя C: пн-вс полностью
    ("2024-01-21", 1),
]:
    for i in range(n):
        three_week_rows.append(
            {
                "message_id": f"tw_{day}_m{i}",
                "datetime": f"{day}T09:00:00",
                "sentiment": "позитив",
                "views": 100,
                "audience": 50,
                "engagement": 5,
            }
        )
three_weeks = weekly_metrics_for_comparison(pd.DataFrame(three_week_rows))
check("три недели -> три точки", len(three_weeks) == 3, str(len(three_weeks)))
if len(three_weeks) == 3:
    check("первая (край) неделя не помечена неполной", three_weeks[0]["partial"] is False, str(three_weeks[0]))
    check(
        "средняя неделя не помечена неполной, хотя не начинается с понедельника",
        three_weeks[1]["partial"] is False and "неполная" not in three_weeks[1]["label"],
        three_weeks[1]["label"],
    )
    check("последняя (край) неделя не помечена неполной", three_weeks[2]["partial"] is False, str(three_weeks[2]))

# Край выборки может обрезаться и с ДРУГОЙ стороны: неделя заканчивается
# ровно в воскресенье, но начинается позже понедельника — проверяет, что
# охват сверяется по обеим границам, а не только по одной.
tail_week_rows = [
    {"message_id": "tail_a", "datetime": "2024-01-01T09:00:00", "sentiment": "позитив", "views": 100, "audience": 50, "engagement": 5},
    {"message_id": "tail_b", "datetime": "2024-01-07T09:00:00", "sentiment": "позитив", "views": 100, "audience": 50, "engagement": 5},
    {"message_id": "tail_c", "datetime": "2024-01-10T09:00:00", "sentiment": "позитив", "views": 100, "audience": 50, "engagement": 5},
    {"message_id": "tail_d", "datetime": "2024-01-14T09:00:00", "sentiment": "позитив", "views": 100, "audience": 50, "engagement": 5},
]
tail_weeks = weekly_metrics_for_comparison(pd.DataFrame(tail_week_rows))
if len(tail_weeks) == 2:
    check(
        "вторая неделя доходит до воскресенья (14.01), но не до понедельника (08.01) - тоже неполная",
        tail_weeks[1]["partial"] is True and "10.01–14.01" in tail_weeks[1]["label"],
        tail_weeks[1]["label"],
    )

# Полный месяц (данные достают до 1-го и до последнего дня) не должен
# считаться неполным — граница месяца не «неделя+6 дней», а его настоящий
# последний день (28-31, разный по месяцам).
full_month_rows = [
    {"message_id": "fm_a", "datetime": "2024-01-01T09:00:00", "sentiment": "позитив", "views": 100, "audience": 50, "engagement": 5},
    {"message_id": "fm_b", "datetime": "2024-01-31T09:00:00", "sentiment": "позитив", "views": 100, "audience": 50, "engagement": 5},
    {"message_id": "fm_c", "datetime": "2024-02-15T09:00:00", "sentiment": "позитив", "views": 100, "audience": 50, "engagement": 5},
]
full_month = monthly_metrics_for_comparison(pd.DataFrame(full_month_rows))
if len(full_month) == 2:
    check(
        "январь с данными на 1-е и 31-е число - полный месяц, не помечен неполным",
        full_month[0]["partial"] is False and full_month[0]["label"] == "Январь 2024",
        full_month[0]["label"],
    )

monthly_rows = []
for day, n in [("2024-01-05", 2), ("2024-01-20", 3), ("2024-02-10", 4)]:
    for i in range(n):
        monthly_rows.append(
            {
                "message_id": f"{day}_m{i}",
                "datetime": f"{day}T09:00:00",
                "sentiment": "позитив",
                "views": 100,
                "audience": 50,
                "engagement": 5,
            }
        )
monthly_messages = pd.DataFrame(monthly_rows)
monthly = monthly_metrics_for_comparison(monthly_messages)
check("два месяца -> две точки", len(monthly) == 2, str(len(monthly)))
if len(monthly) == 2:
    # Данные за январь — только 05.01 и 20.01, до 01.01 и после 20.01 в
    # выборке ничего нет: это край выборки (январь — первый месяц), а не
    # реальное затишье внутри полного месяца, поэтому подпись честно
    # называет фактический охват вместо всего календарного января.
    check(
        "подпись месяца - название по-русски, год и честный охват (неполный январь)",
        monthly[0]["label"] == "Январь 2024 (неполный месяц: 05.01–20.01)",
        monthly[0]["label"],
    )
    check("январь помечен неполным", monthly[0]["partial"] is True, str(monthly[0]))
    check(
        "февраль (последний месяц выборки, данные только 10.02) тоже помечен неполным",
        monthly[1]["partial"] is True and "неполный месяц" in monthly[1]["label"],
        monthly[1]["label"],
    )
    check(
        "январь объединил оба дня (05.01 и 20.01): 2+3=5",
        monthly[0]["messages"] == 5,
        str(monthly[0]),
    )
    check("февраль - 4 сообщения", monthly[1]["messages"] == 4, str(monthly[1]))

print("9. available_buckets: список для пикера, минимум 1 бакет (не 2, как для сравнения)")
one_week_messages = pd.DataFrame(
    [
        {"message_id": "a", "datetime": "2024-01-01T09:00:00", "sentiment": "позитив", "views": 1, "audience": 1, "engagement": 1},
        {"message_id": "b", "datetime": "2024-01-02T09:00:00", "sentiment": "позитив", "views": 1, "audience": 1, "engagement": 1},
    ]
)
check(
    "недельных точек для сравнения нет (обе даты в одной неделе -> daily/weekly_metrics_for_comparison пуст)",
    weekly_metrics_for_comparison(one_week_messages) == [],
)
check(
    "но available_buckets всё равно отдаёт этот один бакет - пикеру есть что показать",
    len(available_buckets(one_week_messages, "week")) == 1,
    str(available_buckets(one_week_messages, "week")),
)
check(
    "available_buckets по дням для тех же данных - 2 дня (01.01 и 02.01)",
    len(available_buckets(one_week_messages, "day")) == 2,
)
check(
    "неизвестная/'period' гранулярность -> пустой список (пикер не нужен)",
    available_buckets(one_week_messages, "period") == [] and available_buckets(one_week_messages, "bogus") == [],
)

print("10. filter_messages_by_buckets: сужает по собственной дате сообщения, не по period_id")
mixed_messages = pd.DataFrame(
    [
        {"message_id": "a", "period_id": "p1", "datetime": "2024-01-01T09:00:00"},
        {"message_id": "b", "period_id": "p1", "datetime": "2024-01-08T09:00:00"},
        {"message_id": "c", "period_id": "p1", "datetime": None},
    ]
)
only_first_day = filter_messages_by_buckets(mixed_messages, "day", ["2024-01-01"])
check(
    "выбран один день -> осталось только его сообщение",
    list(only_first_day["message_id"]) == ["a"],
    str(list(only_first_day["message_id"])),
)
check(
    "сообщение без даты не попало ни в один день (не потерялось молча в другую сторону - просто не выбрано)",
    "c" not in set(only_first_day["message_id"]),
)
check(
    "granularity='period' -> сообщения не сужаются (весь file/период как есть)",
    len(filter_messages_by_buckets(mixed_messages, "period", ["2024-01-01"])) == len(mixed_messages),
)
check(
    "пустой список выбранных бакетов -> сообщения не сужаются (не пустой дашборд по ошибке)",
    len(filter_messages_by_buckets(mixed_messages, "day", [])) == len(mixed_messages),
)
both_weeks = filter_messages_by_buckets(mixed_messages, "week", ["2024-01-01", "2024-01-08"])
# Обе недели — это все недели выборки, то есть не сужение. Раньше сообщение без
# даты выпадало и здесь, а значит, и при гранулярности по умолчанию («День»,
# отмечено всё) — со всего дашборда, включая индексы бренда.
check(
    "выбраны все недели -> выборка целиком, сообщение без даты в итогах",
    set(both_weeks["message_id"]) == {"a", "b", "c"},
    str(set(both_weeks["message_id"])),
)
all_days = filter_messages_by_buckets(mixed_messages, "day", ["2024-01-01", "2024-01-08"])
check(
    "выбраны все дни -> выборка целиком",
    len(all_days) == len(mixed_messages),
    str(list(all_days["message_id"])),
)
check(
    "выбрана часть -> сообщение без даты не входит",
    "c" not in set(filter_messages_by_buckets(mixed_messages, "week", ["2024-01-08"])["message_id"]),
)

print("11. unresolved_date_count: сколько сообщений не попадёт ни в один день/неделю/месяц")
check(
    "одно сообщение без даты из трёх",
    unresolved_date_count(mixed_messages) == 1,
    str(unresolved_date_count(mixed_messages)),
)
check("нет колонки datetime -> 0, не исключение", unresolved_date_count(pd.DataFrame({"x": [1]})) == 0)
check("пустой датафрейм -> 0", unresolved_date_count(pd.DataFrame()) == 0)

print("12. build_comparison_metrics(granularity='week'/'month') - новые значения гранулярности работают")
week_agg = build_comparison_metrics(weekly_messages, pd.DataFrame(), [], granularity="week")
check("granularity='week' даёт агрегат с недельными точками", week_agg is not None and len(week_agg["comparison_sequence"]) == 2)
month_agg = build_comparison_metrics(monthly_messages, pd.DataFrame(), [], granularity="month")
check("granularity='month' даёт агрегат с месячными точками", month_agg is not None and len(month_agg["comparison_sequence"]) == 2)

print("13. Изменение доли в процентных пунктах")
# Разделитель был последним местом, где число писалось с точкой: на одном
# экране карточки индексов бренда показывали «+1,14», а карточки тональности
# рядом — «+1.4». Точки в самой единице измерения при этом трогать нельзя.
from services.period_comparison import pp_delta  # noqa: E402

check("рост со знаком и запятой", pp_delta(0.48, 0.44) == "+4,0 п.п.", pp_delta(0.48, 0.44))
check("падение со знаком минус", pp_delta(0.40, 0.52) == "-12,0 п.п.", pp_delta(0.40, 0.52))
check("единица измерения не искажена", pp_delta(0.48, 0.44).endswith(" п.п."), pp_delta(0.48, 0.44))
check("в подписи нет «п,п,»", "п,п," not in pp_delta(0.48, 0.44), pp_delta(0.48, 0.44))
check("без изменения знак не рисуется", pp_delta(0.5, 0.5) == "0,0 п.п.", pp_delta(0.5, 0.5))

print("14. Неполная неделя/месяц — по границам выгрузок, а не по датам сообщений")
# Находки проверки d369aec: у малого бизнеса тихий последний день месяца —
# обычное дело, и весь загруженный март подписывался «неполный месяц:
# 01.03–30.03». А при двух несвязных загрузках (01–07.03 и 16–22.03) неделя
# 02–08.03 считалась полной, хотя 08.03 не входит ни в одну. Мерило — диапазоны
# date_from–date_to выбранных периодов.
# Мутационные проверки: не передавать coverage в build_comparison_metrics →
# падают «загруженный март — полный» и «неделя с разрывом»; проверять охват
# только у крайних недель → падает «неделя с разрывом»; откат на периоды без
# фильтра по сообщениям → падает «откат не выдумывает пустой период».
from services.period_comparison import period_coverage_days  # noqa: E402


def _rows(days, prefix):
    rows = []
    for day, n in days:
        for i in range(n):
            rows.append(
                {
                    "message_id": f"{prefix}_{day}_{i}",
                    "period_id": prefix,
                    "datetime": f"{day}T09:00:00",
                    "sentiment": "позитив",
                    "views": 100,
                    "audience": 50,
                    "engagement": 5,
                }
            )
    return rows


march_periods = pd.DataFrame(
    [
        {"period_id": "mar", "period_name": "Март", "date_from": "2026-03-01", "date_to": "2026-03-31"},
        {"period_id": "apr", "period_name": "Апрель", "date_from": "2026-04-01", "date_to": "2026-04-30"},
    ]
)
quiet_edges = pd.DataFrame(
    _rows([("2026-03-02", 3), ("2026-03-30", 2)], "mar") + _rows([("2026-04-10", 4)], "apr")
)
monthly_cov = build_comparison_metrics(quiet_edges, march_periods, ["mar", "apr"], granularity="month")
labels_cov = [p["label"] for p in (monthly_cov or {}).get("comparison_sequence", [])]
check(
    "загруженный целиком март с тихими 1-м и 31-м — полный месяц",
    labels_cov[:1] == ["Март 2026"],
    str(labels_cov),
)
check(
    "апрель загружен целиком — тоже полный, хотя данные только 10.04",
    labels_cov[1:2] == ["Апрель 2026"],
    str(labels_cov),
)
check(
    "охват выгрузок: март + апрель = 61 день",
    len(period_coverage_days(march_periods, ["mar", "apr"]) or set()) == 61,
    str(len(period_coverage_days(march_periods, ["mar", "apr"]) or set())),
)
check("без дат у периодов охват неизвестен", period_coverage_days(pd.DataFrame([{"period_id": "x"}]), ["x"]) is None)

split_periods = pd.DataFrame(
    [
        {"period_id": "w1", "period_name": "01–07.03", "date_from": "2026-03-01", "date_to": "2026-03-07"},
        {"period_id": "w3", "period_name": "16–22.03", "date_from": "2026-03-16", "date_to": "2026-03-22"},
    ]
)
split_msgs = pd.DataFrame(
    _rows([("2026-03-02", 2), ("2026-03-05", 2)], "w1") + _rows([("2026-03-16", 2), ("2026-03-20", 2)], "w3")
)
weekly_split = build_comparison_metrics(split_msgs, split_periods, ["w1", "w3"], granularity="week")
split_labels = [p["label"] for p in (weekly_split or {}).get("comparison_sequence", [])]
check(
    "неделя 02–08.03 с разрывом между загрузками подписана по фактическим дням",
    "02.03–07.03 (неполная неделя)" in split_labels,
    str(split_labels),
)
check(
    "неделя 16–22.03 загружена целиком — полная",
    "16.03–22.03" in split_labels,
    str(split_labels),
)
three_periods = pd.DataFrame(
    [
        {"period_id": "a", "period_name": "a", "date_from": "2026-03-02", "date_to": "2026-03-08"},
        {"period_id": "b", "period_name": "b", "date_from": "2026-03-09", "date_to": "2026-03-12"},
        {"period_id": "c", "period_name": "c", "date_from": "2026-03-16", "date_to": "2026-03-22"},
    ]
)
three_msgs = pd.DataFrame(
    _rows([("2026-03-03", 2)], "a") + _rows([("2026-03-10", 2)], "b") + _rows([("2026-03-18", 2)], "c")
)
middle = build_comparison_metrics(three_msgs, three_periods, ["a", "b", "c"], granularity="week")
middle_labels = [p["label"] for p in (middle or {}).get("comparison_sequence", [])]
check(
    "внутренняя неделя с разрывом между загрузками тоже подписана честно",
    middle_labels == ["02.03–08.03", "09.03–12.03 (неполная неделя)", "16.03–22.03"],
    str(middle_labels),
)

print("15. Откат на периоды целиком не выдумывает пустой период")
# Находка проверки c46fa02: при сужении гранулярностью до дней одного периода
# дневных точек меньше двух, и сравнение откатывалось на периоды — второй
# считался по нулю сообщений, и отчёт писал «было 0, стало 3».
two_periods = pd.DataFrame(
    [
        {"period_id": "p1", "period_name": "Неделя 1", "date_from": "2026-09-08", "date_to": "2026-09-14"},
        {"period_id": "p2", "period_name": "Неделя 2", "date_from": "2026-09-15", "date_to": "2026-09-21"},
    ]
)
only_p2_day = pd.DataFrame(_rows([("2026-09-17", 3)], "p2"))
check(
    "в выборке остался один период — сравнивать не с чем, а не «было 0»",
    build_comparison_metrics(only_p2_day, two_periods, ["p1", "p2"], granularity="day") is None,
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Дневная разбивка динамики работает корректно.")
