# -*- coding: utf-8 -*-
"""Срез по тегам: весь дашборд — только по сообщениям выбранных тегов.

Два периода загружаются настоящей загрузкой: в каждом сообщения двух брендов
(«Технониколь» и «Кнауф») с разными текстами, чтобы инфоповоды собрались по
брендам.

Мутационные проверки (что ломает какой тест):
- срез не применяется к сообщениям -> «Обзор: сообщений — только среза»
  краснеет;
- прошлый период не режется срезом -> «изменение — к срезу прошлого периода»
  краснеет;
- инфоповоды без сообщений среза остаются -> «в инфоповодах нет чужого
  бренда» и «инфоповод без сообщений в выборке уходит» краснеют;
- «Индексы бренда» получают срез -> «индексы бренда — без среза» краснеет;
- отчёт при срезе показывает сохранённое саммари периода -> «отчёт: саммари
  по срезу, а не сохранённое» краснеет;
- правка саммари при срезе не скрыта -> «при срезе правки саммари нет»
  краснеет;
- выбор среза не сверяется с новой выборкой -> «тег того же написания
  остаётся в срезе после смены периода» краснеет.
"""

import io
import os
import sys
import tempfile
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

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services.ingest import ingest_file_bytes  # noqa: E402
from services.manual_moderation import drop_events_without_messages, recompute_event_counts  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Инфоповоды после сужения выборки")
events = pd.DataFrame({"event_id": ["e1", "e2"], "event_title": ["Кровля", "Штукатурка"],
                       "message_count": [5, 7], "negative_count": [0, 0]})
narrowed = pd.DataFrame({"event_id": ["e1", "e1"], "message_id": ["m1", "m2"]})
kept = recompute_event_counts(drop_events_without_messages(events, narrowed), narrowed)
check("инфоповод без сообщений в выборке уходит", kept["event_id"].tolist() == ["e1"], str(kept.to_dict("records")))
check("счётчик оставшегося пересчитан", kept["message_count"].tolist() == [2])
check("без колонки инфоповода — ничего не выкидывается",
      len(drop_events_without_messages(events, pd.DataFrame({"message_id": ["m1"]}))) == 2)

print("2. Дашборд со срезом")
NOW = datetime.now(timezone.utc).isoformat()
PROJECT = "slice_project"
WORK_DIR = tempfile.mkdtemp(prefix="tag_slice_")
CLIENT.db["platform_projects"] = [
    {"project_id": PROJECT, "project_name": "Кровля и стены", "status": "active",
     "viewer_code_hash": store.hash_code("viewer-code"), "editor_code_hash": store.hash_code("editor-code"),
     "settings": {"topic_profile": "universal",
                  "category_brands": {"own": ["Технониколь"], "competitors": ["Кнауф"]}},
     "created_at": NOW, "updated_at": NOW}
]
for table in ("platform_periods", "platform_table_rows", "platform_manual_rows", "platform_sessions"):
    CLIENT.db[table] = []

ROOF = ["Кровля Технониколь протекает после первого дождя", "Мембрана Технониколь порвалась на стыке кровли",
        "Технониколь заменил кровельный материал по гарантии"]
WALL = ["Штукатурка Кнауф трескается через месяц", "Гипсокартон Кнауф отсырел в ванной",
        "Кнауф выпустил новую шпаклёвку для стен"]


def export(start_day: int, tags_of) -> bytes:
    """Выгрузка Brand Analytics: сюжет — инфоповод, теги — колонки после «Обработано»."""
    rows = []
    for i in range(24):
        tags = tags_of(i).split("|")
        roof = "Технониколь" in tags
        texts = ROOF if roof else WALL
        rows.append({
            "ID сообщения": f"m{start_day}_{i}",
            "Дата": f"{start_day + i // 8:02d}.04.2026",
            "Время": f"{9 + i % 8:02d}:00",
            "Сообщение": f"{texts[i % 3]} #{start_day}-{i}",
            "Автор": f"Автор {i}",
            "Url": f"https://vk.com/wall-1_{start_day}{i:02d}",
            "Источник": ["Кровельщики", "Стройка"][i % 2],
            "Тональность": ["негатив", "нейтрал", "позитив"][i % 3],
            "Аудитория": str(1000 + i),
            "Просмотры": str(100 + i),
            "Вовлеченность": str(1 + i),
            "Тип источника": "Соцсети",
            "Тип": "Пост",
            "Сюжет": "Протечки кровли Технониколь" if roof else "Трещины в штукатурке Кнауф",
            "Обработано": "да",
            "Технониколь": "Технониколь" if roof else "",
            "Кнауф": "Кнауф" if "Кнауф" in tags else "",
            "Кровля": "Кровля" if "Кровля" in tags else "",
        })
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


# Апрель-1: Технониколь 12, Кнауф 12. Апрель-2: Технониколь 16, Кнауф 8.
FIRST = ingest_file_bytes(export(3, lambda i: ["Технониколь", "Кнауф", "Кнауф", "Технониколь|Кровля"][i % 4]),
                          project_id=PROJECT, source_filename="april_1.xlsx", source_system="auto", work_dir=WORK_DIR)
SECOND = ingest_file_bytes(export(13, lambda i: ["Технониколь", "Технониколь|Кровля", "Кнауф"][i % 3]),
                           project_id=PROJECT, source_filename="april_2.xlsx", source_system="auto", work_dir=WORK_DIR)
