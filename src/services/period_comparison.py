# -*- coding: utf-8 -*-
"""Хелперы для сравнения периодов: хронологический порядок и метрики по периоду.

Используется разделом «Обзор» (последовательное сравнение периодов) и
«Клиентским обзором» (что изменилось к предыдущему периоду).
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .formatting import fmt_date_short, period_picker_label
from .metrics_compute import format_int, overview_metrics, percent_text


def previous_period_id(
    periods: pd.DataFrame, selected_ids: list[str]
) -> str | None:
    """Период, который идёт перед самым ранним из выбранных.

    Нужен, чтобы динамика была видна даже когда открыт один период — важно не
    абсолютное число, а «стало больше или меньше».

    Живёт здесь, а не в app.py: тем же вопросом задаются и индексы бренда, а
    импортировать из роутера значило бы завести круговую зависимость.
    """
    if periods is None or periods.empty or not selected_ids:
        return None
    if "period_id" not in periods.columns:
        return None
    work = periods.copy()
    order = pd.to_datetime(work.get("date_from"), errors="coerce")
    if order.isna().all():
        order = pd.to_datetime(work.get("uploaded_at"), errors="coerce")
    work["_order"] = order
    work = work.sort_values("_order", na_position="first")
    ordered = work["period_id"].astype(str).tolist()
    selected = {str(x) for x in selected_ids}
    positions = [i for i, pid in enumerate(ordered) if pid in selected]
    if not positions or positions[0] == 0:
        return None
    return ordered[positions[0] - 1]


def period_row_label(row: pd.Series, fallback: str = "") -> str:
    if not isinstance(row, pd.Series):
        return fallback or "период"
    # period_picker_label - тот же хелпер, что и подписи в сайдбаре: без
    # года, без дублирования даты, если название периода и так уже дата.
    # Этот "label" попадает и в "Последний период: ..." на инфографике, и в
    # текст для ИИ (comparison_block) - раньше там оседали полные даты.
    return period_picker_label(row, fallback=fallback or "период")


def selected_period_rows(periods: pd.DataFrame, period_ids: list[str]) -> pd.DataFrame:
    ids = [str(x) for x in (period_ids or []) if str(x).strip()]
    if (
        not ids
        or periods is None
        or periods.empty
        or "period_id" not in periods.columns
    ):
        return pd.DataFrame({"period_id": ids})
    work = periods[periods["period_id"].astype(str).isin(ids)].copy()
    if work.empty:
        return pd.DataFrame({"period_id": ids})
    # Preserve missing selected ids so comparison does not silently lose a period.
    existing = set(work["period_id"].astype(str))
    missing = [pid for pid in ids if pid not in existing]
    if missing:
        work = pd.concat(
            [work, pd.DataFrame({"period_id": missing})], ignore_index=True
        )
    return work


def ordered_period_ids(periods: pd.DataFrame, period_ids: list[str]) -> list[str]:
    """Return selected period ids in chronological order for sequence comparison."""
    ids = [str(x) for x in (period_ids or []) if str(x).strip()]
    if len(ids) <= 1:
        return ids
    rows = selected_period_rows(periods, ids).copy()
    if rows.empty or "period_id" not in rows.columns:
        return ids

    date_series = pd.Series(pd.NaT, index=rows.index, dtype="datetime64[ns]")
    for col in ["date_from", "start_date", "uploaded_at", "date_to", "end_date"]:
        if col in rows.columns:
            parsed = pd.to_datetime(rows[col], errors="coerce", dayfirst=True)
            date_series = date_series.combine_first(parsed)
    rows["_sort_date"] = date_series
    rows["_input_order"] = (
        rows["period_id"].astype(str).map({pid: i for i, pid in enumerate(ids)})
    )
    rows = rows.sort_values(
        ["_sort_date", "_input_order"], na_position="last", kind="mergesort"
    )
    ordered = rows["period_id"].astype(str).tolist()
    # If all dates are missing, keep the user's selection order.
    if rows["_sort_date"].isna().all():
        return ids
    # Preserve any ids that were not present in metadata.
    for pid in ids:
        if pid not in ordered:
            ordered.append(pid)
    return ordered


def period_metrics_for_comparison(
    messages: pd.DataFrame, periods: pd.DataFrame, period_ids: list[str]
) -> list[dict[str, Any]]:
    ordered_ids = ordered_period_ids(periods, period_ids)
    result: list[dict[str, Any]] = []
    if len(ordered_ids) < 2:
        return result

    rows = selected_period_rows(periods, ordered_ids)
    row_by_id = (
        {str(r.get("period_id")): r for _, r in rows.iterrows()}
        if not rows.empty
        else {}
    )
    for pid in ordered_ids:
        if (
            isinstance(messages, pd.DataFrame)
            and not messages.empty
            and "period_id" in messages.columns
        ):
            subset = messages[messages["period_id"].astype(str) == str(pid)].copy()
        else:
            subset = pd.DataFrame()
        metrics = overview_metrics(subset)
        sent = metrics.get("sentiment", {})
        total = max(1, int(sent.get("total", 0) or 0))
        row = row_by_id.get(str(pid), pd.Series({"period_id": pid}))
        metrics.update(
            {
                "period_id": str(pid),
                "label": period_row_label(row, str(pid)),
                "positive_share": (
                    float(sent.get("positive", 0) or 0) / total if total else 0.0
                ),
                "neutral_share": (
                    float(sent.get("neutral", 0) or 0) / total if total else 0.0
                ),
                "negative_share": (
                    float(sent.get("negative", 0) or 0) / total if total else 0.0
                ),
            }
        )
        result.append(metrics)
    return result


def daily_metrics_for_comparison(messages: pd.DataFrame) -> list[dict[str, Any]]:
    """То же самое, что period_metrics_for_comparison, но по календарным дням.

    Раньше динамика считалась по загруженным периодам целиком (неделя —
    одна точка на графике), хотя у каждого сообщения уже есть точная дата
    (колонка "datetime", см. services/message_normalize.py). Разбивка по
    дням показывает движение внутри самой загрузки, а не только между
    файлами — так и просил аналитик: «грузится период с 1 по 7, но на
    графиках это разделено по дням».

    Форма результата совпадает с period_metrics_for_comparison один в один
    (те же ключи: period_id/label/messages/.../*_share), поэтому всё
    остальное — build_comparison_metrics, comparison_visual_rows, таблица,
    круговые диаграммы — работает без изменений, просто на других точках.
    """
    if (
        not isinstance(messages, pd.DataFrame)
        or messages.empty
        or "datetime" not in messages.columns
    ):
        return []
    work = messages.copy()
    work["_day"] = pd.to_datetime(work["datetime"], errors="coerce").dt.floor("D")
    work = work.dropna(subset=["_day"])
    if work.empty:
        return []
    days = sorted(work["_day"].unique())
    if len(days) < 2:
        return []

    result: list[dict[str, Any]] = []
    for day in days:
        subset = work[work["_day"] == day]
        metrics = overview_metrics(subset)
        sent = metrics.get("sentiment", {})
        total = max(1, int(sent.get("total", 0) or 0))
        day_ts = pd.Timestamp(day)
        metrics.update(
            {
                "period_id": day_ts.strftime("%Y-%m-%d"),
                "label": day_ts.strftime("%d.%m"),
                "positive_share": (
                    float(sent.get("positive", 0) or 0) / total if total else 0.0
                ),
                "neutral_share": (
                    float(sent.get("neutral", 0) or 0) / total if total else 0.0
                ),
                "negative_share": (
                    float(sent.get("negative", 0) or 0) / total if total else 0.0
                ),
            }
        )
        result.append(metrics)
    return result


def selected_period_label(periods: pd.DataFrame, period_ids: list[str]) -> str:
    """Human-readable label for the currently selected period set."""
    ids = [str(x) for x in (period_ids or []) if str(x).strip()]
    if not ids:
        return "выбранный период"
    if periods is None or periods.empty or "period_id" not in periods.columns:
        return ", ".join(ids[:3]) + (f" и еще {len(ids) - 3}" if len(ids) > 3 else "")

    subset = periods[periods["period_id"].astype(str).isin(ids)].copy()
    if subset.empty:
        return ", ".join(ids[:3]) + (f" и еще {len(ids) - 3}" if len(ids) > 3 else "")

    if len(subset) == 1:
        # period_picker_label - тот же хелпер, что и подписи в сайдбаре: не
        # дублирует дату дважды, если название периода и так уже дата
        # ("24.04.2026-30.04.2026 · 24.04.2026-30.04.2026" было ровно такой
        # подписью раньше - видно даже в имени файла выгрузки), и не тащит
        # год туда, где он не нужен.
        return period_picker_label(subset.iloc[0], fallback=ids[0])

    dates: list[pd.Timestamp] = []
    for col in ["date_from", "date_to", "start_date", "end_date"]:
        if col in subset.columns:
            parsed = pd.to_datetime(
                subset[col], errors="coerce", dayfirst=True
            ).dropna()
            dates.extend(parsed.tolist())
    if dates:
        start_s = fmt_date_short(min(dates))
        end_s = fmt_date_short(max(dates))
        date_part = (
            f"{start_s}–{end_s}"
            if start_s and end_s and start_s != end_s
            else start_s or end_s
        )
    else:
        date_part = ""

    names = [
        str(x).strip()
        for x in subset.get("period_name", pd.Series(dtype=str)).fillna("").tolist()
        if str(x).strip()
    ]
    if len(names) <= 3 and names:
        name_part = "; ".join(names)
    else:
        name_part = f"{len(subset)} период(а/ов)"
    return f"{name_part} · {date_part}" if date_part else name_part


def build_comparison_metrics(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    *,
    granularity: str = "period",
) -> dict[str, Any] | None:
    """Посчитать последовательное сравнение периодов без отрисовки.

    Нужна и разделу «Динамика», и выгрузкам в разделе «Отчёт», поэтому расчёт
    отделён от интерфейса.

    granularity="day" — точки по календарным дням внутри выбранных периодов
    (см. daily_metrics_for_comparison), с откатом на period_metrics_for_
    comparison, если дней с датой меньше двух (например, дата не
    распозналась при импорте). По умолчанию — «period», как было: отчёт в
    разделе «Отчёт» специально не переключен на дни, чтобы не менять
    поведение выгрузок.
    """
    comparison = (
        daily_metrics_for_comparison(messages) if granularity == "day" else []
    )
    if len(comparison) < 2:
        comparison = period_metrics_for_comparison(messages, periods, period_ids)
    if len(comparison) < 2:
        return None
    previous, current = comparison[-2], comparison[-1]
    first, last = comparison[0], comparison[-1]
    aggregate = overview_metrics(messages)
    aggregate["period_label"] = selected_period_label(periods, period_ids)
    aggregate["comparison_sequence"] = comparison
    aggregate["comparison"] = {
        "first": first,
        "previous": previous,
        "current": current,
        "last": last,
    }
    return aggregate


def metric_delta(current: float, previous: float) -> str:
    try:
        current = float(current or 0)
        previous = float(previous or 0)
    except (TypeError, ValueError):
        return "0"
    diff = current - previous
    sign = "+" if diff > 0 else ""
    if previous:
        pct = diff / previous * 100
        pct_sign = "+" if pct > 0 else ""
        return f"{sign}{format_int(diff)} ({pct_sign}{pct:.0f}%)"
    if diff:
        return f"{sign}{format_int(diff)}"
    return "0"


def pp_delta(current_share: float, previous_share: float) -> str:
    diff = (float(current_share or 0) - float(previous_share or 0)) * 100
    sign = "+" if diff > 0 else ""
    return f"{sign}{diff:.1f} п.п."


def comparison_row(
    metric: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    sent = metric.get("sentiment", {}) or {}
    row = {
        "Период": metric.get("label", metric.get("period_id", "")),
        "Сообщений": format_int(metric.get("messages", 0)),
        "Δ сообщений": (
            "—"
            if previous is None
            else metric_delta(metric.get("messages", 0), previous.get("messages", 0))
        ),
        "Аудитория": format_int(metric.get("audience", 0)),
        "Δ аудитории": (
            "—"
            if previous is None
            else metric_delta(metric.get("audience", 0), previous.get("audience", 0))
        ),
        "Охват": format_int(metric.get("reach", 0)),
        "Δ охвата": (
            "—"
            if previous is None
            else metric_delta(metric.get("reach", 0), previous.get("reach", 0))
        ),
        "Вовлеченность": format_int(metric.get("engagement", 0)),
        "Δ вовлеченности": (
            "—"
            if previous is None
            else metric_delta(
                metric.get("engagement", 0), previous.get("engagement", 0)
            )
        ),
        "Позитив": percent_text(sent.get("positive", 0), sent.get("total", 0)),
        "Δ позитива": (
            "—"
            if previous is None
            else pp_delta(
                metric.get("positive_share", 0), previous.get("positive_share", 0)
            )
        ),
        "Нейтрал": percent_text(sent.get("neutral", 0), sent.get("total", 0)),
        "Δ нейтрала": (
            "—"
            if previous is None
            else pp_delta(
                metric.get("neutral_share", 0), previous.get("neutral_share", 0)
            )
        ),
        "Негатив": percent_text(sent.get("negative", 0), sent.get("total", 0)),
        "Δ негатива": (
            "—"
            if previous is None
            else pp_delta(
                metric.get("negative_share", 0), previous.get("negative_share", 0)
            )
        ),
    }
    return row


_YEAR_IN_DATE_RE = re.compile(r"(\d{2}\.\d{2})\.\d{4}")


def short_period_chart_label(label: Any) -> str:
    """Compact period label for chart axes: only the period name, without repeated dates.

    Год убирается из дат («24.04.2026» → «24.04»): на оси графика он не
    несёт пользы — сравниваемые периоды почти всегда в пределах одного
    года, — а место экономит, что и было целью подписи. Произвольные
    (не автосгенерированные) названия периодов regex не трогает.
    """
    raw = str(label or "").strip()
    if not raw:
        return "Период"
    raw = _YEAR_IN_DATE_RE.sub(r"\1", raw)
    for sep in [" · ", " — ", " - "]:
        if sep in raw:
            raw = raw.split(sep, 1)[0].strip()
            break
    if len(raw) > 28:
        raw = raw[:25].rstrip() + "…"
    return raw or "Период"


def dedupe_chart_labels(labels: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for label in labels:
        base = str(label or "Период").strip() or "Период"
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base} #{seen[base]}")
    return result


def comparison_visual_rows(comparison: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    raw_labels: list[str] = []
    for item in comparison:
        full_label = str(item.get("label", item.get("period_id", "")) or "")
        raw_labels.append(short_period_chart_label(full_label))
    short_labels = dedupe_chart_labels(raw_labels)

    for item, short_label in zip(comparison, short_labels):
        sent = item.get("sentiment", {}) or {}
        total = max(1, int(sent.get("total", 0) or 0))
        full_label = str(item.get("label", item.get("period_id", "")) or "")
        rows.append(
            {
                "Период": short_label,
                "Полный период": full_label,
                "Сообщения": int(item.get("messages", 0) or 0),
                "Аудитория": int(item.get("audience", 0) or 0),
                "Охват": int(item.get("reach", 0) or 0),
                "Вовлеченность": int(item.get("engagement", 0) or 0),
                "Позитив, %": round(
                    float(sent.get("positive", 0) or 0) / total * 100, 1
                ),
                "Нейтрал, %": round(
                    float(sent.get("neutral", 0) or 0) / total * 100, 1
                ),
                "Негатив, %": round(
                    float(sent.get("negative", 0) or 0) / total * 100, 1
                ),
                "Позитив": int(sent.get("positive", 0) or 0),
                "Нейтрал": int(sent.get("neutral", 0) or 0),
                "Негатив": int(sent.get("negative", 0) or 0),
            }
        )
    return pd.DataFrame(rows)


def chart_number_label(value: Any, *, percent: bool = False) -> str:
    try:
        number = float(value or 0)
    except Exception:
        number = 0.0
    if percent:
        raw = f"{number:.1f}".replace(".", ",")
        raw = raw[:-2] if raw.endswith(",0") else raw
        return f"{raw}%"
    return format_int(number)


COMPARISON_TABLE_VIEWS = {
    "Сообщения": [("Сообщений", "messages"), ("Δ сообщений", None)],
    "Аудитория": [("Аудитория", "audience"), ("Δ аудитории", None)],
    "Охват": [("Охват", "reach"), ("Δ охвата", None)],
    "Вовлеченность": [("Вовлеченность", "engagement"), ("Δ вовлеченности", None)],
    "Тональность": [],
    "Все показатели": [],
}


def build_comparison_table(
    comparison: list[dict[str, Any]], view: str = "Сообщения"
) -> pd.DataFrame:
    """Сравнительная таблица по одному показателю.

    Полная таблица на четырнадцать колонок не помещается на экран и обрезается
    справа, поэтому по умолчанию показывается один показатель с изменением.
    """
    rows: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for item in comparison:
        full = comparison_row(item, previous)
        if view == "Все показатели":
            rows.append(full)
        elif view == "Тональность":
            rows.append(
                {
                    key: full[key]
                    for key in [
                        "Период",
                        "Позитив",
                        "Δ позитива",
                        "Нейтрал",
                        "Δ нейтрала",
                        "Негатив",
                        "Δ негатива",
                    ]
                    if key in full
                }
            )
        else:
            keys = ["Период"] + [name for name, _ in COMPARISON_TABLE_VIEWS.get(view, [])]
            rows.append({key: full[key] for key in keys if key in full})
        previous = item
    return pd.DataFrame(rows)
