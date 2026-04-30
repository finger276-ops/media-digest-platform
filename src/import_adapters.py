"""Import adapters for different monitoring-system exports.

The dashboard works with one canonical table that is close to the original
Mediologia CSV schema. This module reads CSV/XLSX files from different systems
and maps their columns to that canonical schema before preprocessing.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

CANONICAL_COLUMNS = [
    "№",
    "Дата",
    "Сообщение",
    "Автораспознанный текст",
    "Ссылка",
    "Площадка",
    "Автор",
    "Профиль автора",
    "Блог",
    "Профиль блога",
    "Тип",
    "Тональность",
    "Токсичность",
    "WOM",
    "Страна",
    "Регион",
    "Город",
    "Количество дублей",
    "Просмотры",
    "Вовлечённость",
    "Лайки",
    "Комментарии",
    "Репосты",
    "Текст родительского поста",
    "Ссылка на родительский пост",
    "Дата публикации родительского поста",
    "Теги",
    "Категории",
    "Сюжет",
    "Id сообщения",
    "Основная тема",
    "Все темы",
    "Все темы (список)",
    "Релевантное",
    "source_system",
    "source_file",
]

MEDIALOGIA_DEFAULT_TAGS = [
    "Коэффициент",
    "Законы и налоги",
    "яндекс",
    "WB Такси",
    "Фастен",
    "Приложение и сбои",
    "Яндекс Про",
    "Забастовка",
]


def _clean_col_name(value: object) -> str:
    value = "" if value is None else str(value)
    value = value.replace("\ufeff", "").replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if value.lower().startswith("unnamed"):
        return ""
    return value


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [_clean_col_name(c) for c in df.columns]
    df = df.loc[:, [bool(str(c).strip()) for c in df.columns]]
    df = df.dropna(how="all")
    for col in df.columns:
        df[col] = df[col].fillna("").astype(str)
    df = df.loc[~df.apply(lambda r: all(str(v).strip() == "" for v in r), axis=1)].reset_index(drop=True)
    return df


def _sniff_delimiter(line: str) -> str:
    candidates = [";", ",", "\t"]
    counts = {sep: line.count(sep) for sep in candidates}
    return max(counts, key=counts.get) if max(counts.values()) else ";"


def _find_csv_header(path: Path) -> tuple[int, str]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        lines = [f.readline() for _ in range(50)]
    best_idx = 0
    best_score = -1
    best_sep = ";"
    header_tokens = [
        "дата",
        "текст",
        "сообщение",
        "id сообщения",
        "url",
        "ссылка",
        "источник",
        "автор",
        "тональность",
        "блог",
        "площадка",
        "основная тема",
        "все темы",
        "релевантное",
    ]
    for idx, line in enumerate(lines):
        if not line:
            continue
        sep = _sniff_delimiter(line)
        parts = [p.strip().strip('"').lower() for p in line.split(sep)]
        score = sum(any(token in part for part in parts) for token in header_tokens)
        if score > best_score and len(parts) >= 4:
            best_idx = idx
            best_score = score
            best_sep = sep
    return best_idx, best_sep


def _read_csv_any(path: Path) -> pd.DataFrame:
    header_idx, sep = _find_csv_header(path)
    try:
        df = pd.read_csv(
            path,
            sep=sep,
            encoding="utf-8-sig",
            skiprows=header_idx,
            dtype=str,
            keep_default_na=False,
            engine="python",
            quoting=csv.QUOTE_MINIMAL,
            on_bad_lines="warn",
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            path,
            sep=sep,
            encoding="cp1251",
            skiprows=header_idx,
            dtype=str,
            keep_default_na=False,
            engine="python",
            quoting=csv.QUOTE_MINIMAL,
            on_bad_lines="warn",
        )
    return _clean_dataframe(df)


EXCEL_HEADER_TOKENS = [
    "дата",
    "время",
    "время публикации",
    "текст",
    "текст сообщения",
    "сообщение",
    "hash сообщения",
    "id сообщения",
    "url",
    "ссылка",
    "ссылка на сообщение",
    "источник",
    "тип источника",
    "заголовок",
    "автор",
    "url автора",
    "кто пишет",
    "где пишет",
    "место публикации",
    "url места публикации",
    "тональность",
    "сюжет",
    "теги",
    "основная тема",
    "все темы",
    "релевантное",
]


def _excel_header_score(values: list[str]) -> int:
    """Score a potential Excel header row by known export column names."""
    normalized = [_clean_col_name(v).lower() for v in values]
    normalized = [v for v in normalized if v]
    if not normalized:
        return -1

    score = sum(any(token in value for value in normalized) for token in EXCEL_HEADER_TOKENS)

    # Strong signals for Brand Analytics / monitoring-system message exports.
    strong_columns = {
        "hash сообщения",
        "id сообщения",
        "текст",
        "источник",
        "url",
        "тип источника",
        "место публикации",
        "url места публикации",
    }
    score += 2 * sum(1 for col in normalized if col in strong_columns)

    # A valid message export usually has many real columns; tiny summary rows should lose.
    if len(normalized) >= 8:
        score += 2
    if len(normalized) >= 15:
        score += 2
    return score


def _find_excel_header(frame: pd.DataFrame) -> int:
    if frame is None or frame.empty:
        return 0

    best_idx = 0
    best_score = -1
    scan = frame.head(60).fillna("").astype(str)
    for idx, row in scan.iterrows():
        values = [_clean_col_name(v) for v in row.tolist()]
        non_empty = sum(1 for value in values if value)
        if non_empty < 4:
            continue
        score = _excel_header_score(values)
        if score > best_score:
            best_idx = int(idx)
            best_score = score
    return best_idx


def _safe_excel_preview(path: Path, sheet_name: str | int, nrows: int = 60) -> pd.DataFrame:
    try:
        preview = pd.read_excel(
            path,
            sheet_name=sheet_name,
            header=None,
            dtype=str,
            nrows=nrows,
            keep_default_na=False,
        )
    except Exception:
        return pd.DataFrame()
    if preview is None or preview.empty:
        return pd.DataFrame()
    preview = preview.dropna(how="all")
    if preview.empty:
        return pd.DataFrame()
    return preview


def _sheet_name_bonus(sheet_name: str | int) -> int:
    name = str(sheet_name or "").strip().lower().replace("ё", "е")
    if name in {"сообщения", "messages", "публикации", "posts"}:
        return 100
    if "сообщ" in name or "message" in name or "публикац" in name:
        return 40
    if name in {"лист1", "sheet1"}:
        return -20
    return 0


def _select_excel_sheet_and_header(path: Path, sheet_name: str | int | None = None) -> tuple[str | int, int]:
    xls = pd.ExcelFile(path)
    sheets = xls.sheet_names
    if not sheets:
        raise ValueError("В Excel-файле не найдено листов.")

    if sheet_name is not None:
        preview = _safe_excel_preview(path, sheet_name)
        if preview.empty:
            raise ValueError(f"Выбранный лист Excel пустой или не содержит таблицы: {sheet_name}")
        header_idx = _find_excel_header(preview)
        if header_idx < 0 or header_idx >= len(preview):
            header_idx = 0
        return sheet_name, header_idx

    candidates: list[tuple[int, str | int, int, int]] = []
    skipped_empty: list[str] = []

    for candidate in sheets:
        preview = _safe_excel_preview(path, candidate)
        if preview.empty:
            skipped_empty.append(str(candidate))
            continue

        header_idx = _find_excel_header(preview)
        if header_idx < 0 or header_idx >= len(preview):
            header_idx = 0

        row_values = [_clean_col_name(v) for v in preview.iloc[header_idx].fillna("").astype(str).tolist()]
        score = _excel_header_score(row_values) + _sheet_name_bonus(candidate)
        candidates.append((score, candidate, header_idx, len(preview)))

    if not candidates:
        skipped = ", ".join(skipped_empty) if skipped_empty else "нет доступных листов"
        raise ValueError(
            "Не удалось найти непустой лист с таблицей сообщений. "
            f"Пустые/неподходящие листы: {skipped}."
        )

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, selected_sheet, header_idx, _ = candidates[0]
    if best_score < 3:
        sheet_list = ", ".join(str(s) for s in sheets)
        raise ValueError(
            "Не удалось определить лист с сообщениями. "
            "Нужен лист с колонками вроде: Дата, Время, Текст, Url/Ссылка, Автор, Источник. "
            f"Листы в файле: {sheet_list}."
        )
    return selected_sheet, header_idx


def _read_excel_any(path: Path, sheet_name: str | int | None = None) -> pd.DataFrame:
    selected_sheet, header_idx = _select_excel_sheet_and_header(path, sheet_name=sheet_name)
    df = pd.read_excel(
        path,
        sheet_name=selected_sheet,
        header=header_idx,
        dtype=str,
        keep_default_na=False,
    )
    cleaned = _clean_dataframe(df)
    if cleaned.empty:
        raise ValueError(f"На листе Excel «{selected_sheet}» не найдено строк с сообщениями.")
    return cleaned

def detect_source_system(df: pd.DataFrame) -> str:
    cols = {str(c).strip().lower() for c in df.columns}
    if {"hash сообщения", "источник", "url", "тип источника"} & cols and "id сообщения" in cols:
        return "brand_analytics"
    if "кто пишет" in cols or "где пишет" in cols or "время публикации" in cols:
        return "mediologia_excel"
    if "автораспознанный текст" in cols or "профиль блога" in cols or "блог" in cols:
        return "mediologia"
    return "generic"


def first_existing(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lower_map:
            return df[lower_map[key]].fillna("").astype(str)
    return pd.Series([""] * len(df), index=df.index, dtype="object")


def _join_text_parts(*parts: pd.Series) -> pd.Series:
    result = pd.Series([""] * len(parts[0]), index=parts[0].index, dtype="object") if parts else pd.Series(dtype="object")
    for part in parts:
        result = ["\n".join([x for x in [str(a).strip(), str(b).strip()] if x]) for a, b in zip(result, part.fillna("").astype(str))]
        result = pd.Series(result, index=part.index, dtype="object")
    return result


def _normalize_sentiment(series: pd.Series) -> pd.Series:
    def convert(value: object) -> str:
        s = str(value or "").strip().lower().replace("ё", "е")
        if not s:
            return ""
        if "нег" in s or s in {"negative", "-", "минус"}:
            return "негативная"
        if "позит" in s or "полож" in s or s in {"positive", "+", "плюс"}:
            return "позитивная"
        if "нейтр" in s or s in {"neutral", "0"}:
            return "нейтральная"
        return str(value).strip()
    return series.apply(convert)



def _normalize_topics_list(value: object) -> str:
    """Normalize list-like topic values to semicolon-separated text."""
    s = "" if value is None else str(value).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return ""
    s = s.replace("\ufeff", "").replace("\xa0", " ")
    # Values may arrive as: ['A', 'B'], A; B, A|B, or multiline.
    s = s.strip("[]")
    s = s.replace("'", "").replace('"', "")
    parts = re.split(r"\s*[;|,\n]\s*", s)
    seen = []
    for part in parts:
        part = re.sub(r"\s+", " ", str(part).strip())
        if part and part not in seen:
            seen.append(part)
    return "; ".join(seen)


def _normalize_bool_text(value: object) -> str:
    s = "" if value is None else str(value).strip().lower().replace("ё", "е")
    if s in {"true", "1", "да", "yes", "+", "истина", "верно"}:
        return "True"
    if s in {"false", "0", "нет", "no", "-", "ложь", "неверно"}:
        return "False"
    return str(value).strip() if value is not None else ""


def _combine_date_time_series(date_series: pd.Series, time_series: pd.Series) -> pd.Series:
    """Combine separate Date and Time columns from monitoring-system Excel exports."""
    result = []
    for date_value, time_value in zip(date_series.fillna("").astype(str), time_series.fillna("").astype(str)):
        d = re.sub(r"\s+", " ", str(date_value or "").strip())
        t = re.sub(r"\s+", " ", str(time_value or "").strip())
        if not d:
            result.append(t)
            continue
        if not t or t.lower() in {"nan", "none", "nat", "null"}:
            result.append(d)
            continue
        # Do not duplicate time when the date column already contains it.
        if re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", d):
            result.append(d)
        else:
            result.append(f"{d} {t}")
    return pd.Series(result, index=date_series.index, dtype="object")


def canonicalize_table(raw: pd.DataFrame, source_file: str = "", source_system: str = "auto") -> pd.DataFrame:
    df = _clean_dataframe(raw)
    detected = detect_source_system(df) if source_system in {"", "auto", None} else str(source_system)
    out = pd.DataFrame(index=df.index)

    out["№"] = first_existing(df, ["№", "N", "ID", "ID сообщения", "Hash сообщения"])
    date_series = first_existing(df, ["Дата", "Время публикации", "Дата публикации", "Дата сообщения", "Date", "Published"])
    time_series = first_existing(df, ["Время", "Time", "Published time", "Publication time"])
    out["Дата"] = _combine_date_time_series(date_series, time_series)

    message = first_existing(df, ["Сообщение", "Текст сообщения", "Текст", "Message", "Text", "Содержание"])
    recognized = first_existing(df, ["Автораспознанный текст", "Распознанный текст", "OCR", "Расшифровка"])
    title = first_existing(df, ["Заголовок", "Title"])
    if detected == "brand_analytics":
        out["Сообщение"] = _join_text_parts(title, message)
    else:
        out["Сообщение"] = message
    out["Автораспознанный текст"] = recognized

    out["Ссылка"] = first_existing(df, ["Ссылка", "Ссылка на сообщение", "Url", "URL", "url", "Link"])
    out["Площадка"] = first_existing(df, ["Площадка", "Источник", "Система", "Source"])
    out["Автор"] = first_existing(df, ["Автор", "Кто пишет", "Author", "Пользователь", "User"])
    out["Профиль автора"] = first_existing(df, ["Профиль автора", "Ссылка на автора", "Url автора", "URL автора", "Author URL"])

    blog = first_existing(df, ["Блог", "Где пишет", "Место публикации", "Источник", "Канал", "Группа", "Чат"])
    blog_profile = first_existing(df, ["Профиль блога", "Ссылка на блог", "Url места публикации", "URL места публикации", "Url источника"])
    out["Блог"] = blog
    out["Профиль блога"] = blog_profile

    out["Тип"] = first_existing(df, ["Тип", "Тип сообщения", "Message type"])
    out["Тональность"] = _normalize_sentiment(first_existing(df, ["Тональность", "Sentiment", "Окраска", "Тон"] ))
    out["Токсичность"] = first_existing(df, ["Токсичность", "Агрессия", "Toxicity", "Aggression"])
    out["WOM"] = first_existing(df, ["WOM", "Мнения"])
    out["Страна"] = first_existing(df, ["Страна", "Country"])
    out["Регион"] = first_existing(df, ["Регион", "Region"])
    out["Город"] = first_existing(df, ["Город", "City"])
    out["Количество дублей"] = first_existing(df, ["Количество дублей", "Дублей", "Duplicates"])
    out["Просмотры"] = first_existing(df, ["Просмотры", "Views"])
    out["Вовлечённость"] = first_existing(df, ["Вовлечённость", "Вовлеченность", "Engagement"])
    out["Лайки"] = first_existing(df, ["Лайки", "Likes"])
    out["Комментарии"] = first_existing(df, ["Комментарии", "Комментариев", "Comments"])
    out["Репосты"] = first_existing(df, ["Репосты", "Reposts", "Shares"])
    out["Текст родительского поста"] = first_existing(df, ["Текст родительского поста", "Родительский пост", "Parent text"])
    out["Ссылка на родительский пост"] = first_existing(df, ["Ссылка на родительский пост", "Parent URL", "Parent link"])
    out["Дата публикации родительского поста"] = first_existing(df, ["Дата публикации родительского поста", "Parent date"])

    out["Теги"] = first_existing(df, ["Теги", "Tags", "Метки", "Tag"])
    out["Категории"] = first_existing(df, ["Категории", "Category", "Categories"])
    out["Сюжет"] = first_existing(df, ["Сюжет", "Topic", "Theme", "Тема"])
    out["Id сообщения"] = first_existing(df, ["Id сообщения", "ID сообщения", "message_id", "id", "Hash сообщения"])

    # Optional human/topic markup columns. If present, they become a top-level
    # boundary for clustering, but do not replace information events.
    main_topic = first_existing(df, ["Основная тема", "Главная тема", "Main topic", "Primary topic", "Topic main"])
    all_topics_raw = first_existing(df, ["Все темы", "Темы", "Topics", "All topics"])
    all_topics_list = first_existing(df, ["Все темы (список)", "Список тем", "Topics list", "All topics list"])
    relevant = first_existing(df, ["Релевантное", "Релевантность", "Relevant", "Is relevant"])

    out["Основная тема"] = main_topic.apply(lambda x: re.sub(r"\s+", " ", str(x).strip()))
    out["Все темы"] = all_topics_raw.apply(_normalize_topics_list)
    out["Все темы (список)"] = all_topics_list.apply(_normalize_topics_list)
    out.loc[out["Все темы (список)"].str.strip() == "", "Все темы (список)"] = out.loc[
        out["Все темы (список)"].str.strip() == "", "Все темы"
    ]
    out["Релевантное"] = relevant.apply(_normalize_bool_text)

    for tag in MEDIALOGIA_DEFAULT_TAGS:
        if tag in df.columns:
            out[tag] = first_existing(df, [tag])

    out["source_system"] = detected
    out["source_file"] = Path(source_file).name if source_file else ""

    for col in CANONICAL_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    out = out[[c for c in CANONICAL_COLUMNS if c in out.columns] + [c for c in out.columns if c not in CANONICAL_COLUMNS]]
    out = _clean_dataframe(out)
    return out


def read_source_table(path: str | Path, source_system: str = "auto", sheet_name: str | int | None = None) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        raw = _read_excel_any(path, sheet_name=sheet_name)
    else:
        raw = _read_csv_any(path)
    return canonicalize_table(raw, source_file=str(path), source_system=source_system)


def get_excel_sheet_names(path: str | Path) -> list[str]:
    path = Path(path)
    if path.suffix.lower() not in {".xlsx", ".xls", ".xlsm"}:
        return []
    try:
        return pd.ExcelFile(path).sheet_names
    except Exception:
        return []
