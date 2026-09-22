# -*- coding: utf-8 -*-
"""Раздел «Клиентский обзор»: изменения к предыдущему периоду.

build_period_change_insights склоняет глагол по роду слова «Количество»
(среднего рода) — «Количество сообщений/аудитории/охвата/вовлечённости
ВЫРОСЛО», а не по роду существительного, которое идёт после него. Раньше
средний род учли только для «сообщений» («выросло»), а для «аудитории»,
«охвата» и «вовлечённости» ошибочно оставили женский род («выросла») —
в клиентском автотексте получалось «Количество аудитории выросла».

Мутационная проверка: вернуть родовую развилку по label (женский род по
умолчанию, средний только для "сообщений") -> тест "средний род для
аудитории/охвата/вовлечённости" краснеет.
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

from client_insights_ui import build_period_change_insights  # noqa: E402

failures = []


def check(label, condition, detail=""):
    mark = "  ✓ " if condition else "  ✗ "
    print(mark + label + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def messages_for(period_id, ids, audience, reach, engagement):
    return pd.DataFrame(
        [
            {
                "message_id": mid,
                "period_id": period_id,
                "sentiment": "нейтрал",
                "audience": audience,
                "views": reach,
                "engagement": engagement,
            }
            for mid in ids
        ]
    )


PERIODS = pd.DataFrame(
    [
        {"period_id": "p1", "period_name": "Период 1", "date_from": "2026-04-01", "date_to": "2026-04-07"},
        {"period_id": "p2", "period_name": "Период 2", "date_from": "2026-04-08", "date_to": "2026-04-14"},
    ]
)

print("1. Рост по всем метрикам — средний род для всех, не только для «сообщений»")
grown = pd.concat(
    [
        messages_for("p1", ["a", "b"], audience=500, reach=1000, engagement=10),
        messages_for("p2", ["c", "d", "e", "f"], audience=900, reach=2000, engagement=40),
    ],
    ignore_index=True,
)
insights_up = build_period_change_insights(grown, PERIODS, ["p1", "p2"])
text_up = " ".join(insights_up)
check("рост сообщений — средний род", "Количество сообщений выросло" in text_up, text_up)
check(
    "рост аудитории — средний род, а не «выросла»",
    "Количество аудитории выросло" in text_up and "выросла" not in text_up,
    text_up,
)
check(
    "рост охвата — средний род, а не «выросла»",
    "Количество охвата выросло" in text_up,
    text_up,
)
check(
    "рост вовлечённости — средний род, а не «выросла»",
    "Количество вовлеченности выросло" in text_up,
    text_up,
)

print("2. Падение по всем метрикам — средний род «снизилось» для всех")
dropped = pd.concat(
    [
        messages_for("p1", ["a", "b", "c", "d"], audience=900, reach=2000, engagement=40),
        messages_for("p2", ["e", "f"], audience=500, reach=1000, engagement=10),
    ],
    ignore_index=True,
)
insights_down = build_period_change_insights(dropped, PERIODS, ["p1", "p2"])
text_down = " ".join(insights_down)
check("падение сообщений — средний род", "Количество сообщений снизилось" in text_down, text_down)
check(
    "падение аудитории — средний род, а не «снизилась»",
    "Количество аудитории снизилось" in text_down and "снизилась" not in text_down,
    text_down,
)
check("падение охвата — средний род", "Количество охвата снизилось" in text_down, text_down)
check("падение вовлечённости — средний род", "Количество вовлеченности снизилось" in text_down, text_down)

print("3. Один период — сравнивать не с чем, пустой список без падения")
check(
    "один период не падает и не даёт инсайтов",
    build_period_change_insights(grown[grown["period_id"] == "p1"], PERIODS, ["p1"]) == [],
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Согласование рода в «Клиентском обзоре» верно для всех метрик.")
