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
from .metrics_compute import format_int, overview_metrics, percent_text, sentiment_unmarked


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


_MONTH_NAMES_RU = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


def _bucket_start(messages: pd.DataFrame, granularity: str) -> pd.Series | None:
    """Начало бакета (день/понедельник недели/1-е число месяца) для каждого
    сообщения по его СОБСТВЕННОЙ дате - единая точка истины для границ
    бакета, чтобы подсчёт метрик (*_metrics_for_comparison) и фильтр
    сообщений (filter_messages_by_buckets) не могли разъехаться в подсчёте
    (иначе в списке дней для выбора одно число, а на странице другое)."""
    if (
        not isinstance(messages, pd.DataFrame)
        or messages.empty
        or "datetime" not in messages.columns
    ):
        return None
    dt = pd.to_datetime(messages["datetime"], errors="coerce")
    if granularity == "day":
        return dt.dt.floor("D")
    if granularity == "week":
        return (dt - pd.to_timedelta(dt.dt.weekday, unit="D")).dt.floor("D")
    if granularity == "month":
        return dt.dt.to_period("M").dt.to_timestamp()
    return None


def _bucket_id(bucket_start: pd.Timestamp, granularity: str) -> str:
    if granularity == "month":
        return bucket_start.strftime("%Y-%m")
    return bucket_start.strftime("%Y-%m-%d")


# Выше этого числа дней охват периода не раскрывается поштучно: у выгрузки с
# ошибочной датой «с 1900 года» множество дней было бы бессмысленно большим.
MAX_COVERAGE_DAYS = 3660


def _parse_period_date(value: Any) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce", format="ISO8601")
    if pd.isna(parsed):
        parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    ts = pd.Timestamp(parsed)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()


def period_coverage_days(
    periods: pd.DataFrame | None, period_ids: list[str] | None
) -> set[pd.Timestamp] | None:
    """Календарные дни, которые покрыты выбранными загрузками (date_from–date_to).

    Тихий день и день, которого нет ни в одной загрузке, по сообщениям не
    отличить: ни там, ни там нет ни одного упоминания. Отличает их только
    диапазон выгрузки. None — у выбранных периодов нет распознанных дат, и
    охват неизвестен.
    """
    ids = {str(x) for x in (period_ids or []) if str(x).strip()}
    if (
        not ids
        or periods is None
        or periods.empty
        or "period_id" not in periods.columns
    ):
        return None
    days: set[pd.Timestamp] = set()
    known = False
    for _, row in periods[periods["period_id"].astype(str).isin(ids)].iterrows():
        start = _parse_period_date(row.get("date_from"))
        end = _parse_period_date(row.get("date_to"))
        if start is None or end is None or end < start:
            continue
        if (end - start).days > MAX_COVERAGE_DAYS:
            continue
        known = True
        days.update(pd.date_range(start, end, freq="D"))
    return days if known else None


def _bucket_end(bucket_start: pd.Timestamp, granularity: str) -> pd.Timestamp:
    if granularity == "week":
        return bucket_start + pd.Timedelta(days=6)
    if granularity == "month":
        return bucket_start + pd.offsets.MonthEnd(0)
    return bucket_start


def _bucket_label(
    bucket_start: pd.Timestamp,
    granularity: str,
    *,
    actual_start: pd.Timestamp | None = None,
    actual_end: pd.Timestamp | None = None,
) -> str:
    """Подпись бакета. actual_start/actual_end — реальный охват датами внутри
    бакета; передаются, только когда он меньше календарной недели/месяца (см.
    _bucketed_metrics), и тогда подпись честно называет фактические границы,
    а не весь календарный срок, которого в выборке нет."""
    if granularity == "week":
        end = bucket_start + pd.Timedelta(days=6)
        if actual_start is not None and actual_end is not None:
            return (
                f"{actual_start.strftime('%d.%m')}–{actual_end.strftime('%d.%m')} "
                "(неполная неделя)"
            )
        return f"{bucket_start.strftime('%d.%m')}–{end.strftime('%d.%m')}"
    if granularity == "month":
        name = _MONTH_NAMES_RU.get(bucket_start.month, bucket_start.strftime("%m"))
        label = f"{name} {bucket_start.year}"
        if actual_start is not None and actual_end is not None:
            return (
                f"{label} (неполный месяц: {actual_start.strftime('%d.%m')}–"
                f"{actual_end.strftime('%d.%m')})"
            )
        return label
    return bucket_start.strftime("%d.%m")


