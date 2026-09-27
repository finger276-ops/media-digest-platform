# -*- coding: utf-8 -*-
"""Раздел «Источники»: площадки, авторы, новые площадки.

Мутационные проверки (что ломает какой тест):
- source_keys по названию, а не по адресу -> «два сообщества с одним
  названием — две площадки» и «переименованное сообщество — одна площадка»
  краснеют;
- аудитория площадки суммой по сообщениям -> «аудитория площадки — её
  подписчики, а не сумма» краснеет;
- display_table без проверки метрики -> «нет охвата в выгрузке — прочерк»
  краснеет;
- display_table без проверки разметки -> «нет разметки — негатив прочерком»
  краснеет;
- new_sources без вычитания прошлых площадок -> «новые площадки — только те,
  которых не было» краснеет;
- comparison_basis берёт первый выбранный период, а не последний ->
  «несколько периодов: последний с предыдущим» краснеет;
- comparison_basis не загружает прошлый период -> «один период: сравнение с
  предыдущим загруженным» краснеет.
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
from services.source_stats import (  # noqa: E402
    build_author_statistics,
    build_source_statistics,
    display_table,
    messages_of_source,
    new_sources,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def msg(profile, title, author, *, period="p2", audience=1000, views=10, engagement=1, sentiment="нейтральная",
        platform="vk.com", platform_type="Соцсети", declared="audience|reach|engagement"):
    return {
        "chat_profile": profile, "chat_title": title, "platform": platform, "platform_type": platform_type,
        "author": author, "author_profile": f"https://vk.com/{author}", "audience": audience, "views": views,
        "engagement": engagement, "sentiment": sentiment, "period_id": period,
        mc.SOURCE_METRICS_COLUMN: declared, "text_clean": f"Сообщение от {author}",
    }


rows = [
    # Одно сообщество, переименованное между постами: площадка одна.
    msg("https://vk.com/club1", "Такси города", "ivan", sentiment="негативная"),
    msg("https://vk.com/club1", "Такси города (архив)", "petr"),
    msg("https://vk.com/club1", "Такси города", "ivan", audience=1200),
    # Другое сообщество с тем же названием: другая площадка.
    msg("https://vk.com/club2", "Такси города", "anna"),
    # Отзовик без адреса блога: узнаётся по названию.
    msg("", "Отзовик", "olga", platform="otzovik.com", platform_type="Отзывы", audience=0, views=0),
]
messages = mc.prepare_dashboard_messages(pd.DataFrame(rows))

print("1. Площадки узнаются по адресу, а не по названию")
sources = build_source_statistics(messages)
by_key = {row["_key"]: row for _, row in sources.iterrows()}
check("два сообщества с одним названием — две площадки", {"https://vk.com/club1", "https://vk.com/club2"} <= set(by_key), str(list(by_key)))
check("переименованное сообщество — одна площадка", by_key.get("https://vk.com/club1", {}).get("messages") == 3, str(sources.to_dict("records")))
check("подпись — самое частое название", by_key["https://vk.com/club1"]["label"] == "Такси города")
check("аудитория площадки — её подписчики, а не сумма", by_key["https://vk.com/club1"]["audience"] == 1200, str(by_key["https://vk.com/club1"]["audience"]))
check("охват — сумма по сообщениям", by_key["https://vk.com/club1"]["reach"] == 30)
check("авторов у площадки — уникальные", by_key["https://vk.com/club1"]["authors"] == 2)
check("доля негатива посчитана", abs(by_key["https://vk.com/club1"]["negative_share"] - 1 / 3) < 1e-9)
check("площадка без адреса узнаётся по названию", by_key.get("отзовик", {}).get("type") == "Отзывы", str(list(by_key)))
check("сортировка по числу сообщений", sources.iloc[0]["_key"] == "https://vk.com/club1")
check("сообщения площадки выбираются по ключу", len(messages_of_source(messages, "https://vk.com/club1")) == 3)

print("2. Авторы")
authors = build_author_statistics(messages)
ivan = authors[authors["label"] == "ivan"]
check("автор с двумя сообщениями", not ivan.empty and int(ivan.iloc[0]["messages"]) == 2, str(authors.to_dict("records")))
check("где пишет автор", not ivan.empty and ivan.iloc[0]["places"] == "Такси города")

print("3. Прочерки вместо ложных нулей")
table = display_table(sources, messages, kind="sources")
check("охват есть — числа", table["Охват"].iloc[0] == "30", str(table.to_dict("records")))
no_reach = mc.prepare_dashboard_messages(pd.DataFrame([dict(r, views=0, **{mc.SOURCE_METRICS_COLUMN: "audience|engagement"}) for r in rows]))
table_no_reach = display_table(build_source_statistics(no_reach), no_reach, kind="sources")
check("нет охвата в выгрузке — прочерк", set(table_no_reach["Охват"]) == {"—"}, str(table_no_reach["Охват"].tolist()))
unmarked = mc.prepare_dashboard_messages(pd.DataFrame([dict(r, sentiment="") for r in rows]))
table_unmarked = display_table(build_source_statistics(unmarked), unmarked, kind="sources")
check("нет разметки — негатив прочерком", set(table_unmarked["Доля негатива"]) == {"—"}, str(table_unmarked["Доля негатива"].tolist()))
authors_table = display_table(authors, messages, kind="authors")
check("у авторов нет колонки аудитории площадки", "Аудитория" not in authors_table.columns and "Где пишет" in authors_table.columns)

print("4. Новые площадки")
previous = mc.prepare_dashboard_messages(pd.DataFrame([msg("https://vk.com/club1", "Такси города", "ivan", period="p1")]))
fresh = new_sources(messages, previous)
check("новые площадки — только те, которых не было", set(fresh["_key"]) == {"https://vk.com/club2", "отзовик"}, str(fresh["_key"].tolist()))
check("без прошлого периода — пусто, а не всё", new_sources(messages, pd.DataFrame()).empty)

print("5. С чем сравнивать")
from sources_ui import comparison_basis  # noqa: E402

periods = pd.DataFrame(
    [
        {"period_id": "p0", "period_name": "Март", "date_from": "2026-03-01", "date_to": "2026-03-07"},
        {"period_id": "p1", "period_name": "Апрель", "date_from": "2026-04-01", "date_to": "2026-04-07"},
        {"period_id": "p2", "period_name": "Май", "date_from": "2026-05-01", "date_to": "2026-05-07"},
    ]
)
both = pd.concat([previous, messages], ignore_index=True)
current, prev, cur_label, prev_label = comparison_basis(both, periods, ["p2", "p1"], None)
check("несколько периодов: последний с предыдущим", cur_label.startswith("Май") and prev_label.startswith("Апрель")
      and set(current["period_id"]) == {"p2"} and set(prev["period_id"]) == {"p1"}, f"{cur_label} / {prev_label}")
loaded = []
current, prev, cur_label, prev_label = comparison_basis(messages, periods, ["p2"], lambda pid: loaded.append(pid) or previous)
check("один период: сравнение с предыдущим загруженным", loaded == ["p1"] and prev_label.startswith("Апрель") and len(prev) == 1, f"{loaded} {prev_label}")
_c, prev_none, _l, _p = comparison_basis(messages, periods, ["p0"], lambda pid: previous)
check("самый ранний период: сравнивать не с чем", prev_none is None)

print("6. Раздел в приложении")
from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT
NOW = datetime.now(timezone.utc).isoformat()
PROJECT = "sources"
CLIENT.db["platform_projects"] = [
    {"project_id": PROJECT, "project_name": "Источники", "status": "active", "settings": {},
     "created_at": NOW, "updated_at": NOW, "viewer_code_hash": store.hash_code("viewer-code")}
]
CLIENT.db["platform_periods"] = [
    {"project_id": PROJECT, "period_id": pid, "period_name": name, "date_from": start, "date_to": end,
     "source_filename": "f.xlsx", "status": "active", "manifest": {}, "uploaded_at": f"{start}T12:00:00+00:00"}
    for pid, name, start, end in (("p1", "Апрель", "2026-04-01", "2026-04-07"), ("p2", "Май", "2026-05-01", "2026-05-07"))
]
table_rows = []
for index, row in enumerate(rows + [dict(msg("https://vk.com/club1", "Такси города", "ivan", period="p1"))]):
    payload = dict(row)
    day = "2026-05-02" if payload["period_id"] == "p2" else "2026-04-02"
    payload.update({"message_id": f"m{index}", "date": day, "datetime": f"{day}T10:00:00",
                    "message_link": f"https://example.com/{index}", "tags": "Тарифы", "event_title": ""})
    table_rows.append({"project_id": PROJECT, "period_id": payload["period_id"], "table_name": "messages",
                       "row_id": payload["message_id"], "payload": payload})
CLIENT.db["platform_table_rows"] = table_rows

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402


def open_sources(period_ids):
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
    at.session_state["platform_project_id"] = PROJECT
    at.session_state["platform_project_role"] = "viewer"
    at.session_state["platform_nav_page"] = "Источники"
    at.session_state[f"period_select_{PROJECT}"] = period_ids
    at.run()
    return at


at = open_sources(["p2"])
check("раздел открылся без исключений", not at.exception, str(at.exception))
check("раздел есть в меню пользователя", "Источники" in [str(b.label) for b in at.sidebar.button])
cards = {str(m.label): str(m.value) for m in at.metric}
check("площадок — три", cards.get("Площадок") == "3", str(cards))
check("авторов — четыре", cards.get("Авторов") == "4", str(cards))
check("новых площадок к апрелю — две", cards.get("Новых площадок") == "2", str(cards))
tables = [el.value for el in at.dataframe]
check("таблица площадок на экране", any("Площадка" in t.columns for t in tables), str([list(t.columns) for t in tables]))
check("таблица авторов на экране", any("Автор" in t.columns for t in tables))

at = open_sources(["p1"])
cards = {str(m.label): str(m.value) for m in at.metric}
check("самый ранний период: новых площадок — прочерк", cards.get("Новых площадок") == "—", str(cards))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Раздел «Источники» считает площадки и авторов правильно.")
