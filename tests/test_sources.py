# -*- coding: utf-8 -*-
"""Раздел «Источники»: площадки, авторы, новые площадки.

Площадка — сайт или соцсеть (vk.com, telegram.org, otzovik.com). Названия
сообществ и каналов в раздел не попадают.

Мутационные проверки (что ломает какой тест):
- площадки по названию сообщества, а не по домену -> «в таблице площадок нет
  названий сообществ» и «площадки — домены» краснеют;
- из ссылок берётся не только домен -> «профиль без адреса — не площадка»
  краснеет;
- без алиаса t.me -> «t.me и telegram.org — одна площадка» краснеет;
- без снятия m. -> «мобильный адрес — та же площадка» краснеет;
- аудитория площадки максимумом, а не суммой сообществ -> «аудитория площадки —
  сумма её сообществ, каждое один раз» краснеет;
- «Где пишет» по сообществам -> «где пишет автор — домены» краснеет;
- display_table без проверки метрики -> «нет охвата в выгрузке — прочерк»
  краснеет;
- display_table без проверки разметки -> «нет разметки — негатив прочерком»
  краснеет;
- new_sources без вычитания прошлых площадок -> «новые площадки — только те,
  которых не было» краснеет;
- главная площадка не первая по сообщениям -> «главная площадка — vk.com»
  краснеет;
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
    NO_SOURCE_LABEL,
    build_author_statistics,
    build_source_statistics,
    display_table,
    messages_of_source,
    new_sources,
    platform_labels,
    platform_name,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def msg(profile, title, author, *, period="p2", audience=1000, views=10, engagement=1, sentiment="нейтральная",
        platform="vk.com", platform_type="Соцсети", link="", declared="audience|reach|engagement"):
    return {
        "chat_profile": profile, "chat_title": title, "platform": platform, "platform_type": platform_type,
        "author": author, "author_profile": f"https://vk.com/{author}", "audience": audience, "views": views,
        "engagement": engagement, "sentiment": sentiment, "period_id": period, "message_link": link,
        mc.SOURCE_METRICS_COLUMN: declared, "text_clean": f"Сообщение от {author}",
    }


COMMUNITIES = ("Такси города", "Такси города (архив)", "Барахолка Луганск,Алчевск,", "Подслушано", "Такси чат")
rows = [
    # Три сообщества ВКонтакте — одна площадка vk.com.
    msg("https://vk.com/club1", "Такси города", "ivan", sentiment="негативная"),
    msg("https://vk.com/club1", "Такси города (архив)", "petr"),
    msg("https://vk.com/club1", "Такси города", "ivan", audience=1200),
    msg("https://vk.com/club2", "Барахолка Луганск,Алчевск,", "anna", audience=500),
    # Площадка не заполнена: домен из мобильного адреса сообщества.
    msg("https://m.vk.com/club3", "Подслушано", "olga", platform="", audience=300),
    # Telegram под двумя адресами — одна площадка.
    msg("https://t.me/taxi_chat", "Такси чат", "sergey", platform="", platform_type="Мессенджеры", audience=800),
    msg("https://t.me/taxi_chat", "Такси чат", "ivan", platform="telegram.org", platform_type="Мессенджеры", audience=800),
    # Отзыв без площадки и блога: домен из ссылки на сообщение.
    msg("", "", "masha", platform="", platform_type="Отзывы", audience=0, views=0,
        link="https://otzovik.com/review_1.html"),
]
messages = mc.prepare_dashboard_messages(pd.DataFrame(rows))

print("1. Площадки — домены, а не сообщества")
check("адрес → домен", platform_name("https://www.otzovik.com/review_1.html") == "otzovik.com")
check("регистр и алиас vk.ru", platform_name("VK.RU") == "vk.com")
check("название из колонки «Площадка» остаётся", platform_name("Одноклассники") == "Одноклассники")
check("профиль без адреса — не площадка", platform_labels(pd.DataFrame(
    [{"platform": "", "chat_profile": "club9", "chat_title": "Барахолка"}])).tolist() == [NO_SOURCE_LABEL])
sources = build_source_statistics(messages)
by_key = {row["_key"]: row for _, row in sources.iterrows()}
check("площадки — домены", list(sources["label"]) == ["vk.com", "telegram.org", "otzovik.com"], str(sources["label"].tolist()))
check("в таблице площадок нет названий сообществ",
      not any(name in value for name in COMMUNITIES
              for value in display_table(sources, messages, kind="sources").astype(str).values.ravel()),
      str(display_table(sources, messages, kind="sources").to_dict("records")))
check("мобильный адрес — та же площадка", by_key.get("vk.com", {}).get("messages") == 5, str(sources.to_dict("records")))
check("t.me и telegram.org — одна площадка", by_key.get("telegram.org", {}).get("messages") == 2, str(list(by_key)))
check("площадка из ссылки на сообщение", by_key.get("otzovik.com", {}).get("type") == "Отзывы", str(list(by_key)))
check("аудитория площадки — сумма её сообществ, каждое один раз",
      by_key["vk.com"]["audience"] == 1200 + 500 + 300 and by_key["telegram.org"]["audience"] == 800,
      f'{by_key["vk.com"]["audience"]} / {by_key["telegram.org"]["audience"]}')
check("охват — сумма по сообщениям", by_key["vk.com"]["reach"] == 50)
check("авторов у площадки — уникальные", by_key["vk.com"]["authors"] == 4)
check("доля негатива посчитана", abs(by_key["vk.com"]["negative_share"] - 1 / 5) < 1e-9)
check("сообщения площадки выбираются по ключу", len(messages_of_source(messages, "vk.com")) == 5)

print("2. Авторы")
authors = build_author_statistics(messages)
ivan = authors[authors["label"] == "ivan"]
check("автор с тремя сообщениями", not ivan.empty and int(ivan.iloc[0]["messages"]) == 3, str(authors.to_dict("records")))
check("где пишет автор — домены", not ivan.empty and ivan.iloc[0]["places"] == "vk.com, telegram.org",
      str(ivan["places"].tolist()))
check("в таблице авторов нет названий сообществ",
      not any(name in value for name in COMMUNITIES
              for value in display_table(authors, messages, kind="authors").astype(str).values.ravel()))

print("3. Прочерки вместо ложных нулей")
table = display_table(sources, messages, kind="sources")
check("охват есть — числа", table["Охват"].iloc[0] == "50", str(table.to_dict("records")))
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
check("новые площадки — только те, которых не было", set(fresh["_key"]) == {"telegram.org", "otzovik.com"},
      str(fresh["_key"].tolist()))
check("новое сообщество старой площадки — не новая площадка", "vk.com" not in set(fresh["_key"]))
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
                    "message_link": payload["message_link"] or f"https://vk.com/wall-1_{index}",
                    "tags": "Тарифы", "event_title": ""})
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
check("авторов — шесть", cards.get("Авторов") == "6", str(cards))
check("новых площадок к апрелю — две", cards.get("Новых площадок") == "2", str(cards))
check("главная площадка — vk.com", cards.get("Главная площадка") == "vk.com", str(cards))
tables = [el.value for el in at.dataframe]
platform_table = next((t for t in tables if "Площадка" in t.columns), None)
check("таблица площадок на экране — домены", platform_table is not None
      and list(platform_table["Площадка"]) == ["vk.com", "telegram.org", "otzovik.com"],
      str([list(t.columns) for t in tables]))
check("таблица авторов на экране", any("Автор" in t.columns for t in tables))
check("на экране нет названий сообществ", not any(
    name in value for t in tables for value in t.astype(str).values.ravel() for name in COMMUNITIES))

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
