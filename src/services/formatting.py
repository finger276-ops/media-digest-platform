# -*- coding: utf-8 -*-
"""Общие хелперы форматирования, используемые в нескольких разделах UI."""

from __future__ import annotations

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