P1, P2 = str(FIRST["period_id"]), str(SECOND["period_id"])
store.save_manual(PROJECT, "ai_texts", f"ai_text::risks::{P2}", {"text": "Риски всего периода.", "kind": "risks"})
store.save_manual(PROJECT, "summaries", f"summary::{P2}",
                  {"summary": "Сохранённое саммари всего периода.", "period_ids": [P2]})

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from tag_slice_ui import BRAND_INDEX_NOTE, slice_state_key  # noqa: E402

SLICE_KEY = slice_state_key(PROJECT)


def open_page(page, *, slice_tags=None, periods=(P2,), admin=True):
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=240)
    if admin:
        at.session_state["platform_is_admin"] = True
    at.session_state["platform_project_id"] = PROJECT
    at.session_state["platform_project_role"] = "editor"
    at.session_state["platform_nav_page"] = page
    at.session_state[f"period_select_{PROJECT}"] = list(periods)
    at.session_state[f"granularity_mode::{PROJECT}::{'|'.join(sorted(periods))}"] = "period"
    if slice_tags is not None:
        at.session_state[SLICE_KEY] = list(slice_tags)
    at.run()
    return at


def slice_widget(at):
    return next((m for m in at.multiselect if str(m.label) == "Срез по тегам"), None)


def metric(at, label):
    return next((m for m in at.metric if str(m.label) == label), None)


at = open_page("Обзор")
check("дашборд открылся", not at.exception, str(at.exception))
widget = slice_widget(at)
check("выбор среза на месте", widget is not None, str([str(m.label) for m in at.multiselect]))
check("в списке — число сообщений тега", widget is not None and widget.format_func("Технониколь") == "Технониколь · 16",
      widget.format_func("Технониколь") if widget is not None else "")
total = metric(at, "Сообщений")
check("без среза — все сообщения периода", total is not None and str(total.value) == "24", str(total.value if total else None))

at = open_page("Обзор", slice_tags=["Технониколь"])
check("со срезом дашборд открылся", not at.exception, str(at.exception))
total = metric(at, "Сообщений")
check("Обзор: сообщений — только среза", total is not None and str(total.value) == "16", str(total.value if total else None))
check("изменение — к срезу прошлого периода (16 против 12)", total is not None and str(total.delta).startswith("+4 "),
      repr(total.delta if total else None))
risks = [str(m.value) for m in at.markdown if "Риски периода" in str(m.value)]
check("риски ИИ подписаны как текст по всему периоду", any("без среза" in r for r in risks), str(risks))

print("3. Разделы со срезом")
at = open_page("Сообщения", slice_tags=["Технониколь"])
tag_lines = [str(c.value) for c in at.caption if str(c.value).startswith("Теги: ")]
check("Сообщения: только сообщения среза", tag_lines and all("Технониколь" in line for line in tag_lines),
      str(tag_lines[:4]))

at = open_page("Инфоповоды", slice_tags=["Технониколь"])
check("Инфоповоды открылись", not at.exception, str(at.exception))
page_text = " ".join(str(el.value) for el in list(at.markdown) + list(at.caption) + list(at.info))
tables = " ".join(t.to_string() for t in (el.value for el in at.dataframe))
check("инфоповоды собрались", "Протечки кровли Технониколь" in page_text + tables,
      (page_text + tables)[:300])
check("в инфоповодах нет чужого бренда", "Технониколь" in tables and "Кнауф" not in tables and "Штукатурка" not in tables
      and "Гипсокартон" not in tables, tables[:300])

plain_brand = {str(m.label): str(m.value) for m in open_page("Индексы бренда").metric}
at = open_page("Индексы бренда", slice_tags=["Технониколь"])
check("Индексы бренда открылись", not at.exception, str(at.exception))
sliced_brand = {str(m.label): str(m.value) for m in at.metric}
# Со срезом по своему бренду доля голоса стала бы 100 %.
check("индексы бренда — без среза", bool(plain_brand) and sliced_brand == plain_brand,
      f"{plain_brand} / {sliced_brand}")
check("подпись, что срез к индексам не применён", any(BRAND_INDEX_NOTE in str(c.value) for c in at.caption))

print("4. Отчёт со срезом")
at = open_page("Отчёт", slice_tags=["Технониколь"])
check("Отчёт открылся", not at.exception, str(at.exception))
texts = " ".join(str(m.value) for m in at.markdown)
check("отчёт: саммари по срезу, а не сохранённое", "Сохранённое саммари всего периода" not in texts
      and any("Срез по тегам (тег: Технониколь)" in str(c.value) for c in at.caption),
      str([str(c.value) for c in at.caption if "Срез" in str(c.value)]))
check("при срезе правки саммари нет", "Редактировать саммари" not in [str(e.label) for e in at.expander],
      str([str(e.label) for e in at.expander]))
at = open_page("Отчёт")
check("без среза — сохранённое саммари и правка на месте",
      "Сохранённое саммари всего периода" in " ".join(str(m.value) for m in at.markdown)
      and "Редактировать саммари" in [str(e.label) for e in at.expander])

print("5. Смена периода")
at = open_page("Обзор", slice_tags=["ТЕХНОНИКОЛЬ", "Кровля"], periods=(P1,))
widget = slice_widget(at)
check("тег того же написания остаётся в срезе после смены периода",
      widget is not None and sorted(widget.value) == ["Кровля", "Технониколь"], str(widget.value if widget else None))
total = metric(at, "Сообщений")
check("срез в другом периоде — его сообщения", total is not None and str(total.value) == "12",
      str(total.value if total else None))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Срез по тегам режет весь дашборд, кроме индексов бренда.")
