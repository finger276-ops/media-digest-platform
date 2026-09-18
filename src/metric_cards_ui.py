# -*- coding: utf-8 -*-
"""Полоса показателей: одинаковые числа — одинаковые карточки.

Метрики рисовались в шести местах по-разному: где-то в рамке, где-то без, где-то
по четыре в ряду, где-то по три. Одни и те же числа выглядели то сводкой, то
подписью к заголовку, и глаз каждый раз перестраивался заново.

Здесь один способ на всю платформу. Поменять вид карточек теперь можно в одном
месте, а не искать по разделам.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import streamlit as st

# Цвет у изменения утверждает «лучше» или «хуже». Для части метрик это неправда:
# высокая доля голоса не всегда хороша, а рост числа сообщений может быть ростом
# скандала. Там, где направление неоднозначно, изменение показывается серым.
DELTA_NORMAL = "normal"
DELTA_INVERSE = "inverse"
DELTA_NEUTRAL = "off"

DEFAULT_COLUMNS = 4

# st.metric не переносит длинную подпись/значение по словам — при узкой
# карточке текст просто выходит за её границы и визуально прячется под
# следующей карточкой в ряду (не аккуратное "...", а буквальное наложение
# текста). Перенос строк задаёт не сам stMetricLabel/stMetricValue, а
# вложенный <p> внутри stMarkdownContainer — именно там остаётся
# white-space: nowrap, если переопределить стиль только на внешнем блоке.
# Разрешаем перенос строк — карточка станет выше, а не обрежет соседнюю.
# Один инжект на всю платформу, так как карточки везде общие.
_WRAP_CSS = """
<style>
[data-testid="stMetricLabel"] p,
[data-testid="stMetricValue"] p {
    white-space: normal !important;
    overflow-wrap: break-word;
    word-break: break-word;
}
</style>
"""


def metric_card(
    label: str,
    value: Any,
    *,
    delta: Any = None,
    delta_color: str = DELTA_NORMAL,
    help_text: str = "",
) -> dict[str, Any]:
    """Описание одной карточки — чтобы вызовы читались как данные."""
    return {
        "label": str(label),
        "value": value,
        "delta": delta,
        "delta_color": delta_color,
        "help": help_text,
    }


def render_metric_row(
    cards: Sequence[dict[str, Any]] | Iterable[dict[str, Any]],
    *,
    columns: int = DEFAULT_COLUMNS,
) -> None:
    """Нарисовать показатели карточками в рамке, по `columns` в ряду.

    Ряды набираются по порядку: последний может быть неполным, и это лучше,
    чем растягивать три карточки на всю ширину — так они выглядели бы крупнее
    остальных без всякой причины.
    """
    items = [card for card in cards if card]
    if not items:
        return
    st.markdown(_WRAP_CSS, unsafe_allow_html=True)
    width = max(1, int(columns))
    for start in range(0, len(items), width):
        chunk = items[start : start + width]
        # Пустые колонки в хвосте нужны, чтобы карточки последнего ряда были
        # той же ширины, что и в предыдущих.
        for column, card in zip(st.columns(width), chunk):
            with column, st.container(border=True):
                st.metric(
                    card.get("label", ""),
                    card.get("value", ""),
                    delta=card.get("delta"),
                    delta_color=card.get("delta_color", DELTA_NORMAL),
                    help=card.get("help") or None,
                )
