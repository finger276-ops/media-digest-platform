# -*- coding: utf-8 -*-
"""Раздел «Обзор»: цвет изменения тональности.

Рост негатива в карточке «Негатив» красился зелёной стрелкой вверх, как
хорошая новость (тот же цвет, что у роста позитива), потому что metric_card
нигде не передавал DELTA_INVERSE — он был объявлен в metric_cards_ui, но не
использовался ни разу. Нейтрал неоднозначен в обе стороны (рост нейтрала
может значить и что скандал утих, и что бренд перестали обсуждать), поэтому
его изменение серое, без оценки.

Два соседних недочёта той же проверки (несопоставимое сравнение периодов в
шапке «Обзора» — несколько выбранных периодов или сужение гранулярностью)
проверяются сквозным прогоном там, где уже есть готовая фикстура: несколько
периодов — в tests/test_ui_smoke.py (раздел «3.2»), сужение гранулярностью —
в tests/test_granularity_ui.py (раздел «7»).

Мутационные проверки:
- убрать _TONE_DELTA_COLOR (карточка без delta_color) -> падает «Негатив —
  инверсия цвета» (metric_card тогда использует DELTA_NORMAL по умолчанию);
- поменять местами inverse/neutral для негатива и нейтрала -> падают «Негатив
  — инверсия цвета» и «Нейтрал — нейтральный цвет».
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
filled = _fill_daily_chart_gaps(gap)
check("тихий день добавлен - три точки вместо двух", len(filled) == 3, str(len(filled)))
if len(filled) == 3:
    check("порядок дат по возрастанию", [p["period_id"] for p in filled] == ["2026-04-27", "2026-04-28", "2026-04-29"], str([p["period_id"] for p in filled]))
    check("вставленный день пустой (0 сообщений)", filled[1]["messages"] == 0, str(filled[1]))
    check("подпись вставленного дня — просто дата, без выдумок", filled[1]["label"] == "28.04", filled[1]["label"])
    check("настоящие дни не тронуты", filled[0] is gap[0] and filled[2] is gap[1])

check("без пропусков список не меняется (та же последовательность)", _fill_daily_chart_gaps([gap[0], gap[0]]) == [gap[0], gap[0]])
check("меньше двух точек - как есть", _fill_daily_chart_gaps([gap[0]]) == [gap[0]])
check(
    "period_id не похож на дату (сработал откат на периоды целиком) - как есть, без исключения",
    _fill_daily_chart_gaps([{"period_id": "p1"}, {"period_id": "p2"}]) == [{"period_id": "p1"}, {"period_id": "p2"}],
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

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Цвет карточек тональности и заполнение тихих дней на графике — на месте.")