def _bucketed_metrics(
    messages: pd.DataFrame,
    granularity: str,
    *,
    min_buckets: int = 1,
    coverage: set[pd.Timestamp] | None = None,
) -> list[dict[str, Any]]:
    bucket_start = _bucket_start(messages, granularity)
    if bucket_start is None:
        return []
    work = messages.copy()
    work["_bucket"] = bucket_start
    work = work.dropna(subset=["_bucket"])
    if work.empty:
        return []
    buckets = sorted(work["_bucket"].unique())
    if len(buckets) < min_buckets:
        return []

    # Неделя/месяц часто попадает в выборку не целиком: выгрузка начинается
    # в среду 23.04, а неделя формально с понедельника 21.04. Подпись
    # «21.04–27.04» тогда обещает полную неделю, которой в данных нет, а
    # сравнение с соседней полной неделей выглядит как провал или взлёт.
    #
    # Мерило — диапазоны выгрузок (coverage, см. period_coverage_days), а не
    # даты сообщений: тихое воскресенье внутри загруженной недели — это
    # реальный ноль, а не обрезанный край, и неделя остаётся полной. Зато
    # разрыв между двумя загрузками внутри недели (01–07.03 и 16–22.03)
    # виден и у внутренних недель. Без дат выгрузок остаётся запасное правило
    # по датам сообщений — и только для крайних недель/месяцев.
    day_dates = pd.to_datetime(work["datetime"], errors="coerce").dt.floor("D")
    result: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        mask = work["_bucket"] == bucket
        subset = work[mask]
        metrics = overview_metrics(subset)
        sent = metrics.get("sentiment", {})
        total = max(1, int(sent.get("total", 0) or 0))
        bucket_ts = pd.Timestamp(bucket)
        actual_start = actual_end = None
        if granularity in ("week", "month"):
            bucket_end = _bucket_end(bucket_ts, granularity)
            message_days = day_dates[mask.to_numpy()].dropna()
            if coverage is not None:
                bucket_days = pd.date_range(bucket_ts, bucket_end, freq="D")
                covered = [day for day in bucket_days if day in coverage]
                if not covered and not message_days.empty:
                    # Сообщения вне заявленного диапазона выгрузки: охват
                    # известен только по ним самим.
                    actual_start, actual_end = message_days.min(), message_days.max()
                elif covered and len(covered) < len(bucket_days):
                    actual_start, actual_end = covered[0], covered[-1]
            elif index in (0, len(buckets) - 1) and not message_days.empty:
                reaches_start = bool((message_days == bucket_ts).any())
                reaches_end = bool((message_days == bucket_end).any())
                if not (reaches_start and reaches_end):
                    actual_start, actual_end = message_days.min(), message_days.max()
        metrics.update(
            {
                "period_id": _bucket_id(bucket_ts, granularity),
                "label": _bucket_label(
                    bucket_ts,
                    granularity,
                    actual_start=actual_start,
                    actual_end=actual_end,
                ),
                "partial": actual_start is not None,
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


def daily_metrics_for_comparison(
    messages: pd.DataFrame, coverage: set[pd.Timestamp] | None = None
) -> list[dict[str, Any]]:
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
    Недельная/месячная разбивка (weekly_/monthly_metrics_for_comparison)
    следуют тому же контракту.

    coverage (дни выбранных выгрузок) для дней не нужен и принимается ради
    единой сигнатуры с неделями и месяцами.
    """
    return _bucketed_metrics(messages, "day", min_buckets=2)


def weekly_metrics_for_comparison(
    messages: pd.DataFrame, coverage: set[pd.Timestamp] | None = None
) -> list[dict[str, Any]]:
    """daily_metrics_for_comparison, но по неделям (пн-вс) - авто-группировка
    для длинных периодов, где день-в-день даёт слишком много точек."""
    return _bucketed_metrics(messages, "week", min_buckets=2, coverage=coverage)


def monthly_metrics_for_comparison(
    messages: pd.DataFrame, coverage: set[pd.Timestamp] | None = None
) -> list[dict[str, Any]]:
    """daily_metrics_for_comparison, но по календарным месяцам."""
    return _bucketed_metrics(messages, "month", min_buckets=2, coverage=coverage)


GRANULARITY_FUNCS = {
    "day": daily_metrics_for_comparison,
    "week": weekly_metrics_for_comparison,
    "month": monthly_metrics_for_comparison,
}


def available_buckets(
    messages: pd.DataFrame,
    granularity: str,
    coverage: set[pd.Timestamp] | None = None,
) -> list[dict[str, Any]]:
    """Список дней/недель/месяцев для пикера гранулярности - в отличие от
    *_metrics_for_comparison (которым для СРАВНЕНИЯ нужно минимум 2 точки),
    здесь достаточно одного бакета: показать в пикере "24.04 (12 сообщ.)"
    нужно, даже если в выборке всего один день."""
    if granularity not in GRANULARITY_FUNCS:
        return []
    return _bucketed_metrics(messages, granularity, min_buckets=1, coverage=coverage)


def unresolved_date_count(messages: pd.DataFrame) -> int:
    """Сколько сообщений не попадут ни в один день/неделю/месяц - у них не
    распозналась дата при импорте. daily_/weekly_/monthly_metrics_for_
    comparison и filter_messages_by_buckets молча их пропускают; это число -
    чтобы предупредить аналитика, а не тихо терять данные."""
    if (
        not isinstance(messages, pd.DataFrame)
        or messages.empty
        or "datetime" not in messages.columns
    ):
        return 0
    return int(pd.to_datetime(messages["datetime"], errors="coerce").isna().sum())


def filter_messages_by_buckets(
    messages: pd.DataFrame, granularity: str, selected_bucket_ids: list[str] | None
) -> pd.DataFrame:
    """Сузить сообщения до выбранных дней/недель/месяцев по СОБСТВЕННОЙ дате
    каждого сообщения - независимо от того, каким файлом/периодом оно было
    загружено. Один файл на 15 дней дробится ровно так же, как пять файлов
    по 3 дня - фильтр не знает и не спрашивает, откуда пришли сообщения.

    granularity="period" (гранулярность "Файлы целиком") или пустой выбор
    бакетов - сообщения не сужаются: иначе "ничего не выбрано" молча дал бы
    пустой дашборд вместо всей выборки. Выбор всех бакетов тоже не сужает:
    сообщения без даты остаются в итогах."""
    if granularity not in GRANULARITY_FUNCS or not selected_bucket_ids:
        return messages
    bucket_start = _bucket_start(messages, granularity)
    if bucket_start is None:
        return messages
    valid = bucket_start.notna()
    ids = pd.Series(pd.NA, index=messages.index, dtype="object")
    ids[valid] = [
        _bucket_id(pd.Timestamp(ts), granularity) for ts in bucket_start[valid]
    ]
    selected = {str(x) for x in selected_bucket_ids}
    # Отмечены все дни/недели/месяцы выборки — это не сужение, а вся выборка.
    # Сообщения без распознанной даты в разбивку не попадают, но из итогов
    # выпадать не должны: гранулярность по умолчанию («День», отмечено всё)
    # иначе молча убирала их со всего дашборда, включая индексы бренда.
    if set(ids[valid]) <= selected:
        return messages
    return messages[ids.isin(selected)]


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

    granularity="day"/"week"/"month" — точки по календарным дням/неделям/
    месяцам внутри выбранных периодов (см. GRANULARITY_FUNCS), с откатом на
    period_metrics_for_comparison, если точек меньше двух (например, дата
    не распозналась при импорте, или сообщения из одного дня). По
    умолчанию — «period»: раздел «Отчёт» и любой другой вызов без явной
    гранулярности сравнивает загруженные периоды целиком, а не дробит их.
    """
    func = GRANULARITY_FUNCS.get(granularity)
    coverage = period_coverage_days(periods, period_ids)
    comparison = func(messages, coverage=coverage) if func else []
    if len(comparison) < 2:
        # Откат на периоды целиком — только по тем, у которых в сообщениях
        # что-то осталось. Если гранулярность оставила дни одного периода,
        # второй посчитался бы по нулю сообщений, и в саммари и отчёте
        # появлялось «было 0, стало 3» — период не опустел, он выпал из выбора.
        present_ids = list(period_ids or [])
        if (
            isinstance(messages, pd.DataFrame)
            and not messages.empty
            and "period_id" in messages.columns
        ):
            with_messages = set(messages["period_id"].astype(str))
            present_ids = [pid for pid in present_ids if str(pid) in with_messages]
        comparison = period_metrics_for_comparison(messages, periods, present_ids)
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
    """Изменение доли в процентных пунктах.

    Запятая ставится только в числе: точки в «п.п.» — часть единицы измерения,
    и замена по всей строке превращала бы их в «п,п,». Этот разделитель был
    последним местом, где число писалось с точкой, — рядом на том же экране
    карточки индексов бренда показывали запятую.
    """
    diff = (float(current_share or 0) - float(previous_share or 0)) * 100
    sign = "+" if diff > 0 else ""
    return f"{sign}{diff:.1f}".replace(".", ",") + " п.п."


def comparison_row(
    metric: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    sent = metric.get("sentiment", {}) or {}
    # Без разметки тональности доли — не измерение, а «всё в нейтрале»: прочерк.
    # Изменение долей — только если размечены оба соседних периода.
    unmarked = sentiment_unmarked(sent)
    no_tone_delta = (
        previous is None
        or unmarked
        or sentiment_unmarked(previous.get("sentiment"))
    )

    def _share(key: str) -> str:
        return "—" if unmarked else percent_text(sent.get(key, 0), sent.get("total", 0))

    def _share_delta(key: str) -> str:
        if no_tone_delta:
            return "—"
        return pp_delta(metric.get(f"{key}_share", 0), previous.get(f"{key}_share", 0))

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
        "Позитив": _share("positive"),
        "Δ позитива": _share_delta("positive"),
        "Нейтрал": _share("neutral"),
        "Δ нейтрала": _share_delta("neutral"),
        "Негатив": _share("negative"),
        "Δ негатива": _share_delta("negative"),
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
                # Числа не трогаем (NaN дал бы «nan%» в подписях): точки без
                # разметки убирает с графиков тональности сам экран. Точка без
                # единого сообщения — тоже не «размечена»: sentiment_unmarked
                # сама по себе считает пустой период измеренным нулём (это
                # верно для карточек, где 0 периода — законный ноль), но доля
                # 0/0 для круговой и линии тональности была бы выдуманной
                # «Позитив 0 %, Нейтрал 0 %», а не честным «данных нет».
                "Тональность размечена": bool(int(sent.get("total", 0) or 0))
                and not sentiment_unmarked(sent),
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
