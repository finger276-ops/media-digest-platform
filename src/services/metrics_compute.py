from __future__ import annotations

from typing import Any

import pandas as pd


def numeric_series(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    """Return a parsed numeric series from the first useful existing column.

    If a processed metric column exists but contains only zeros while a raw
    alias is also present, try the raw alias before giving up. This helps with
    older uploaded periods and mixed Brand Analytics exports.
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)

    # Prefer pre-parsed dashboard columns when available. This avoids reparsing
    # audience/reach/engagement on every rerun and every chart/table render.
    precomputed_map = {
        "_audience": {"audience", "Аудитория"},
        "_reach": {"views", "Просмотры", "Просмотров", "reach", "Охват"},
        "_engagement": {"engagement", "Вовлечённость", "Вовлеченность", "engagement_count"},
    }
    requested = set(columns or [])
    for pre_col, aliases in precomputed_map.items():
        if pre_col in df.columns and requested & aliases:
            return pd.to_numeric(df[pre_col], errors="coerce").fillna(0)

    fallback = pd.Series([0] * len(df), index=df.index, dtype=float)

    for col in columns:
        if col not in df.columns:
            continue
        series = (
            df[col]
            .fillna("")
            .astype(str)
            .str.replace("\ufeff", "", regex=False)
            .str.replace("\u00a0", "", regex=False)
            .str.replace("\u202f", "", regex=False)
            .str.replace(" ", "", regex=False)
            .str.replace("\t", "", regex=False)
            .str.replace(r"[^0-9\-]", "", regex=True)
            .pipe(pd.to_numeric, errors="coerce")
            .fillna(0)
        )
        # Use the first column with a non-zero value. Keep a zero fallback in
        # case all aliases are empty or genuinely zero.
        if float(series.sum()) != 0:
            return series
        fallback = series

    return fallback


def prepare_dashboard_messages(messages: pd.DataFrame) -> pd.DataFrame:
    """Add reusable normalized columns for dashboard calculations."""
    if messages is None or messages.empty:
        return messages
    work = messages.copy()
    if "_audience" not in work.columns:
        work["_audience"] = numeric_series(work, ["audience", "Аудитория"]).astype(int)
    if "_reach" not in work.columns:
        work["_reach"] = numeric_series(work, ["views", "Просмотры", "Просмотров", "reach", "Охват"]).astype(int)
    if "_engagement" not in work.columns:
        work["_engagement"] = numeric_series(work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]).astype(int)
    if "_sentiment_lower" not in work.columns:
        work["_sentiment_lower"] = work.get("sentiment", pd.Series([""] * len(work), index=work.index)).fillna("").astype(str).str.lower().str.replace("ё", "е", regex=False)
    if "_is_negative_bool" not in work.columns:
        work["_is_negative_bool"] = work["_sentiment_lower"].str.contains("нег|negative|отриц", regex=True, na=False)
        if "is_negative" in work.columns:
            work["_is_negative_bool"] = work["_is_negative_bool"] | work["is_negative"].astype(str).str.lower().isin(["true", "1", "yes", "да", "негатив", "negative"])
    if "_period_id_str" not in work.columns and "period_id" in work.columns:
        work["_period_id_str"] = work["period_id"].astype(str)
    return work


def format_int(value: Any) -> str:
    try:
        return f"{int(float(value)):,}".replace(",", " ")
    except Exception:
        return "0"


def sentiment_counts(messages: pd.DataFrame) -> dict[str, int]:
    """Return positive/neutral/negative counts for any project profile."""
    total = int(len(messages)) if isinstance(messages, pd.DataFrame) else 0
    if total == 0:
        return {"positive": 0, "neutral": 0, "negative": 0, "total": 0}

    if "_sentiment_lower" in messages.columns:
        sentiment = messages["_sentiment_lower"].fillna("").astype(str)
    else:
        sentiment = messages.get("sentiment", pd.Series([""] * total, index=messages.index)).fillna("").astype(str).str.lower().str.replace("ё", "е", regex=False)
    positive_mask = sentiment.str.contains("позит|positive|полож", regex=True, na=False)
    negative_mask = sentiment.str.contains("нег|negative|отриц", regex=True, na=False)
    neutral_mask = sentiment.str.contains("нейтр|neutral", regex=True, na=False)

    if "_is_negative_bool" in messages.columns:
        negative_mask = messages["_is_negative_bool"].astype(bool)
    elif "is_negative" in messages.columns:
        negative_mask = negative_mask | messages["is_negative"].astype(str).str.lower().isin(["true", "1", "yes", "да", "негатив", "negative"])

    positive = int(positive_mask.sum())
    negative = int(negative_mask.sum())
    neutral_detected = int(neutral_mask.sum())
    neutral = max(0, total - positive - negative)
    if neutral_detected and neutral_detected > neutral:
        neutral = neutral_detected
        # Keep total stable if imported data has overlapping/dirty sentiment values.
        overflow = positive + negative + neutral - total
        if overflow > 0:
            neutral = max(0, neutral - overflow)
    return {"positive": positive, "neutral": neutral, "negative": negative, "total": total}


def percent_text(count: int, total: int) -> str:
    return f"{count / total * 100:.0f}%" if total else "0%"


def overview_metrics(messages: pd.DataFrame) -> dict[str, Any]:
    total_messages = int(len(messages)) if isinstance(messages, pd.DataFrame) else 0
    return {
        "messages": total_messages,
        "audience": int(numeric_series(messages, ["audience", "Аудитория"]).sum()) if total_messages else 0,
        "reach": int(numeric_series(messages, ["views", "Просмотры", "Просмотров", "reach", "Охват"]).sum()) if total_messages else 0,
        "engagement": int(numeric_series(messages, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]).sum()) if total_messages else 0,
        "sentiment": sentiment_counts(messages),
    }
