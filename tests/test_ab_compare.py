# -*- coding: utf-8 -*-
"""Сравнение двух произвольных периодов (А/Б) в разделе «Динамика».

Мутационные проверки (что ломает какой тест):
- compare_metrics без metrics_comparable -> «охвата нет в А — изменение
  прочерком, не +100 %» краснеет;
- compare_metrics без проверки разметки -> «тональность не размечена в Б —
  прочерк» краснеет;
- compare_metrics считает от Б к А -> «изменение считается от А к Б»
  краснеет;
- compare_tags без сортировки по модулю изменения -> «первым — тег с
  наибольшим изменением» краснеет;
- compare_sources путает стороны -> «новые площадки в Б» краснеет;
- default_pair берёт первые два периода -> «по умолчанию — предпоследний
  против последнего» краснеет;
- render_ab_comparison без проверки одинаковых периодов -> «один и тот же
  период — просьба выбрать разные» краснеет;
- default_tag_pair без настроек брендов -> «по умолчанию — свой бренд против
  конкурента из настроек» краснеет;
- compare_tags без exclude -> «в «Других тегах» нет самих сравниваемых
  тегов» краснеет;
- доля площадки от всех сообщений, а не от сообщений тега -> «площадки: доли
  у каждого тега и разница» краснеет;
- колонки таблицы не переименованы в теги -> «таблица: колонки — теги,
  разница Б к А» краснеет.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

import pandas as pd  # noqa: E402

from services import metrics_compute as mc  # noqa: E402
from services.ab_compare import compare_metrics, compare_sources, compare_tags, comparison_notes  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def period(pid, n, *, views=10, sentiment="негативная", declared="audience|reach|engagement", tags=None, chats=None):
    tags = tags or ["Тарифы"] * n
    chats = chats or [f"https://vk.com/c{i % 3}" for i in range(n)]
    return mc.prepare_dashboard_messages(
        pd.DataFrame(
            {
                "period_id": [pid] * n,
                "views": [views] * n,
                "audience": [100] * n,
                "engagement": [1] * n,
                "sentiment": [sentiment if i % 2 == 0 else "нейтральная" for i in range(n)] if sentiment else [""] * n,
                "tags": tags,
                "chat_profile": chats,
                "chat_title": [c.rsplit("/", 1)[-1] for c in chats],
                mc.SOURCE_METRICS_COLUMN: [declared] * n,
                "text_clean": ["x"] * n,
            }
        )
    )


print("1. Метрики и тональность")
a = period("a", 4)
b = period("b", 6)
table = compare_metrics(a, b).set_index("Показатель")
check("изменение считается от А к Б", table.loc["Сообщений", "Изменение"] == "+2 (+50%)", str(table.to_dict("index")))
check("значения А и Б на месте", table.loc["Сообщений", "А"] == "4" and table.loc["Сообщений", "Б"] == "6")
no_reach_a = period("a", 4, views=0, declared="audience|engagement")
table = compare_metrics(no_reach_a, b).set_index("Показатель")
check("охвата нет в А — изменение прочерком, не +100 %", table.loc["Охват", "А"] == "—" and table.loc["Охват", "Изменение"] == "—",
      str(table.loc["Охват"].to_dict()))
check("причина прочерка названа", any("Охват: нет в выгрузке периода А" in note for note in comparison_notes(no_reach_a, b)))
unmarked_b = period("b", 6, sentiment="")
table = compare_metrics(a, unmarked_b).set_index("Показатель")
check("тональность не размечена в Б — прочерк", table.loc["Негатив, доля", "Б"] == "—" and table.loc["Негатив, доля", "Изменение"] == "—",
      str(table.loc["Негатив, доля"].to_dict()))
check("доли размеченного периода на месте", table.loc["Негатив, доля", "А"] == "50%")

print("2. Теги и площадки")
tags_a = period("a", 6, tags=["Тарифы"] * 5 + ["Водители"])
tags_b = period("b", 6, tags=["Тарифы"] * 1 + ["Водители"] * 2 + ["Приложение"] * 3)
tag_table = compare_tags(tags_a, tags_b)
check("первым — тег с наибольшим изменением", tag_table.iloc[0]["Тег"] == "Тарифы", str(tag_table.to_dict("records")))
check("новый тег в Б виден", "Приложение" in set(tag_table["Тег"]))
# Площадка — домен: другое сообщество ВКонтакте — не новая площадка.
src_a = period("a", 3, chats=["https://otzovik.com/reviews/taxi", "https://vk.com/club1", "https://vk.com/club1"])
src_b = period("b", 3, chats=["https://t.me/taxi", "https://vk.com/club2", "https://t.me/taxi"])
new, gone = compare_sources(src_a, src_b)
check("новые площадки в Б", list(new["Площадка"]) == ["telegram.org"], str(new.to_dict("records")))
check("пропавшие после А", list(gone["Площадка"]) == ["otzovik.com"], str(gone.to_dict("records")))

print("3. Выбор по умолчанию")
from ab_compare_ui import default_pair  # noqa: E402

periods = pd.DataFrame(
    [
        {"period_id": "p1", "period_name": "Март", "date_from": "2026-03-01", "date_to": "2026-03-07"},
        {"period_id": "p3", "period_name": "Май", "date_from": "2026-05-01", "date_to": "2026-05-07"},
        {"period_id": "p2", "period_name": "Апрель", "date_from": "2026-04-01", "date_to": "2026-04-07"},
    ]
)
check("по умолчанию — предпоследний против последнего", default_pair(periods) == ("p2", "p3"), str(default_pair(periods)))
check("один период — сравнивать нечего", default_pair(periods.head(1)) is None)

print("4. Блок в «Динамике»")
from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT
NOW = datetime.now(timezone.utc).isoformat()
CLIENT.db["platform_projects"] = [
    {"project_id": "ab", "project_name": "АБ", "status": "active", "settings": {}, "created_at": NOW, "updated_at": NOW}
]
CLIENT.db["platform_periods"] = [
    {"project_id": "ab", "period_id": pid, "period_name": name, "date_from": start, "date_to": end,
     "source_filename": "f.xlsx", "status": "active", "manifest": {}, "uploaded_at": f"{start}T12:00:00+00:00"}
    for pid, name, start, end in (("p1", "Март", "2026-03-01", "2026-03-07"), ("p2", "Апрель", "2026-04-01", "2026-04-07"),
                                  ("p3", "Май", "2026-05-01", "2026-05-07"))
]
rows = []
for pid, count, day in (("p1", 3, "2026-03-02"), ("p2", 5, "2026-04-02"), ("p3", 8, "2026-05-02")):
    for i in range(count):
        rows.append({"project_id": "ab", "period_id": pid, "table_name": "messages", "row_id": f"{pid}_{i}",
                     "payload": {"message_id": f"{pid}_{i}", "period_id": pid, "date": day, "datetime": f"{day}T1{i % 9}:00:00",
                                 "sentiment": "негативная", "views": 10, "audience": 100, "engagement": 1,
                                 "tags": "Тарифы", "text_clean": f"Сообщение {i}", "chat_title": f"Чат {i % 2}",
                                 "author": f"a{i}", mc.SOURCE_METRICS_COLUMN: "audience|reach|engagement"}})
CLIENT.db["platform_table_rows"] = rows

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

st.cache_data.clear()
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
at.session_state["platform_project_id"] = "ab"
at.session_state["platform_project_role"] = "viewer"
at.session_state["platform_nav_page"] = "Динамика"
at.session_state["period_select_ab"] = ["p3"]
at.run()
check("раздел открылся", not at.exception, str(at.exception))
boxes = {str(s.label): s for s in at.selectbox}
check("выбор периодов А и Б на месте", {"Период А", "Период Б"} <= set(boxes), str(list(boxes)))


def ab_table():
    for el in at.dataframe:
        if "Показатель" in el.value.columns and "Изменение" in el.value.columns:
            return el.value.set_index("Показатель")
    return None


table = ab_table()
check("по умолчанию Апрель против Мая", table is not None and table.loc["Сообщений", "Изменение"] == "+3 (+60%)",
      str(table.to_dict("index")) if table is not None else "таблицы нет")
if "Период А" in boxes:
    boxes["Период А"].set_value("p1").run()
    table = ab_table()
    check("выбран Март против Мая — пересчитано", table is not None and table.loc["Сообщений", "Изменение"] == "+5 (+167%)",
          str(table.to_dict("index")) if table is not None else "таблицы нет")
    {str(s.label): s for s in at.selectbox}["Период Б"].set_value("p1").run()
    check("один и тот же период — просьба выбрать разные", any("два разных периода" in str(i.value) for i in at.info),
          str([str(i.value) for i in at.info]))

check("один тег в выборке — режима тегов нет", not [r for r in at.radio if str(r.label) == "Что сравнивать"])

print("5. Два тега (бренда)")
from services.ab_compare import (  # noqa: E402
    compare_platform_shares,
    compare_type_shares,
    default_tag_pair,
)
from services.tag_compute import filter_messages_by_tags  # noqa: E402

BRANDS = pd.DataFrame({
    # «Кровля» — самый частый тег, но это тема, а не бренд.
    "tags": ["Технониколь|Кровля"] * 6 + ["Кнауф|Кровля"] * 4 + ["Кровля"] * 5 + ["Технониколь|Кнауф"],
    "platform": ["vk.com"] * 4 + ["t.me"] * 2 + ["vk.com", "t.me", "t.me", "t.me"] + ["vk.com"] * 5 + ["vk.com"],
    "message_type": ["Пост"] * 5 + ["Комментарий"] + ["Пост", "Пост", "Репост", "Репост"] + ["Пост"] * 6,
    "sentiment": ["негатив", "нейтрал"] * 8,
    "text_clean": ["x"] * 16, "views": [10] * 16, "audience": [100] * 16, "engagement": [1] * 16,
})
BRAND_MAP = {"own": ["ТЕХНОНИКОЛЬ"], "competitors": ["Кнауф"]}
check("по умолчанию — свой бренд против конкурента из настроек",
      default_tag_pair(BRANDS, BRAND_MAP) == ("Технониколь", "Кнауф"), str(default_tag_pair(BRANDS, BRAND_MAP)))
check("без настроек брендов — два самых частых тега", default_tag_pair(BRANDS) == ("Кровля", "Технониколь"),
      str(default_tag_pair(BRANDS)))
side_a = filter_messages_by_tags(BRANDS, ["Технониколь"])
side_b = filter_messages_by_tags(BRANDS, ["Кнауф"])
platforms = compare_platform_shares(side_a, side_b).set_index("Площадка")
check("площадки: доли у каждого тега и разница", platforms.loc["vk.com", "А"] == "5 · 71%"
      and platforms.loc["telegram.org", "Б"] == "3 · 60%" and platforms.loc["telegram.org", "Разница доли"] == "+31,4 п.п.",
      str(platforms.to_dict("index")))
types = compare_type_shares(side_a, side_b).set_index("Тип сообщения")
check("типы: доли у каждого тега и разница", types.loc["Репост", "Б"] == "2 · 40%"
      and types.loc["Пост", "Разница доли"] == "-25,7 п.п.", str(types.to_dict("index")))
check("нет типа у одной стороны — прочерк", compare_type_shares(side_a, side_b.drop(columns=["message_type"]))
      .set_index("Тип сообщения").loc["Пост", "Разница доли"] == "—")
neighbours = compare_tags(side_a, side_b, exclude=["Технониколь", "Кнауф"])
check("в «Других тегах» нет самих сравниваемых тегов", set(neighbours["Тег"]) == {"Кровля"}, str(neighbours.to_dict("records")))


def brands_app():
    import pandas as pd
    import streamlit as st

    from ab_compare_ui import render_ab_comparison

    periods = pd.DataFrame([
        {"period_id": "p1", "period_name": "Март", "date_from": "2026-03-01", "date_to": "2026-03-07"},
        {"period_id": "p2", "period_name": "Апрель", "date_from": "2026-04-01", "date_to": "2026-04-07"},
    ])
    render_ab_comparison("proj", periods, lambda pid: pd.DataFrame(), current_messages=st.session_state["brands"],
                         brand_map={"own": ["ТЕХНОНИКОЛЬ"], "competitors": ["Кнауф"]})


at = AppTest.from_function(brands_app, default_timeout=60)
at.session_state["brands"] = BRANDS
at.run()
mode = next((r for r in at.radio if str(r.label) == "Что сравнивать"), None)
check("выбор: два периода или два тега", mode is not None and "Два тега (бренда)" in list(mode.options),
      str([str(r.label) for r in at.radio]))
mode.set_value("Два тега (бренда)").run()
check("сравнение тегов открылось", not at.exception, str(at.exception))
boxes = {str(s.label): s for s in at.selectbox}
check("по умолчанию Технониколь против Кнауф", boxes.get("Тег А") is not None and boxes["Тег А"].value == "Технониколь"
      and boxes["Тег Б"].value == "Кнауф", str({k: v.value for k, v in boxes.items()}))
table = next((el.value for el in at.dataframe if "Показатель" in el.value.columns), None)
check("таблица: колонки — теги, разница Б к А", table is not None
      and list(table.columns) == ["Показатель", "Технониколь", "Кнауф", "Разница Б к А"]
      and table.set_index("Показатель").loc["Сообщений", "Технониколь"] == "7", str(table.to_dict("records") if table is not None else None))
tab_frames = [el.value for el in at.dataframe]
check("площадки и типы на вкладках", any("Площадка" in t.columns and "Технониколь" in t.columns for t in tab_frames)
      and any("Тип сообщения" in t.columns for t in tab_frames), str([list(t.columns) for t in tab_frames]))
boxes["Тег Б"].set_value("Технониколь").run()
check("один и тот же тег — просьба выбрать разные", any("два разных тега" in str(i.value) for i in at.info))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Сравнение двух периодов и двух тегов работает.")
