# -*- coding: utf-8 -*-
"""Автотекст «Саммари периода» без ИИ — читаемый вид с метриками и динамикой.

Аналитик пожаловался: пока никто не сгенерировал текст моделью, на экране и
в выгрузке видна россыпь предложений без структуры — ни самих метрик
(аудитория/охват/вовлечённость — только число сообщений и доля негатива),
ни динамики к предыдущему периоду. build_auto_summary (summary_ui.py)
переписан: переиспользует metrics_block/comparison_block из services/
ai_summary.py (те же функции, что строят карточку данных для ИИ — не
третья независимая реализация) и размечает подзаголовки маркером "## "
(report_export._classify_summary_line превращает его в оформленный
подзаголовок при экспорте; Streamlit рисует его как заголовок «из коробки»
через st.markdown).

Заодно из client_insights_ui.build_client_insights_summary убраны две
дублирующие вставки: агрегатная динамика периода (теперь есть один раз в
build_auto_summary) и топ тегов/инфоповодов со статистикой (дублировал
отдельный оформленный блок отчёта — report_export._PdfTopListsBlock).

Мутационные проверки (что ломает какой тест):
- в build_auto_summary убрать вызов metrics_block -> тест "содержит блок
  «Метрики периода» с реальными числами" краснеет;
- показывать "## Динамика" всегда (не проверять comparison.previous/current)
  -> тест "без сравнения периодов блока «Динамика» нет" краснеет;
- вернуть в client_insights_ui дублирующий список "Топ тегов для отчета"
  -> тест "автотекст не дублирует топ тегов/инфоповодов" краснеет.
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

from services.metrics_compute import overview_metrics  # noqa: E402
from services.period_comparison import build_comparison_metrics  # noqa: E402
from summary_ui import build_auto_summary  # noqa: E402

failures = []


def check(label, condition, detail=""):
    mark = "  ✓ " if condition else "  ✗ "
    print(mark + label + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


MESSAGES = pd.DataFrame(
    [
        {
            "message_id": "a",
            "chat_title": "Канал 1",
            "author": "u1",
            "period_id": "p1",
            "sentiment": "негатив",
            "audience": 1000,
            "views": 2000,
            "engagement": 50,
        },
        {
            "message_id": "b",
            "chat_title": "Канал 1",
            "author": "u2",
            "period_id": "p1",
            "sentiment": "позитив",
            "audience": 1500,
            "views": 3000,
            "engagement": 80,
        },
        {
            "message_id": "c",
            "chat_title": "Канал 2",
            "author": "u3",
            "period_id": "p1",
            "sentiment": "нейтрал",
            "audience": 500,
            "views": 900,
            "engagement": 10,
        },
    ]
)
EVENTS_AGG = pd.DataFrame(
    [
        {"title": "Запуск завода", "message_count": 2, "views": 5000, "engagement": 130, "importance_score": 5.0},
        {"title": "Без сюжета", "message_count": 1, "views": 900, "engagement": 10, "importance_score": 1.0},
    ]
)
PERIODS = pd.DataFrame(
    [{"period_id": "p1", "period_name": "Период 1", "date_from": "2026-04-24", "date_to": "2026-04-30"}]
)

print("1. Один период: метрики есть, «Динамики» нет (сравнивать не с чем)")
metrics_single = overview_metrics(MESSAGES)
text_single = build_auto_summary(MESSAGES, EVENTS_AGG, PERIODS, ["p1"], metrics=metrics_single)
check("блок «Метрики периода» на месте", "## Метрики периода" in text_single, text_single)
check("реальное число сообщений в тексте (не выдумано)", "Сообщений: 3" in text_single, text_single)
check("тональность посчитана (не только доля негатива в интро)", "Тональность:" in text_single, text_single)
check(
    "без сравнения периодов блока «Динамика» нет (не показываем «сравнивать не с чем»)",
    "## Динамика" not in text_single,
    text_single,
)
check("блок «Клиентский обзор» размечен как подзаголовок", "## Клиентский обзор" in text_single, text_single)
check(
    "автотекст не дублирует топ тегов/инфоповодов (это отдельный блок отчёта)",
    "Топ тегов для отчета" not in text_single and "Топ инфоповодов для отчета" not in text_single,
    text_single,
)

print("2. Два периода: «Динамика» появляется с реальными «было/стало», не filler-строкой")
messages_p1 = MESSAGES.copy()
messages_p2 = MESSAGES.copy()
messages_p2["period_id"] = "p2"
messages_p2["message_id"] = ["d", "e", "f"]
# У второго периода на 2 сообщения больше - есть реальная дельта, которую
# можно найти в тексте.
messages_p2 = pd.concat(
    [
        messages_p2,
        pd.DataFrame(
            [
                {"message_id": "g", "chat_title": "Канал 1", "author": "u4", "period_id": "p2", "sentiment": "негатив", "audience": 400, "views": 300, "engagement": 5},
                {"message_id": "h", "chat_title": "Канал 1", "author": "u5", "period_id": "p2", "sentiment": "негатив", "audience": 400, "views": 300, "engagement": 5},
            ]
        ),
    ],
    ignore_index=True,
)
messages_two_periods = pd.concat([messages_p1, messages_p2], ignore_index=True)
periods_two = pd.concat(
    [
        PERIODS,
        pd.DataFrame([{"period_id": "p2", "period_name": "Период 2", "date_from": "2026-05-01", "date_to": "2026-05-07"}]),
    ],
    ignore_index=True,
)
aggregate = build_comparison_metrics(messages_two_periods, periods_two, ["p1", "p2"])
check("сравнение двух периодов построилось", aggregate is not None)
text_two = build_auto_summary(messages_two_periods, EVENTS_AGG, periods_two, ["p1", "p2"], metrics=aggregate)
check("блок «Динамика» появился, когда есть с чем сравнивать", "## Динамика" in text_two, text_two)
check(
    "динамика содержит реальную дельту (было 3, стало 5), а не filler-текст",
    "было 3" in text_two and "стало 5" in text_two,
    text_two,
)
check(
    "filler-фраза «сравнивать не с чем» не просочилась в текст при реальном сравнении",
    "сравнивать не с чем" not in text_two,
    text_two,
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Автотекст саммари без ИИ читаем и содержит метрики/динамику.")
