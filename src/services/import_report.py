"""Что платформа прочитала из выгрузки, а что не поняла.

Синонимов имён колонок не хватит никогда. Разбор четырёх реальных выгрузок —
Brand Analytics и Медиалогии, каждая в csv и xlsx — показал, что у одной и той
же системы колонки называются по-разному в разных форматах, а разные системы
называют одно и то же так, что заранее не угадать: «Аудитория» против
«Аудитории блога», «Оценка» против «Оценки от 1 до 5», «Просмотры» против
«Просмотров».

Пока платформа молчит о непонятых колонках, каждая такая потеря обнаруживается
случайно и спустя месяцы. Аудитория у проектов на Медиалогии была нулевой ровно
поэтому. Отчёт делает потерю видимой в момент загрузки: добавить синоним —
минутное дело, найти причину задним числом — расследование.

Отчёт строится из того, что произошло при разборе, а не из отдельного списка
синонимов: список пришлось бы держать рядом с шестью десятками вызовов и
следить, чтобы он не разошёлся с ними, а разошедшийся список врёт.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

# Колонки, которые незачем показывать как непонятые: платформа добавляет их
# сама либо использует не как данные. «Обработано» — маркер начала тегов: по
# нему платформа находит колонки-теги, а само значение ей не нужно.
SERVICE_COLUMNS = frozenset(
    {"source_file", "source_tag_columns", "№", "Обработано", "Processed"}
)

NORMALIZATION_LABELS = {
    "html_entities": "расшифровано HTML-мнемоник",
    "excel_cr": "убрано экранированных переводов строки (_x000d_)",
    "line_endings": "приведено переводов строки",
    "grouped_numbers": "склеено чисел с пробелом в разрядах",
}


def _filled(values: pd.Series) -> int:
    text = values.fillna("").astype(str).str.strip()
    return int((text != "").sum())


def build_import_report(
    raw: pd.DataFrame,
    *,
    consumed: set[str],
    normalized: dict[str, int],
    detected_system: str = "",
    tag_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Сводка разбора: что прочитано, что осталось непонятым."""
    rows = int(len(raw)) if raw is not None else 0
    tag_columns = list(tag_columns or [])
    used = {str(c) for c in consumed} | {str(c) for c in tag_columns}

    recognized: list[str] = []
    unrecognized: list[dict[str, Any]] = []
    empty: list[str] = []
    for column in (raw.columns if raw is not None else []):
        name = str(column)
        if name in SERVICE_COLUMNS or name.lower().startswith("unnamed"):
            continue
        count = _filled(raw[column])
        if name in used:
            recognized.append(name)
        elif count:
            unrecognized.append(
                {
                    "column": name,
                    "filled": count,
                    "share": count / rows if rows else 0.0,
                    "sample": _sample(raw[column]),
                }
            )
        else:
            # Колонка есть, но в ней ничего нет: сообщать о ней как о потере
            # значило бы поднимать тревогу на пустом месте.
            empty.append(name)

    unrecognized.sort(key=lambda item: -item["filled"])
    return {
        "rows": rows,
        "source_columns": int(len(raw.columns)) if raw is not None else 0,
        "detected_system": str(detected_system or ""),
        "recognized": recognized,
        "unrecognized": unrecognized,
        "empty": empty,
        "tag_columns": tag_columns,
        "normalized": dict(normalized or {}),
    }


def _sample(values: pd.Series, limit: int = 2) -> str:
    text = values.fillna("").astype(str).str.strip()
    found = [v for v in dict.fromkeys(text[text != ""]) if v][:limit]
    return " · ".join(v[:40] for v in found)


def summarize_import(report: dict[str, Any]) -> str:
    """Одна строка для журнала и для шапки блока."""
    if not report:
        return ""
    total = int(report.get("source_columns", 0))
    known = len(report.get("recognized", []))
    unknown = len(report.get("unrecognized", []))
    parts = [
        f"{report.get('rows', 0)} строк",
        f"{known} из {total} колонок распознано",
    ]
    if unknown:
        parts.append(f"{unknown} не распознано")
    system = report.get("detected_system")
    if system:
        parts.append(f"формат: {system}")
    return ", ".join(parts)


def normalization_lines(report: dict[str, Any]) -> list[str]:
    """Человеческое описание того, что пришлось выправить при чтении."""
    normalized = (report or {}).get("normalized") or {}
    lines = []
    for key, count in sorted(normalized.items(), key=lambda kv: -kv[1]):
        if count <= 0:
            continue
        label = NORMALIZATION_LABELS.get(key, key)
        lines.append(f"{label}: {count}")
    return lines
