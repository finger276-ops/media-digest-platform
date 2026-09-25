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

from metric_cards_ui import DELTA_INVERSE, DELTA_NEUTRAL, DELTA_NORMAL  # noqa: E402
from overview_ui import _tone_cards  # noqa: E402

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

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Цвет карточек тональности зависит от направления, а не только от знака.")
