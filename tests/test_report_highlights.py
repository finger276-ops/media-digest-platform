# -*- coding: utf-8 -*-
"""Единая выборка топ-тегов/топ-инфоповодов для отчёта.

Раньше это были три независимые реализации (превью «Что включить в отчёт»
на «Обзоре», PNG/DOCX/PDF-экспорт, карточка данных для ИИ) с разной
сортировкой и разным списком «технических» заголовков — при равенстве по
числу сообщений они могли выбрать РАЗНЫЙ набор из пяти, не только разный
порядок. top_report_tags/top_report_events — общая точка для всех трёх.

Мутационные проверки (что ломает какой тест):
- в top_report_events убрать сортировку по importance_score (оставить
  только message_count) -> тест "при равенстве сообщений побеждает
  importance_score" краснеет;
- в TECHNICAL_EVENT_TITLES убрать "прочие сообщения"/"общее обсуждение"
  (вернуть старый, более короткий список report_export.py) -> тест
  "объединённый список технических заголовков шире любого из старых"
  краснеет;
- в top_report_tags поменять сортировку на дефолтную (Сообщений, Аудитория,
  Охват, Вовлеченность вместо Сообщений, Охват, Вовлеченность) -> тест
  "при равенстве сообщений и аудитории решает охват" краснеет.
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

from services.report_highlights import (  # noqa: E402
    TECHNICAL_EVENT_TITLES,
    event_title_column,
    is_technical_event_title,
    top_report_events,
    top_report_tags,
)

failures = []


def check(label, condition, detail=""):
    mark = "  ✓ " if condition else "  ✗ "
    print(mark + label + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. is_technical_event_title / event_title_column")
check("«Без сюжета» - технический", is_technical_event_title("Без сюжета"))
check("«без_сюжета» (подчёркивание) - технический", is_technical_event_title("без_сюжета"))
check("«Прочие сообщения» - технический", is_technical_event_title("Прочие сообщения"))
check("«Общее обсуждение» - технический", is_technical_event_title("Общее обсуждение"))
check("регистр и «ё» не важны", is_technical_event_title("ОБЩЕЕ ОБСУЖДЕНИЕ"))
check("пустая строка - технический (нечего показывать)", is_technical_event_title(""))
check("реальный заголовок - не технический", not is_technical_event_title("Запуск завода"))
check(
    "список объединяет оба старых варианта (шире любого из них)",
    {"без_сюжета", "прочие сообщения", "общее обсуждение"} <= TECHNICAL_EVENT_TITLES,
    str(TECHNICAL_EVENT_TITLES),
)
df_title = pd.DataFrame({"event_title": ["a"], "title": ["b"]})
check(
    "event_title_column берёт по приоритету (event_title раньше title)",
    event_title_column(df_title) == "event_title",
)
check("нет колонки заголовка - None", event_title_column(pd.DataFrame({"x": [1]})) is None)

print("2. top_report_tags: сортировка и лимит")
# A и B - по 2 сообщения (равенство), C - только 1 (не участвует в споре за
# первое место, но должен остаться позади обоих). У A выше охват, чем у B -
# при равенстве по «Сообщений» решает именно охват, а не аудитория (у B она
# как раз больше - если бы решала аудитория, B был бы первым).
messages = pd.DataFrame(
    {
        "message_id": [f"m{i}" for i in range(5)],
        "tags": ["A", "A", "B", "B", "C"],
        "audience": [100, 100, 500, 500, 10],
        "views": [50, 50, 10, 10, 900],
        "engagement": [1, 1, 1, 1, 1],
        "sentiment": ["позитив"] * 5,
    }
)
top = top_report_tags(messages, limit=2)
check("лимит соблюдён", len(top) == 2, str(len(top)))
check(
    "равенство по сообщениям решает охват (A: 100 > B: 20), а не аудитория (у B она больше)",
    list(top["Тег"]) == ["A", "B"],
    str(list(top["Тег"])),
)
check(
    "C (1 сообщение) не попал в топ-2, несмотря на больший охват",
    "C" not in set(top["Тег"]),
    str(list(top["Тег"])),
)
tie_messages = pd.DataFrame(
    {
        "message_id": [f"m{i}" for i in range(4)],
        "tags": ["X", "X", "Y", "Y"],
        "audience": [900, 900, 10, 10],
        "views": [10, 10, 900, 900],
        "engagement": [1, 1, 1, 1],
        "sentiment": ["позитив"] * 4,
    }
)
tie_top = top_report_tags(tie_messages, limit=2)
check(
    "при равенстве сообщений и разном соотношении аудитория/охват - побеждает охват (Y), не аудитория (X)",
    list(tie_top["Тег"])[0] == "Y",
    str(list(tie_top["Тег"])),
)
check("пустые сообщения -> пустой список, не исключение", top_report_tags(pd.DataFrame()).empty)

print("3. top_report_events: фильтр технических заголовков и сортировка")
events = pd.DataFrame(
    {
        "title": ["Запуск завода", "Без сюжета", "Жалобы на монтаж", "Общее обсуждение"],
        "message_count": [10, 50, 10, 5],
        "importance_score": [8.0, 1.0, 9.0, 1.0],
    }
)
top_events = top_report_events(events, limit=5)
check(
    "технические заголовки отфильтрованы, даже с большим числом сообщений",
    "Без сюжета" not in set(top_events["title"]) and "Общее обсуждение" not in set(top_events["title"]),
    str(list(top_events["title"])),
)
check(
    "при равенстве сообщений (10 и 10) побеждает importance_score (Жалобы на монтаж выше Запуска завода)",
    list(top_events["title"]) == ["Жалобы на монтаж", "Запуск завода"],
    str(list(top_events["title"])),
)
check(
    "нет колонки заголовка -> пустой список, не исключение",
    top_report_events(pd.DataFrame({"message_count": [1]})).empty,
)
check(
    "все события технические -> пустой список",
    top_report_events(pd.DataFrame({"title": ["Без сюжета"], "message_count": [100]})).empty,
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Единая выборка топ-тегов/топ-инфоповодов работает корректно.")
