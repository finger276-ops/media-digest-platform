# -*- coding: utf-8 -*-
"""Общие хелперы форматирования, используемые в нескольких разделах UI."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}")


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
        # «День первым» — для русских дат вида 05.04.2026. ISO-строку
        # (2026-04-05, как её отдаёт база) pandas 3 с dayfirst=True читает
        # как 4 мая, поэтому её разбираем без этого флага.
        dayfirst = not (isinstance(value, str) and _ISO_DATE_RE.match(value.strip()))
        ts = pd.to_datetime(value, errors="coerce", dayfirst=dayfirst)
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


# Внутренние коды платформы — в базе и в коде; человеку показывается подпись.
# Один словарь на всю платформу: раньше подписи форматов были скопированы в
# три страницы, а там, где их не было, аналитик видел «mediologia_excel».
SOURCE_SYSTEM_LABELS = {
    "auto": "Автоопределение",
    "mediologia": "Медиалогия CSV",
    "mediologia_excel": "Медиалогия Excel",
    "brand_analytics": "Brand Analytics",
    "generic": "Универсальный CSV/Excel",
}

STATUS_TITLES = {
    "active": "Активен",
    "hidden": "Скрыт",
    "archived": "В архиве",
}


def source_system_title(value: Any) -> str:
    code = str(value or "").strip()
    return SOURCE_SYSTEM_LABELS.get(code.lower(), code)


def status_title(value: Any) -> str:
    code = str(value or "").strip() or "active"
    return STATUS_TITLES.get(code.lower(), code)


# Транслитерация для имён скачиваемых файлов. Кириллическое имя браузер
# получает в заголовке как filename*=utf-8''…, и кнопка скачивания Streamlit
# такое имя не подхватывает: файл «ТЕХНОНИКОЛЬ_апрель.docx» сохранялся как
# «download» без расширения. Латиница доходит как есть.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def ascii_filename(*parts: Any, ext: str, fallback: str = "file") -> str:
    """Имя файла латиницей: «Кнауф», «01.04–07.04» → «Knauf_01.04_07.04.docx»."""
    raw = "_".join(str(part) for part in parts if str(part or "").strip())
    latin = []
    for index, char in enumerate(raw):
        lower = char.lower()
        if lower not in _TRANSLIT:
            latin.append(char)
            continue
        mapped = _TRANSLIT[lower]
        if char == lower:
            latin.append(mapped)
            continue
        # Слово заглавными («ТЕХНОНИКОЛЬ») — «TEKHNONIKOL», а не «TEKhNONIKOL».
        neighbours = raw[max(0, index - 1) : index] + raw[index + 1 : index + 2]
        caps_word = any(n.isalpha() and n.isupper() for n in neighbours)
        latin.append(mapped.upper() if caps_word else mapped.capitalize())
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", "".join(latin)).strip("_.")
    return f"{safe[:140] or fallback}.{ext}"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Форма слова по числу: 1 регион, 2 региона, 5 регионов, 11 регионов, 21 регион."""
    n = abs(int(count or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
