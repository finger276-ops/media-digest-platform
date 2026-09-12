"""Headless-пайплайн импорта выгрузок.

Модуль повторяет логику страницы «Загрузка файла», но не зависит от Streamlit,
поэтому его можно вызывать из воркера автозагрузки (scripts/ingest_worker.py),
из CLI и из тестов.

Единый путь обработки:

    файл (bytes) → read_canonical_bytes → run_preprocess_from_dataframe
                 → сгенерированные таблицы → platform_store.save_processed_tables

Так и ручная загрузка через интерфейс, и автозагрузка через n8n дают одинаковый
результат: правила Brand Analytics (инфоповоды по «Сюжет», теги из колонок после
«Обработано») применяются в одном месте.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

import platform_store as store
from import_adapters import read_source_table
from io_utils import read_table
from preprocess import run_preprocess_from_dataframe

GENERATED_TABLES = [
    "events",
    "discussions",
    "messages",
    "discussion_messages",
    "event_discussions",
]

SUPPORTED_SUFFIXES = {".csv", ".txt", ".xlsx", ".xls", ".xlsm"}

SOURCE_SYSTEMS = {
    "auto",
    "mediologia",
    "mediologia_excel",
    "brand_analytics",
    "generic",
}

DEFAULT_ALGORITHM_PARAMS: dict[str, float] = {
    "similarity_threshold": 0.30,
    "event_gap_hours": 3.0,
    "event_window_hours": 16.0,
}


class IngestError(RuntimeError):
    """Ошибка обработки выгрузки с понятным для аналитика текстом."""


def file_sha256(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes or b"").hexdigest()


def normalize_source_system(value: Any) -> str:
    value = str(value or "auto").strip().lower()
    return value if value in SOURCE_SYSTEMS else "auto"


def safe_suffix(filename: str) -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    return suffix if suffix in SUPPORTED_SUFFIXES else ".csv"


def algorithm_params(params: dict[str, Any] | None) -> dict[str, float]:
    """Слить пользовательские параметры алгоритма с дефолтными."""
    merged = dict(DEFAULT_ALGORITHM_PARAMS)
    for key in DEFAULT_ALGORITHM_PARAMS:
        value = (params or {}).get(key)
        if value is None or value == "":
            continue
        try:
            merged[key] = float(value)
        except (TypeError, ValueError):
            continue
    return merged


def read_canonical_bytes(
    file_bytes: bytes, filename: str, source_system: str = "auto"
) -> pd.DataFrame:
    """Прочитать выгрузку из байтов и привести к каноническому виду платформы."""
    if not file_bytes:
        raise IngestError("Пустой файл выгрузки.")
    suffix = safe_suffix(filename)
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        return read_source_table(
            tmp_path, source_system=normalize_source_system(source_system)
        )
    except Exception as exc:  # noqa: BLE001 - пробрасываем понятный текст выше
        raise IngestError(
            "Не удалось прочитать файл выгрузки. Проверьте, что в нем есть таблица "
            "сообщений с датой, текстом и ссылкой/источником. "
            f"Техническая ошибка: {exc}"
        ) from exc
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def read_generated_tables(output_dir: str | Path) -> dict[str, pd.DataFrame]:
    return {name: read_table(str(output_dir), name) for name in GENERATED_TABLES}


def _as_date_text(value: Any) -> str:
    """Привести дату к ISO-строке или вернуть пустую строку."""
    if value in (None, "", "NaT"):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date().isoformat()
        except ValueError:
            continue
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return ""
    return parsed.date().isoformat()


def _human_date(iso_text: str) -> str:
    if not iso_text:
        return ""
    try:
        return datetime.strptime(iso_text, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso_text


def build_period_name(date_from: str, date_to: str, fallback: str = "") -> str:
    """Название периода в формате платформы: 24.04.2026–30.04.2026."""
    left, right = _human_date(date_from), _human_date(date_to)
    if left and right and left != right:
        return f"{left}–{right}"
    if left or right:
        return left or right
    return fallback or datetime.now().strftime("Выгрузка %d.%m.%Y %H:%M")


def process_canonical(
    canonical: pd.DataFrame,
    *,
    project_id: str,
    source_filename: str,
    file_bytes: bytes | None = None,
    period_name: str = "",
    source_system: str = "auto",
    date_from: Any = None,
    date_to: Any = None,
    params: dict[str, Any] | None = None,
    work_dir: str | Path = "data/platform",
    save_raw_file: bool = True,
    replace: bool = True,
    extra_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Обработать каноническую таблицу и сохранить период в Supabase.

    Возвращает словарь с period_id, названием периода, датами и статистикой строк.
    """
    if canonical is None or canonical.empty:
        raise IngestError("В выгрузке не найдено ни одной строки сообщений.")

    project_id = str(project_id or "").strip()
    if not project_id:
        raise IngestError("Не указан проект для загрузки выгрузки.")

    algo = algorithm_params(params)
    date_from_text = _as_date_text(date_from)
    date_to_text = _as_date_text(date_to)

    period_name = str(period_name or "").strip()
    period_id = store.make_period_id(project_id, period_name or "auto", source_filename)
    output_dir = Path(work_dir) / project_id / period_id
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = run_preprocess_from_dataframe(
        canonical,
        output=output_dir,
        source_file=source_filename,
        similarity_threshold=algo["similarity_threshold"],
        event_gap_hours=algo["event_gap_hours"],
        event_window_hours=algo["event_window_hours"],
    )
    tables = read_generated_tables(output_dir)
    messages = tables.get("messages", pd.DataFrame())

    # Даты периода: приоритет у явно заданных, иначе берем из самих сообщений.
    auto_from, auto_to = store.detect_period_dates(messages)
    date_from_text = date_from_text or _as_date_text(auto_from)
    date_to_text = date_to_text or _as_date_text(auto_to)

    if not period_name:
        period_name = build_period_name(
            date_from_text, date_to_text, fallback=Path(source_filename).stem
        )
        # Название сгенерировано по датам сообщений: считаем period_id только от
        # него, без имени файла. Тогда повторная выгрузка за тот же период
        # (например, исправленный отчет BA) перезапишет период, а не создаст дубль.
        period_id = store.make_period_id(project_id, period_name, "")

    storage_path = ""
    storage_error = ""
    if save_raw_file and file_bytes:
        try:
            storage_path = store.save_uploaded_file_to_storage(
                project_id, period_id, source_filename, file_bytes
            )
        except Exception as exc:  # noqa: BLE001 - сырой файл не критичен
            storage_error = str(exc)

    manifest = dict(manifest or {})
    manifest.update(
        {
            "storage_path": storage_path,
            "source_system": normalize_source_system(source_system),
            "algorithm_params": algo,
        }
    )
    if extra_manifest:
        manifest.update(extra_manifest)
    if file_bytes:
        manifest.setdefault("file_sha256", file_sha256(file_bytes))

    store.save_processed_tables(
        project_id=project_id,
        period_id=period_id,
        period_name=period_name,
        source_filename=source_filename,
        tables=tables,
        manifest=manifest,
        date_from=date_from_text or None,
        date_to=date_to_text or None,
        replace=replace,
    )

    return {
        "project_id": project_id,
        "period_id": period_id,
        "period_name": period_name,
        "date_from": date_from_text,
        "date_to": date_to_text,
        "source_filename": source_filename,
        "storage_path": storage_path,
        "storage_error": storage_error,
        "rows_source": int(len(canonical)),
        "messages": int(len(messages)),
        "events": int(len(tables.get("events", pd.DataFrame()))),
        "discussions": int(len(tables.get("discussions", pd.DataFrame()))),
        "output_dir": str(output_dir),
    }


def ingest_file_bytes(
    file_bytes: bytes,
    *,
    project_id: str,
    source_filename: str,
    period_name: str = "",
    source_system: str = "auto",
    date_from: Any = None,
    date_to: Any = None,
    params: dict[str, Any] | None = None,
    work_dir: str | Path = "data/platform",
    save_raw_file: bool = True,
    replace: bool = True,
    extra_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Полный цикл: байты файла → период в платформе."""
    canonical = read_canonical_bytes(file_bytes, source_filename, source_system)
    return process_canonical(
        canonical,
        project_id=project_id,
        source_filename=source_filename,
        file_bytes=file_bytes,
        period_name=period_name,
        source_system=source_system,
        date_from=date_from,
        date_to=date_to,
        params=params,
        work_dir=work_dir,
        save_raw_file=save_raw_file,
        replace=replace,
        extra_manifest=extra_manifest,
    )
