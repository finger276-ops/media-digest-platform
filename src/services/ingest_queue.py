"""Очередь автозагрузки выгрузок в Supabase.

Таблицы описаны в `sql/platform_ingest_schema.sql`:

* `platform_ingest_queue`   — задачи от n8n (файл уже лежит в Supabase Storage);
* `platform_ingest_sources` — маппинг «внешний источник → проект платформы».

Модуль не зависит от Streamlit: его используют и воркер, и интерфейс.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from platform_store import get_supabase_client, now_iso

QUEUE_TABLE = "platform_ingest_queue"
SOURCES_TABLE = "platform_ingest_sources"

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

STATUS_LABELS = {
    STATUS_PENDING: "В очереди",
    STATUS_PROCESSING: "Обрабатывается",
    STATUS_DONE: "Загружено",
    STATUS_ERROR: "Ошибка",
    STATUS_SKIPPED: "Пропущено",
}

DEFAULT_MAX_ATTEMPTS = 3
STALE_PROCESSING_MINUTES = 45


def make_task_id(seed: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    digest = hashlib.md5(f"{seed}{uuid.uuid4()}".encode("utf-8")).hexdigest()[:8]
    return f"ing_{stamp}_{digest}"


def _rows_to_frame(rows: list[dict[str, Any]] | None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Источники автозагрузки
# ---------------------------------------------------------------------------


def list_sources(project_id: str | None = None) -> pd.DataFrame:
    client = get_supabase_client()
    query = client.table(SOURCES_TABLE).select("*")
    if project_id:
        query = query.eq("project_id", str(project_id))
    resp = query.order("source_key").execute()
    return _rows_to_frame(getattr(resp, "data", None))


def get_source(source_key: str) -> dict[str, Any] | None:
    source_key = str(source_key or "").strip()
    if not source_key:
        return None
    client = get_supabase_client()
    resp = (
        client.table(SOURCES_TABLE)
        .select("*")
        .eq("source_key", source_key)
        .limit(1)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


def upsert_source(
    *,
    source_key: str,
    project_id: str,
    title: str = "",
    source_system: str = "auto",
    params: dict[str, Any] | None = None,
    is_active: bool = True,
) -> None:
    source_key = str(source_key or "").strip()
    if not source_key:
        raise ValueError("Не указан ключ источника автозагрузки.")
    client = get_supabase_client()
    client.table(SOURCES_TABLE).upsert(
        {
            "source_key": source_key,
            "project_id": str(project_id),
            "title": str(title or ""),
            "source_system": str(source_system or "auto"),
            "params": params or {},
            "is_active": bool(is_active),
            "updated_at": now_iso(),
        },
        on_conflict="source_key",
    ).execute()


def delete_source(source_key: str) -> None:
    client = get_supabase_client()
    client.table(SOURCES_TABLE).delete().eq("source_key", str(source_key)).execute()


def resolve_task_target(task: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Определить проект и параметры обработки для задачи.

    Возвращает (project_id, params, source_system). Если в задаче не указан
    project_id, проект берется из маппинга источников по source_key.
    """
    project_id = str(task.get("project_id") or "").strip()
    params = _json_field(task.get("params"))
    source_system = str(task.get("source_system") or "auto").strip() or "auto"

    source_key = str(task.get("source_key") or "").strip()
    if source_key:
        source = get_source(source_key)
        if source:
            if not source.get("is_active", True):
                raise ValueError(f"Источник «{source_key}» отключен в настройках.")
            if not project_id:
                project_id = str(source.get("project_id") or "").strip()
            source_params = _json_field(source.get("params"))
            # Параметры задачи имеют приоритет над параметрами источника.
            merged = dict(source_params)
            merged.update(params)
            params = merged
            if source_system == "auto":
                source_system = str(source.get("source_system") or "auto") or "auto"
        elif not project_id:
            raise ValueError(
                f"Источник «{source_key}» не найден в platform_ingest_sources "
                "и в задаче не указан project_id."
            )

    if not project_id:
        raise ValueError("Для задачи не удалось определить проект платформы.")
    return project_id, params, source_system


# ---------------------------------------------------------------------------
# Очередь задач
# ---------------------------------------------------------------------------


