# -*- coding: utf-8 -*-
"""Раздел «Обзор»: цвет изменения тональности.

Рост негатива в карточке «Негатив» красился зелёной стрелкой вверх, как
хорошая новость (тот же цвет, что у роста позитива), потому что metric_card
нигде не передавал DELTA_INVERSE — он был объявлен в metric_cards_ui, но не
использовался ни разу. Нейтрал неоднозначен в обе стороны (рост нейтрала
может значить и что скандал утих, и что бренд перестали обсуждать), поэтому
его изменение серое, без оценки.

Несопоставимое сравнение периодов в шапке «Обзора» (несколько выбранных
периодов или сужение гранулярностью) проверяется сквозным прогоном в
tests/test_overview_previous_period.py.

Мутационные проверки:
- убрать _TONE_DELTA_COLOR (карточка без delta_color) -> падает «Негатив —
  инверсия цвета» (metric_card тогда использует DELTA_NORMAL по умолчанию);
- поменять местами inverse/neutral для негатива и нейтрала -> падают «Негатив
  — инверсия цвета» и «Нейтрал — нейтральный цвет»;
- убрать возврат "0" при неизменной доле -> падает «доля не изменилась —
  дельта "0"» (Streamlit рисовал бы красную стрелку «рост негатива»);
- вставлять нули без проверки covered_days -> падает «день вне выгрузки не
  становится нулём»;
- собирать tone_items из исходной comparison -> падает «круговая тональности
  показывает свой день».
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from metric_cards_ui import DELTA_INVERSE, DELTA_NEUTRAL, DELTA_NORMAL  # noqa: E402
from overview_ui import _fill_daily_chart_gaps, _tone_cards  # noqa: E402
from services.period_comparison import comparison_visual_rows  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Цвет изменения зависит от того, хороша ли метрика при росте")
sent_now = {"positive": 20, "neutral": 60, "negative": 20, "total": 100}
sent_prev = {"positive": 10, "neutral": 60, "negative": 30, "total": 100}
cards = _tone_cards(sent_now, sent_prev)
by_label = {c["label"]: c for c in cards}
check(
    "Позитив — обычный цвет (рост зелёный)",
    by_label["Позитив"]["delta_color"] == DELTA_NORMAL,
    by_label["Позитив"]["delta_color"],
)
check(
    "Нейтрал — нейтральный цвет (направление неоднозначно)",
    by_label["Нейтрал"]["delta_color"] == DELTA_NEUTRAL,
    by_label["Нейтрал"]["delta_color"],
)
check(
    "Негатив — инверсия цвета (рост красный, а не зелёный)",
    by_label["Негатив"]["delta_color"] == DELTA_INVERSE,
    by_label["Негатив"]["delta_color"],
)
check(
    "у негатива в примере реальное падение доли (проверка не тривиальна)",
    by_label["Негатив"]["delta"] is not None and "-" in str(by_label["Негатив"]["delta"]),
    str(by_label["Негатив"]["delta"]),
)
check(
    "без прошлого периода дельты нет ни у одной карточки",
    all(c["delta"] is None for c in _tone_cards(sent_now, None)),
    str([c["delta"] for c in _tone_cards(sent_now, None)]),
)
# Streamlit считает «без изменений» только строку "0": «0,0 п.п.» он рисует
# стрелкой вверх, а у негатива с инверсией — красной, то есть «негатив вырос».
same_neg = _tone_cards(
    {"positive": 1, "neutral": 17, "negative": 2, "total": 20},
    {"positive": 2, "neutral": 7, "negative": 1, "total": 10},
)
check(
    "доля негатива не изменилась (10% и 10%) — дельта \"0\", без стрелки",
    {c["label"]: c for c in same_neg}["Негатив"]["delta"] == "0",
    str({c["label"]: c["delta"] for c in same_neg}),
)
check(
    "реальное изменение по-прежнему показывается в п.п.",
    "п.п." in str({c["label"]: c for c in same_neg}["Позитив"]["delta"]),
    str({c["label"]: c["delta"] for c in same_neg}),
)

def _day_point(day, messages, positive=0, negative=0):
    total = positive + negative
    return {
        "period_id": day,
        "label": pd.Timestamp(day).strftime("%d.%m"),
        "messages": messages,
        "audience": messages * 100,
        "reach": messages * 200,
        "engagement": messages * 10,
        "sentiment": {
            "positive": positive,
            "neutral": max(0, total - positive - negative),
            "negative": negative,
            "total": total,
            "has_markup": bool(total),
        },
        "positive_share": positive / total if total else 0.0,
        "neutral_share": 0.0,
        "negative_share": negative / total if total else 0.0,
    }


print("2. Тихий день не выпадает из графика, а становится нулевой точкой")
# daily_metrics_for_comparison строит бакет, только если в нём есть хоть одно
# сообщение — тихий день молча пропадал из последовательности, и линия на
# графике рисовала ровный переход между соседними днями, будто ничего не
# менялось. Реальный случай из проверки: 5, 0, 5 сообщений за три дня подряд
# оставляли на графике только две точки.
gap = [
    _day_point("2026-04-27", 5, positive=5),
    _day_point("2026-04-29", 5, positive=5),
]
april_upload = set(pd.date_range("2026-04-24", "2026-04-30", freq="D"))
filled = _fill_daily_chart_gaps(gap, april_upload)
check("тихий день добавлен - три точки вместо двух", len(filled) == 3, str(len(filled)))
if len(filled) == 3:
    check("порядок дат по возрастанию", [p["period_id"] for p in filled] == ["2026-04-27", "2026-04-28", "2026-04-29"], str([p["period_id"] for p in filled]))
    check("вставленный день пустой (0 сообщений)", filled[1]["messages"] == 0, str(filled[1]))
    check("подпись вставленного дня — просто дата, без выдумок", filled[1]["label"] == "28.04", filled[1]["label"])
    check("настоящие дни не тронуты", filled[0] is gap[0] and filled[2] is gap[1])

check("без пропусков список не меняется (та же последовательность)", _fill_daily_chart_gaps([gap[0], gap[0]], april_upload) == [gap[0], gap[0]])
check("меньше двух точек - как есть", _fill_daily_chart_gaps([gap[0]], april_upload) == [gap[0]])
check(
    "period_id не похож на дату (сработал откат на периоды целиком) - как есть, без исключения",
    _fill_daily_chart_gaps([{"period_id": "p1"}, {"period_id": "p2"}], april_upload) == [{"period_id": "p1"}, {"period_id": "p2"}],
)
# Находка проверки d369aec: нулём становились и дни, которых нет ни в одной
# загрузке (разрыв между двумя выгрузками) или снятые в пикере гранулярности —
# а за них в выгрузке сотни упоминаний. Ноль — только внутри выгрузки.
two_uploads = set(pd.date_range("2026-03-01", "2026-03-07", freq="D")) | set(
    pd.date_range("2026-03-16", "2026-03-22", freq="D")
)
split = _fill_daily_chart_gaps(
    [
        _day_point("2026-03-05", 3, positive=3),
        _day_point("2026-03-07", 3, positive=3),
        _day_point("2026-03-16", 3, positive=3),
    ],
    two_uploads,
)
check(
    "день вне выгрузки не становится нулём, тихий внутри — становится",
    [p["period_id"] for p in split] == ["2026-03-05", "2026-03-06", "2026-03-07", "2026-03-16"],
    str([p["period_id"] for p in split]),
)
check(
    "охват неизвестен (или гранулярность сужена) — нулей не вставляется",
    _fill_daily_chart_gaps(gap, None) == gap and _fill_daily_chart_gaps(gap, set()) == gap,
)

print("3. День без единого сообщения не попадает в круговую/линию тональности как «размечен»")
# sentiment_unmarked считает пустой период измеренным нулём (это верно для
# карточек: 0 периода — законный ноль), но для доли 0/0 в тональности это
# дало бы выдуманные «Позитив 0%, Нейтрал 0%, Негатив 0%» вместо «данных нет».
visual = comparison_visual_rows(filled)
check(
    "у вставленного дня тональность не считается размеченной",
    not bool(visual.iloc[1]["Тональность размечена"]),
    str(visual.iloc[1].to_dict()),
)
check(
    "у настоящих дней тональность по-прежнему размечена",
    bool(visual.iloc[0]["Тональность размечена"]) and bool(visual.iloc[2]["Тональность размечена"]),
    str(visual[["Тональность размечена"]].to_dict()),
)
check(
    "число сообщений вставленного дня — 0, а не выдуманное",
    int(visual.iloc[1]["Сообщения"]) == 0,
    str(visual.iloc[1]["Сообщения"]),
)

print("4. Графики «Динамики» с тихим днём: круговая тональности и подписи")
# Находки проверки d369aec: круговая тональности брала пары «дата ↔
# тональность» из исходной последовательности, а таблицу точек — из
# заполненной: после тихого дня каждая дата получала тональность соседней, а
# последний день — «нет данных». И тихий день попадал в подпись «Без
# разметки тональности», хотя разметка у проекта есть.
from streamlit.testing.v1 import AppTest  # noqa: E402


def _charts_app():
    import pandas as _pd
    import streamlit as st

    from overview_ui import render_period_comparison_charts

    render_period_comparison_charts(
        st.session_state["comparison"],
        granularity="day",
        covered_days=set(_pd.date_range("2026-04-01", "2026-04-04", freq="D")),
        visible_blocks_default=["Динамика тональности"],
    )


donut_days = [
    _day_point("2026-04-01", 4, positive=4),
    _day_point("2026-04-03", 4, negative=4),
    _day_point("2026-04-04", 4, positive=2, negative=2),
]
charts = AppTest.from_function(_charts_app, default_timeout=60)
charts.session_state["comparison"] = donut_days
charts.run()
check("графики с тихим днём без исключений", not charts.exception, str(charts.exception))
tone_captions = [str(c.value) for c in charts.caption]
check(
    "тихий день не назван «без разметки тональности»",
    not any("Без разметки тональности" in c for c in tone_captions),
    str(tone_captions),
)
check(
    "тихий день назван своими словами",
    any("Упоминаний не было" in c and "02.04" in c for c in tone_captions),
    str(tone_captions),
)
tone_type = [s for s in charts.selectbox if str(s.label) == "Вид тональности"]
if tone_type:
    tone_type[0].set_value("Круговая диаграмма").run()
    picker = [s for s in charts.selectbox if str(s.label) == "Период для круговой диаграммы тональности"]
    check("выбор дня для круговой есть", bool(picker), str([s.label for s in charts.selectbox]))
    if picker:
        picker[0].set_value("03.04").run()
        values = " ".join(str(m.value) for m in charts.markdown)
        check(
            "круговая тональности показывает свой день (03.04: только негатив)",
            "Негатив" in values and "Позитив" not in values,
            values[:300],
        )
        picker = [s for s in charts.selectbox if str(s.label) == "Период для круговой диаграммы тональности"]
        picker[0].set_value("04.04").run()
        no_data = [str(c.value) for c in charts.caption if "нет данных для круговой" in str(c.value)]
        check("последний день не превращается в «нет данных»", not no_data, str(no_data))
else:
    check("переключатель вида тональности найден", False, str([s.label for s in charts.selectbox]))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Цвет карточек тональности и заполнение тихих дней на графике — на месте.")
