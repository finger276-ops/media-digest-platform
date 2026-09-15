"""Проверка обогащения сообщений связями с инфоповодами и агрегации инфоповодов.

Этот код раньше жил внутри app.py без единого теста. Отдельного внимания
заслуживает fallback-путь enrich_messages: у выгрузок Brand Analytics по
сюжетам может не быть таблицы discussion/event-связей, и тогда событие
подбирается по совпадению исходной темы сообщения с event_title/
source_main_topic инфоповода.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.event_enrichment import (  # noqa: E402
    aggregate_events,
    build_event_description,
    enrich_messages,
    pick_event_description,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. enrich_messages: связь через discussion_messages/event_discussions")
messages = pd.DataFrame(
    [
        {"message_id": "m1", "text": "Первое сообщение"},
        {"message_id": "m2", "text": "Второе сообщение"},
        {"message_id": "m3", "text": "Без связи с темой"},
    ]
)
discussion_messages = pd.DataFrame(
    [
        {"message_id": "m1", "discussion_id": "d1"},
        {"message_id": "m2", "discussion_id": "d1"},
    ]
)
event_discussions = pd.DataFrame([{"discussion_id": "d1", "event_id": "e1"}])
events = pd.DataFrame([{"event_id": "e1", "event_title": "Запуск завода"}])

enriched = enrich_messages(messages, event_discussions, discussion_messages, events)
check(
    "связанные сообщения получили event_id",
    (enriched.loc[enriched["message_id"].isin(["m1", "m2"]), "event_id"] == "e1").all(),
    str(enriched[["message_id", "event_id"]].to_dict("records")),
)
check(
    "несвязанное сообщение осталось без event_id",
    enriched.loc[enriched["message_id"] == "m3", "event_id"].iloc[0] == "",
)
check(
    "заголовок темы подставлен по event_id",
    enriched.loc[enriched["message_id"] == "m1", "event_title"].iloc[0] == "Запуск завода",
)

print("2. enrich_messages: fallback по исходной теме (Brand Analytics без discussion-таблиц)")
ba_messages = pd.DataFrame(
    [
        {"message_id": "b1", "source_main_topic": "Запуск линии в Рязани"},
        {"message_id": "b2", "source_main_topic": "запуск линии в рязани"},  # тот же сюжет, другой регистр
        {"message_id": "b3", "source_main_topic": "Другая тема"},
    ]
)
ba_events = pd.DataFrame(
    [
        {"event_id": "be1", "source_main_topic": "Запуск линии в Рязани", "event_title": "Запуск линии в Рязани"},
    ]
)
ba_enriched = enrich_messages(ba_messages, pd.DataFrame(), pd.DataFrame(), ba_events)
check(
    "fallback сработал без таблиц discussion/event",
    ba_enriched.loc[ba_enriched["message_id"] == "b1", "event_id"].iloc[0] == "be1",
    str(ba_enriched[["message_id", "event_id"]].to_dict("records")),
)
check(
    "fallback нечувствителен к регистру",
    ba_enriched.loc[ba_enriched["message_id"] == "b2", "event_id"].iloc[0] == "be1",
)
check(
    "сообщение с другой темой не привязано к чужому инфоповоду",
    ba_enriched.loc[ba_enriched["message_id"] == "b3", "event_id"].iloc[0] == "",
)

print("3. enrich_messages: пустой messages не падает")
try:
    empty_result = enrich_messages(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    check("пустой messages возвращает DataFrame без исключений", isinstance(empty_result, pd.DataFrame))
except Exception as exc:
    check("пустой messages возвращает DataFrame без исключений", False, str(exc))

print("4. aggregate_events: косметические варианты заголовка (регистр/ё-е/кавычки) собираются в один инфоповод")
# aggregate_events склеивает только по нормализации написания (normalize_event_title):
# регистр, ё/е, кавычки/тире, пробелы в числах. Смысловая склейка разных
# формулировок одного сюжета — отдельный механизм (merge_similar_events),
# он сюда не относится и тестируется в test_event_titles.py.
raw_events = pd.DataFrame(
    [
        {
            "event_id": "e1",
            "event_title": "Запуск завода «Рязань»",
            "message_count": 5,
            "chat_count": 2,
            "negative_count": 1,
            "importance_score": 10,
            "main_tags": "Производство|ТЕХНОНИКОЛЬ",
            "start_date": "2026-04-24",
            "end_date": "2026-04-25",
        },
        {
            "event_id": "e2",
            "event_title": "ЗАПУСК ЗАВОДА — РЯЗАНЬ",  # тот же сюжет: регистр + другие кавычки/тире
            "message_count": 3,
            "chat_count": 1,
            "negative_count": 0,
            "importance_score": 8,
            "main_tags": "Производство",
            "start_date": "2026-04-26",
            "end_date": "2026-04-27",
        },
        {
            "event_id": "e3",
            "event_title": "Совсем другая тема",
            "message_count": 2,
            "chat_count": 1,
            "negative_count": 2,
            "importance_score": 5,
            "main_tags": "Жалобы",
            "start_date": "2026-04-24",
            "end_date": "2026-04-24",
        },
    ]
)
agg = aggregate_events(raw_events)
check("два сюжета в итоге (не три)", len(agg) == 2, str(agg["title"].tolist()))
zavod_row = agg[agg["title"].str.contains("Рязань")]
check("сообщения вариантов написания просуммированы", not zavod_row.empty and int(zavod_row.iloc[0]["message_count"]) == 8)
check(
    "вариант заголовка сохранён (не потерян при склейке)",
    not zavod_row.empty and zavod_row.iloc[0]["merged_titles"] == 1,
)
check(
    "негатив тоже просуммирован",
    not zavod_row.empty and int(zavod_row.iloc[0]["negative_count"]) == 1,
)
check(
    "доля негатива посчитана от суммарных сообщений",
    not zavod_row.empty and abs(float(zavod_row.iloc[0]["negative_share"]) - 1 / 8) < 1e-6,
)

print("5. build_event_description: авто-описание по ключевым словам")
group_negative = pd.DataFrame(
    [{"event_summary": "Клиенты жалуются на брак и дефекты партии", "main_tags": "Жалобы"}]
)
desc = build_event_description(group_negative)
check(
    "негативная тема размечена как жалобы/проблемы",
    "проблемы, жалобы" in desc,
    desc,
)
group_empty = pd.DataFrame([{"event_summary": "", "main_tags": ""}])
check(
    "пустая тема не падает и не выдумывает сигналы",
    build_event_description(group_empty) == "В теме обсуждались связанные сообщения выбранного периода.",
)

print("6. pick_event_description: ручное описание важнее автоматического")
group_manual = pd.DataFrame(
    [{"display_description": "Ручное описание аналитика", "event_summary": "жалобы на брак", "main_tags": ""}]
)
check(
    "ручное описание используется, если есть",
    pick_event_description(group_manual) == "Ручное описание аналитика",
)
group_auto = pd.DataFrame([{"event_summary": "жалобы на брак", "main_tags": ""}])
check(
    "без ручного описания используется автоматическое",
    "проблемы, жалобы" in pick_event_description(group_auto),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки обогащения сообщений и агрегации инфоповодов пройдены.")