def enqueue_task(
    *,
    storage_path: str,
    project_id: str | None = None,
    source_key: str = "",
    original_filename: str = "upload.xlsx",
    file_sha256: str = "",
    file_size: int = 0,
    source_system: str = "auto",
    period_name: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    params: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """Поставить задачу в очередь. Используется из UI и тестов; n8n делает то же
    самое обычным POST в PostgREST."""
    if not str(storage_path or "").strip():
        raise ValueError("Не указан путь к файлу в Supabase Storage.")
    task_id = make_task_id(storage_path)
    client = get_supabase_client()
    client.table(QUEUE_TABLE).insert(
        {
            "task_id": task_id,
            "project_id": str(project_id) if project_id else None,
            "source_key": str(source_key or ""),
            "storage_path": str(storage_path),
            "original_filename": str(original_filename or "upload.xlsx"),
            "file_sha256": str(file_sha256 or ""),
            "file_size": int(file_size or 0),
            "source_system": str(source_system or "auto"),
            "period_name": str(period_name or ""),
            "date_from": date_from or None,
            "date_to": date_to or None,
            "params": params or {},
            "context": context or {},
            "status": STATUS_PENDING,
            "created_at": now_iso(),
        }
    ).execute()
    return task_id


def list_tasks(
    project_id: str | None = None,
    statuses: list[str] | None = None,
    limit: int = 100,
) -> pd.DataFrame:
    client = get_supabase_client()
    query = client.table(QUEUE_TABLE).select("*")
    if project_id:
        query = query.eq("project_id", str(project_id))
    if statuses:
        query = query.in_("status", list(statuses))
    resp = query.order("created_at", desc=True).limit(int(limit)).execute()
    return _rows_to_frame(getattr(resp, "data", None))


def get_task(task_id: str) -> dict[str, Any] | None:
    client = get_supabase_client()
    resp = (
        client.table(QUEUE_TABLE)
        .select("*")
        .eq("task_id", str(task_id))
        .limit(1)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


def requeue_stale_tasks(minutes: int = STALE_PROCESSING_MINUTES) -> int:
    """Вернуть в очередь задачи, зависшие в статусе «обрабатывается».

    Такое случается, если предыдущий запуск воркера был прерван (таймаут
    GitHub Actions, перезапуск контейнера).
    """
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=int(minutes))).isoformat()
    client = get_supabase_client()
    resp = (
        client.table(QUEUE_TABLE)
        .update(
            {
                "status": STATUS_PENDING,
                "worker_id": "",
                "error_message": "Задача возвращена в очередь после зависшей обработки.",
            }
        )
        .eq("status", STATUS_PROCESSING)
        .lt("started_at", threshold)
        .execute()
    )
    return len(getattr(resp, "data", None) or [])


def claim_next_task(worker_id: str, candidates: int = 10) -> dict[str, Any] | None:
    """Атомарно захватить следующую задачу.

    Конкурентная безопасность обеспечивается условием `status = 'pending'`
    в UPDATE: если задачу уже забрал другой воркер, обновление вернет 0 строк.
    """
    client = get_supabase_client()
    resp = (
        client.table(QUEUE_TABLE)
        .select("*")
        .eq("status", STATUS_PENDING)
        .order("created_at")
        .limit(int(candidates))
        .execute()
    )
    for row in getattr(resp, "data", None) or []:
        task_id = str(row.get("task_id"))
        attempts = int(row.get("attempts") or 0)
        max_attempts = int(row.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
        if attempts >= max_attempts:
            mark_error(
                task_id,
                f"Превышено число попыток обработки ({attempts}).",
                retry=False,
            )
            continue
        updated = (
            client.table(QUEUE_TABLE)
            .update(
                {
                    "status": STATUS_PROCESSING,
                    "worker_id": str(worker_id),
                    "attempts": attempts + 1,
                    "started_at": now_iso(),
                    "error_message": "",
                }
            )
            .eq("task_id", task_id)
            .eq("status", STATUS_PENDING)
            .execute()
        )
        rows = getattr(updated, "data", None) or []
        if rows:
            return rows[0]
    return None


def mark_done(task_id: str, *, period_id: str, result: dict[str, Any]) -> None:
    client = get_supabase_client()
    client.table(QUEUE_TABLE).update(
        {
            "status": STATUS_DONE,
            "period_id": str(period_id or ""),
            "result": result or {},
            "error_message": "",
            "finished_at": now_iso(),
        }
    ).eq("task_id", str(task_id)).execute()


def mark_error(task_id: str, message: str, *, retry: bool = True) -> None:
    """Пометить задачу ошибкой.

    При `retry=True` задача возвращается в очередь, если не исчерпаны попытки —
    следующий запуск воркера попробует еще раз.
    """
    task = get_task(task_id) or {}
    attempts = int(task.get("attempts") or 0)
    max_attempts = int(task.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    status = (
        STATUS_PENDING if retry and attempts < max_attempts else STATUS_ERROR
    )
    payload: dict[str, Any] = {
        "status": status,
        "error_message": str(message or "")[:4000],
        "worker_id": "",
    }
    if status == STATUS_ERROR:
        payload["finished_at"] = now_iso()
    client = get_supabase_client()
    client.table(QUEUE_TABLE).update(payload).eq("task_id", str(task_id)).execute()


def mark_skipped(task_id: str, message: str) -> None:
    client = get_supabase_client()
    client.table(QUEUE_TABLE).update(
        {
            "status": STATUS_SKIPPED,
            "error_message": str(message or "")[:4000],
            "finished_at": now_iso(),
        }
    ).eq("task_id", str(task_id)).execute()


def retry_task(task_id: str) -> None:
    """Ручной перезапуск задачи из интерфейса: сбрасываем счетчик попыток."""
    client = get_supabase_client()
    client.table(QUEUE_TABLE).update(
        {
            "status": STATUS_PENDING,
            "attempts": 0,
            "error_message": "",
            "worker_id": "",
            "started_at": None,
            "finished_at": None,
        }
    ).eq("task_id", str(task_id)).execute()


def delete_task(task_id: str) -> None:
    client = get_supabase_client()
    client.table(QUEUE_TABLE).delete().eq("task_id", str(task_id)).execute()


def queue_stats(project_id: str | None = None) -> dict[str, int]:
    tasks = list_tasks(project_id=project_id, limit=500)
    if tasks.empty or "status" not in tasks.columns:
        return {key: 0 for key in STATUS_LABELS}
    counts = tasks["status"].value_counts().to_dict()
    return {key: int(counts.get(key, 0)) for key in STATUS_LABELS}
