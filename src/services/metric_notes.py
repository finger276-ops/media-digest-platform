"""Выводы аналитика по индексам бренда.

Метрика сама по себе заказчику ничего не говорит: «NSS +4,35%» — это число, а
не мысль. Что оно означает для бренда, зависит от рынка, от того, что
происходило в периоде, и от того, чего компания добивалась. Машине такой вывод
не составить, поэтому его пишет аналитик, а платформа хранит и подставляет в
отчёт.

Вывод привязан к периоду: одна и та же метрика в разные месяцы означает разное,
и прошлогодний комментарий к новому числу был бы хуже, чем его отсутствие.
"""

from __future__ import annotations

import pandas as pd

from services.cached_store import (
    UNCHECKED_VERSION,
    get_manual,
    list_manual,
    save_manual,
)

TABLE = "brand_metric_notes"
MAX_NOTE_LENGTH = 2000


def period_key(period_ids: list[str] | tuple[str, ...] | None) -> str:
    """Ключ набора периодов.

    Выводы пишутся к тому, что аналитик видит на экране. Если выбрано два
    периода, метрика посчитана по обоим — и вывод относится к этой паре, а не
    к каждому периоду по отдельности.
    """
    # Проверка на None до приведения к строке: str(None) даёт «None», и пустое
    # значение стало бы полноправной частью ключа.
    values = sorted(
        {str(x).strip() for x in (period_ids or []) if x is not None and str(x).strip()}
    )
    return "|".join(values)


def _row_key(metric_code: str, period_ids) -> str:
    return f"{TABLE}::{period_key(period_ids)}::{str(metric_code).strip()}"


def load_notes(project_id: str, period_ids) -> dict[str, str]:
    """Выводы по всем метрикам выбранного периода."""
    key = period_key(period_ids)
    if not project_id:
        return {}
    try:
        rows = list_manual(project_id, table_name=TABLE)
    except Exception:  # noqa: BLE001 — раздел работает и без сохранённых выводов
        return {}
    if rows is None or not isinstance(rows, pd.DataFrame) or rows.empty:
        return {}

    notes: dict[str, str] = {}
    for _, row in rows.iterrows():
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get("period_key", "")) != key:
            continue
        code = str(payload.get("metric") or "").strip()
        text = str(payload.get("note") or "").strip()
        if code and text:
            notes[code] = text
    return notes


def load_note_versions(project_id: str, period_ids) -> dict[str, str]:
    """Версии выводов выбранного периода: код метрики → updated_at.

    В отличие от load_notes, сюда попадают и строки с пустым текстом: запись
    существует — значит, у неё есть версия, и стёртый вывод тоже защищён от
    перезаписи вслепую.
    """
    key = period_key(period_ids)
    if not project_id:
        return {}
    try:
        rows = list_manual(project_id, table_name=TABLE)
    except Exception:  # noqa: BLE001 — раздел работает и без сохранённых выводов
        return {}
    if rows is None or not isinstance(rows, pd.DataFrame) or rows.empty:
        return {}

    versions: dict[str, str] = {}
    for _, row in rows.iterrows():
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get("period_key", "")) != key:
            continue
        code = str(payload.get("metric") or "").strip()
        value = row.get("updated_at")
        if code and not pd.isna(value):
            versions[code] = value
    return versions


def save_note(
    project_id: str,
    period_ids,
    metric_code: str,
    note: str,
    *,
    expected_updated_at=UNCHECKED_VERSION,
) -> None:
    """Сохранить вывод по одной метрике.

    С expected_updated_at сохранение условное: если вывод успел изменить
    другой редактор, поднимается ManualEditConflict и запись не происходит.
    """
    code = str(metric_code or "").strip()
    if not project_id or not code:
        return
    save_manual(
        project_id,
        TABLE,
        _row_key(code, period_ids),
        {
            "metric": code,
            "note": str(note or "").strip()[:MAX_NOTE_LENGTH],
            "period_key": period_key(period_ids),
        },
        expected_updated_at=expected_updated_at,
    )


def get_note(project_id: str, period_ids, metric_code: str) -> str:
    """Один вывод — когда перечитывать всю таблицу незачем."""
    payload = get_manual(project_id, _row_key(metric_code, period_ids))
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("note") or "").strip()
