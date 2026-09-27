# -*- coding: utf-8 -*-
"""Резервные копии данных платформы: снимок таблиц и восстановление.

Все данные платформы — проекты, периоды, обработанные таблицы, ручные правки
аналитиков — живут в одной базе Supabase. Отдельной копии у них не было, а
бесплатный тариф Supabase хранит свои бэкапы недолго и восстанавливает их
только целиком.

Копия — zip с файлом JSON Lines на каждую таблицу platform_* и manifest.json.
Лежит в хранилище (backups/platform_ГГГГ-ММ-ДД_ЧЧММСС.zip), старые копии
сверх последних keep удаляются. Ночная задача GitHub Actions
(.github/workflows/backup.yml) ещё и сохраняет копию вне Supabase.
Исходные файлы выгрузок уже лежат в хранилище, в копию базы они не входят.

Восстановление — upsert по первичному ключу: существующие строки
перезаписываются, лишние не удаляются. Можно восстановить один проект.
Командная строка — scripts/backup_platform.py.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any

import platform_store as store

LOG = logging.getLogger("platform.backup")

# Таблица → колонки конфликта для upsert (первичный или уникальный ключ).
# Порядок — порядок восстановления: сначала проекты, на них ссылаются
# остальные таблицы.
BACKUP_TABLES: dict[str, str] = {
    "platform_projects": "project_id",
    "platform_periods": "period_id",
    "platform_table_rows": "project_id,period_id,table_name,row_id",
    "platform_manual_rows": "project_id,row_key",
    "platform_project_members": "project_id,user_email",
    "platform_tag_hierarchies": "project_id",
    "platform_ingest_sources": "source_key",
    "platform_ingest_queue": "task_id",
    "platform_category_benchmarks": "project_id,period_id",
    "platform_audit_log": "",
}
# Суррогатные ключи, которые база выдаёт сама: при восстановлении их не
# передаём, строка находится по уникальному ключу.
SERIAL_COLUMNS = {"platform_project_members": ("id",), "platform_audit_log": ("id",)}
# У журнала правок нет естественного ключа: повторное восстановление
# задвоило бы записи. Поэтому он восстанавливается, только если в базе (для
# проекта — у этого проекта) журнал пуст.
INSERT_ONLY_IF_EMPTY = {"platform_audit_log"}
BACKUP_PREFIX = "backups"
DEFAULT_KEEP = 14
FORMAT_VERSION = 1


class BackupError(RuntimeError):
    """Копию нельзя сделать или прочитать: понятный текст."""


def dump_tables(client: Any) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Все строки всех таблиц. Таблицы, которых нет в базе, пропускаются.

    Возвращает (данные, пропущенные таблицы): миграция может быть не накачена
    — например, бенчмарки категорий нужны не всем.
    """
    data: dict[str, list[dict[str, Any]]] = {}
    skipped: list[str] = []
    for table in BACKUP_TABLES:
        try:
            data[table] = store._fetch_all(client, table)
        except Exception as exc:  # noqa: BLE001 — таблицы может не быть
            LOG.warning("Таблица %s пропущена: %s", table, exc)
            skipped.append(table)
    return data, skipped


