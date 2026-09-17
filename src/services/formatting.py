# -*- coding: utf-8 -*-
"""Общие хелперы форматирования, используемые в нескольких разделах UI."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd


def fmt_date(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        # pd.isna на массиве или несравнимом объекте — значит, это не NaN.
        pass
    try:
        ts = pd.to_datetime(value, errors="coerce", dayfirst=True)
        if pd.isna(ts):
            return ""
        return ts.strftime("%d.%m.%Y")
    except Exception:  # noqa: BLE001 — непонятная дата в ленте показывается пустой
        return ""


def fmt_period(row: pd.Series) -> str:
    start = fmt_date(row.get("date_from") or row.get("start_date"))
    end = fmt_date(row.get("date_to") or row.get("end_date"))
    if start and end and start != end:
        return f"{start}–{end}"
    return start or end


def fmt_date_short(value: Any) -> str:
    """Дата без года: "24.04" вместо "24.04.2026".

    Год в подписи периода почти никогда не несёт пользы — сравниваемые
    периоды внутри проекта почти всегда в пределах одного года, — а место
    в узких элементах интерфейса (пилюли селектора, оси графиков) экономит.
    """
    full = fmt_date(value)
    if not full:
        return ""
    parts = full.split(".")
    return ".".join(parts[:2]) if len(parts) == 3 else full


def fmt_period_short(row: pd.Series) -> str:
    start = fmt_date_short(row.get("date_from") or row.get("start_date"))
    end = fmt_date_short(row.get("date_to") or row.get("end_date"))
    if start and end and start != end:
        return f"{start}–{end}"
    return start or end


_DATE_ONLY_RE = re.compile(r"^[\d.\-–\s]+$")


def looks_like_date_range(text: str) -> bool:
    """Похоже ли название периода на автосгенерированное "24.04.2026-30.04.2026".

    Отличает автоматическое имя (только цифры, точки, дефисы/тире и пробелы)
    от осмысленного, которое аналитик ввёл сам ("Апрельская волна") - для
    первого дублировать даты дважды в одной подписи незачем, для второго имя
    несёт смысл, который даты не заменяют.
    """
    text = (text or "").strip()
    return bool(text) and bool(_DATE_ONLY_RE.match(text))


def period_picker_label(row: pd.Series, fallback: str = "") -> str:
    """Подпись периода для селектора в сайдбаре: имя + короткие даты без года.

    Раньше подпись всегда была "имя · полная_дата_с_годом", и для
    автосгенерированных имён (они и есть дата) год повторялся дважды в
    одной строке: "24.04.2026-30.04.2026 · 24.04.2026-30.04.2026".
    """
    name = str(row.get("period_name") or "").strip()
    date_part = fmt_period_short(row)
    if name and looks_like_date_range(name):
        return date_part or name or fallback
    if name:
        return f"{name} · {date_part}" if date_part else name
    return date_part or fallback
