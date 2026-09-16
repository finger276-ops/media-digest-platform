"""Базовая чистка текста, дат и коротких строковых полей выгрузки.

Вынесено из preprocess.py при распиле монолита (2237 строк): это фундамент,
на который опираются все остальные части конвейера обработки — ни от чего
внутри preprocess.py эти функции сами не зависят.
"""

from __future__ import annotations

import hashlib
import re

import pandas as pd

from settings import TEXT_PREFIXES_TO_REMOVE


def stable_hash(value: str, prefix: str = "") -> str:
    digest = hashlib.md5(str(value).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}{digest}" if prefix else digest


def normalize_spaces(value: str) -> str:
    value = "" if value is None else str(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def get_text_series(
    df: pd.DataFrame, column: str, default: str = "", aliases: list[str] | None = None
) -> pd.Series:
    """Return a string Series with the same index as df.

    pandas.DataFrame.get(..., "") returns a scalar string when a column is
    missing. Iterating over that scalar caused empty lists in zip(...) and then
    crashes like: Length of values (0) does not match length of index.

    The helper also supports common aliases because exports from different
    periods may have slightly different column names.
    """
    candidates = [column] + list(aliases or [])
    for candidate in candidates:
        if candidate in df.columns:
            return df[candidate].fillna("").astype(str)
    return pd.Series([default] * len(df), index=df.index, dtype="object")


def pick_first_non_empty(*values: str, fallback: str = "") -> str:
    for value in values:
        value = normalize_spaces(value)
        if value:
            return value
    return fallback


def chat_key_from_link(link: str) -> str:
    """Best-effort chat key from Telegram/message links when chat columns differ."""
    link = normalize_spaces(link)
    if not link:
        return ""
    match = re.search(
        r"(?:https?://)?(?:t\.me|telegram\.me)/([^/\s]+)", link, flags=re.IGNORECASE
    )
    if match:
        return match.group(1)
    return re.sub(r"/\d+(?:[?#].*)?$", "", link)


def clean_text(message: str, recognized: str) -> tuple[str, str]:
    message = normalize_spaces(message)
    recognized = normalize_spaces(recognized)

    if message:
        text = message
        source = "message"
    else:
        text = recognized
        source = "recognized"

    for prefix in TEXT_PREFIXES_TO_REMOVE:
        text = text.replace(prefix, "")
    text = text.replace("_x000D_", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = normalize_spaces(text)

    if recognized.startswith("Тексты с изображений"):
        source = "image_ocr" if not message else "message_plus_image_ocr"
    elif recognized.startswith("Расшифровки"):
        source = "transcript" if not message else "message_plus_transcript"

    return text, source


def parse_datetime(series: pd.Series) -> pd.Series:
    prepared = (
        series.fillna("").astype(str).str.replace("\xa0", " ", regex=False).str.strip()
    )
    return pd.to_datetime(prepared, errors="coerce", dayfirst=True)