def build_archive(
    data: dict[str, list[dict[str, Any]]], *, created_at: str, skipped: list[str] | None = None
) -> bytes:
    manifest = {
        "format": FORMAT_VERSION,
        "created_at": created_at,
        "tables": {table: len(rows) for table, rows in data.items()},
        "skipped": list(skipped or []),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for table, rows in data.items():
            lines = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
            archive.writestr(f"{table}.jsonl", lines)
    return buffer.getvalue()


def read_archive(payload: bytes) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError("Файл не похож на резервную копию платформы.") from exc
    if int(manifest.get("format") or 0) > FORMAT_VERSION:
        raise BackupError("Копия сделана более новой версией платформы — обновите код.")
    data: dict[str, list[dict[str, Any]]] = {}
    for table in manifest.get("tables", {}):
        text = archive.read(f"{table}.jsonl").decode("utf-8")
        data[table] = [json.loads(line) for line in text.splitlines() if line.strip()]
    return manifest, data


def backup_name(now: datetime) -> str:
    return f"{BACKUP_PREFIX}/platform_{now.strftime('%Y-%m-%d_%H%M%S')}.zip"


def list_backups(client: Any) -> list[dict[str, Any]]:
    """Копии в хранилище, новые первыми: [{"path", "name", "size"}]."""
    bucket = client.storage.from_(store.storage_bucket_name())
    try:
        entries = bucket.list(BACKUP_PREFIX, {"limit": 1000}) or []
    except TypeError:
        entries = bucket.list(BACKUP_PREFIX) or []
    backups = []
    for entry in entries:
        name = str(entry.get("name") or "")
        if not (name.startswith("platform_") and name.endswith(".zip")):
            continue
        metadata = entry.get("metadata") or {}
        backups.append({"path": f"{BACKUP_PREFIX}/{name}", "name": name, "size": int(metadata.get("size") or 0)})
    # Имя содержит дату и время: сортировка по имени — сортировка по времени.
    return sorted(backups, key=lambda item: item["name"], reverse=True)


def upload_backup(client: Any, payload: bytes, *, now: datetime) -> str:
    path = backup_name(now)
    bucket = client.storage.from_(store.storage_bucket_name())
    try:
        bucket.upload(path, payload, {"content-type": "application/zip", "upsert": "true"})
    except TypeError:
        bucket.upload(path, payload)
    return path


def prune_backups(client: Any, keep: int = DEFAULT_KEEP) -> list[str]:
    """Удалить копии старше последних keep. Последнюю не удаляет никогда."""
    keep = max(1, int(keep))
    stale = [item["path"] for item in list_backups(client)[keep:]]
    if stale:
        client.storage.from_(store.storage_bucket_name()).remove(stale)
    return stale


def make_backup(
    client: Any, *, now: datetime | None = None, upload: bool = True, keep: int = DEFAULT_KEEP
) -> dict[str, Any]:
    """Снимок → zip → хранилище → удаление старых копий. Возвращает сводку."""
    now = now or datetime.now(timezone.utc)
    data, skipped = dump_tables(client)
    if not data:
        raise BackupError("Ни одной таблицы платформы не прочитано — копия не сделана.")
    payload = build_archive(data, created_at=now.isoformat(), skipped=skipped)
    path = ""
    removed: list[str] = []
    if upload:
        path = upload_backup(client, payload, now=now)
        removed = prune_backups(client, keep)
    return {
        "path": path,
        "bytes": payload,
        "size": len(payload),
        "tables": {table: len(rows) for table, rows in data.items()},
        "skipped": skipped,
        "removed": removed,
    }


def _row_in_project(table: str, row: dict[str, Any], project_id: str) -> bool:
    return str(row.get("project_id") or "") == project_id


def restore_archive(
    client: Any,
    payload: bytes,
    *,
    project_id: str | None = None,
    dry_run: bool = False,
    batch_size: int = 200,
) -> dict[str, int]:
    """Восстановить строки из копии; вернуть число строк по таблицам.

    project_id — только строки этого проекта (источники автозагрузки без
    проекта при этом пропускаются).
    """
    _manifest, data = read_archive(payload)
    counts: dict[str, int] = {}
    for table, conflict in BACKUP_TABLES.items():
        rows = data.get(table)
        if rows is None:
            continue
        if project_id:
            rows = [row for row in rows if _row_in_project(table, row, project_id)]
        drop = SERIAL_COLUMNS.get(table, ())
        rows = [{k: v for k, v in row.items() if k not in drop} for row in rows]
        if table in INSERT_ONLY_IF_EMPTY and rows and _has_rows(client, table, project_id):
            counts[table] = 0
            continue
        counts[table] = len(rows)
        if dry_run or not rows:
            continue
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            if conflict:
                client.table(table).upsert(batch, on_conflict=conflict).execute()
            else:
                client.table(table).insert(batch).execute()
    return counts


def _has_rows(client: Any, table: str, project_id: str | None) -> bool:
    query = client.table(table).select("project_id")
    if project_id:
        query = query.eq("project_id", project_id)
    return bool(query.limit(1).execute().data)
