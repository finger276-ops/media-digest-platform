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

Разделы 4-5 проверяют две находки независимой проверки коммита 9b89f3d:
  - карточка «Инфоповодов» и сигнал «Темы с негативом» считали остаточную
    корзину «Без сюжета» как обычную тему — на СМИ-выгрузке с одним таким
    инфоповодом карточка писала «1», а раздел «Инфоповоды» на тех же данных
    писал «ни одного»;
  - «Что изменилось к предыдущему периоду» сравнивало периоды по messages,
    уже суженным гранулярностью до части выбранных периодов: период, чьи дни
    не попали в узкий выбор, считался по нулю сообщений, и получались
    выдуманные «выросло с 0 до 10» и «доля негатива +50 п.п.» на ровном месте.

Мутационные проверки:
- убрать фильтр is_residual в _drop_residual (return events_agg как есть) ->
  падают «карточка не считает остаточную корзину» и «сигнал не подхватывает
  негатив из остаточной корзины»;
- убрать использование reportable_events для risky_events (вернуть
  events_agg.copy()) -> падает «сигнал не подхватывает негатив из остаточной
  корзины»;
- убрать ветку granularity_narrowed (всегда показывать инсайты) -> падает
  «при сужении гранулярностью показана причина, а не выдуманные числа».
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

from client_insights_ui import _drop_residual, build_period_change_insights  # noqa: E402

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

print("4. Остаточная корзина «Без сюжета» не считается инфоповодом")
events_with_residual = pd.DataFrame(
    [
        {"event_id": "e1", "title": "Тема 1", "message_count": 4, "negative_count": 0, "is_residual": False},
        {"event_id": "e2", "title": "Тема 2", "message_count": 3, "negative_count": 0, "is_residual": False},
        {
            "event_id": "e_residual",
            "title": "Без сюжета",
            "message_count": 5,
            "negative_count": 5,
            "is_residual": True,
        },
    ]
)
check(
    "_drop_residual убирает только остаточную корзину",
    len(_drop_residual(events_with_residual)) == 2
    and "e_residual" not in set(_drop_residual(events_with_residual)["event_id"]),
    str(_drop_residual(events_with_residual)["event_id"].tolist()),
)
check(
    "без колонки is_residual кадр не трогается",
    len(_drop_residual(events_with_residual.drop(columns=["is_residual"]))) == 3,
)
check("пустой кадр не падает", _drop_residual(pd.DataFrame()).empty)

from streamlit.testing.v1 import AppTest  # noqa: E402

from client_insights_ui import render_client_insights  # noqa: E402


def _insights_app():
    import streamlit as st

    from client_insights_ui import render_client_insights

    render_client_insights(
        st.session_state["messages"],
        st.session_state["events_agg"],
        st.session_state["periods"],
        st.session_state["selected_period_ids"],
        granularity_narrowed=st.session_state.get("granularity_narrowed", False),
    )


def run_insights(messages, events_agg, periods, selected_period_ids, *, granularity_narrowed=False):
    app = AppTest.from_function(_insights_app, default_timeout=60)
    app.session_state["messages"] = messages
    app.session_state["events_agg"] = events_agg
    app.session_state["periods"] = periods
    app.session_state["selected_period_ids"] = selected_period_ids
    app.session_state["granularity_narrowed"] = granularity_narrowed
    app.run()
    return app


single_period_messages = messages_for("p1", ["a", "b"], audience=500, reach=1000, engagement=10)
app4 = run_insights(single_period_messages, events_with_residual, PERIODS, ["p1"])
check("раздел с остаточной корзиной открылся без исключений", not app4.exception, str(app4.exception))
cards4 = {str(m.label): str(m.value) for m in app4.metric}
check(
    "карточка «Инфоповодов» не считает остаточную корзину",
    cards4.get("Инфоповодов") == "2",
    str(cards4),
)
signals4 = " ".join(str(m.value) for m in app4.markdown)
check(
    "сигнал «Темы с негативом» не подхватывает негатив из остаточной корзины",
    "Темы с негативом" not in signals4,
    signals4[:300],
)

print("5. Сужение гранулярностью прячет сравнение, а не выдуманные числа")
insights_period_msgs = pd.concat(
    [
        messages_for("p1", ["a", "b"], audience=500, reach=1000, engagement=10),
        messages_for("p2", ["c", "d", "e", "f"], audience=900, reach=2000, engagement=40),
    ],
    ignore_index=True,
)
no_events = pd.DataFrame(columns=["event_id", "title", "message_count", "negative_count", "is_residual"])

app5_full = run_insights(
    insights_period_msgs, no_events, PERIODS, ["p1", "p2"], granularity_narrowed=False
)
check("без сужения раздел открылся без исключений", not app5_full.exception, str(app5_full.exception))
text5_full = " ".join(str(m.value) for m in app5_full.markdown)
check(
    "без сужения показаны настоящие изменения",
    "Количество сообщений выросло" in text5_full,
    text5_full[:300],
)

app5_narrow = run_insights(
    insights_period_msgs, no_events, PERIODS, ["p1", "p2"], granularity_narrowed=True
)
check("при сужении раздел открылся без исключений", not app5_narrow.exception, str(app5_narrow.exception))
captions5 = [str(c.value) for c in app5_narrow.caption]
text5_narrow = " ".join(str(m.value) for m in app5_narrow.markdown)
check(
    "при сужении гранулярностью показана причина, а не выдуманные числа",
    any("отмечены не все дни периода" in c for c in captions5)
    and "Количество сообщений выросло" not in text5_narrow,
    str(captions5) + " | " + text5_narrow[:200],
)

print("6. Текст «Клиентского обзора» для саммари и выгрузок при сужении")
# Находка проверки c46fa02: экранный блок при сужении гранулярностью скрыт,
# а текстовая версия (build_client_insights_summary — она уходит в саммари
# «Отчёта» и в Word/PDF/PNG) по-прежнему писала «Теги с заметными
# изменениями»: период, чьи дни выпали из выбора, считался по нулю.
# Мутационная проверка: игнорировать granularity_narrowed в
# build_client_insights_summary → падает «при сужении тегов с изменениями нет».
from client_insights_ui import build_client_insights_summary  # noqa: E402

tagged = insights_period_msgs.assign(tags="Бренд")
summary_full = build_client_insights_summary(tagged, no_events, PERIODS, ["p1", "p2"])
check("без сужения в тексте есть теги с изменениями", "Теги с заметными изменениями" in summary_full, summary_full)
summary_narrow = build_client_insights_summary(
    tagged[tagged["period_id"] == "p2"], no_events, PERIODS, ["p1", "p2"], granularity_narrowed=True
)
check(
    "при сужении тегов с изменениями в тексте нет (p1 выпал из выбора, а не опустел)",
    "Теги с заметными изменениями" not in summary_narrow,
    summary_narrow,
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Согласование рода в «Клиентском обзоре» верно для всех метрик.")
