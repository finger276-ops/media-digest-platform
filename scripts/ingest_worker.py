#!/usr/bin/env python3
"""Воркер автозагрузки выгрузок.

Читает очередь `platform_ingest_queue`, скачивает файл из Supabase Storage,
прогоняет его через штатный пайплайн платформы и создает период проекта.

Запуск:

    python scripts/ingest_worker.py --once
    python scripts/ingest_worker.py --max-tasks 5 --verbose

Переменные окружения (те же имена, что и в Streamlit Secrets):

    SUPABASE_URL                 обязательна
    SUPABASE_SERVICE_ROLE_KEY    обязательна
    SUPABASE_STORAGE_BUCKET      по умолчанию dashboard-csv
    INGEST_NOTIFY_WEBHOOK        необязательный webhook (например, n8n) для
                                 уведомлений об итогах обработки
    INGEST_WORK_DIR              рабочая папка для промежуточных таблиц
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for path in (ROOT, SRC):
    path_s = str(path)
    if path_s not in sys.path:
        sys.path.insert(0, path_s)

import platform_store as store  # noqa: E402
from services import ingest_queue as queue  # noqa: E402
from services.ingest import IngestError, file_sha256, ingest_file_bytes  # noqa: E402

LOG = logging.getLogger("ingest_worker")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Воркер автозагрузки выгрузок")
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=int(os.getenv("INGEST_MAX_TASKS", "10")),
        help="Максимум задач за один запуск",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Обработать одну задачу и выйти",
    )
    parser.add_argument(
        "--work-dir",
        default=os.getenv("INGEST_WORK_DIR", "data/platform"),
        help="Папка для промежуточных таблиц",
    )
    parser.add_argument(
        "--worker-id",
        default=os.getenv("INGEST_WORKER_ID", ""),
        help="Идентификатор воркера (по умолчанию — имя хоста)",
    )
    parser.add_argument(
        "--requeue-stale-minutes",
        type=int,
        default=int(os.getenv("INGEST_STALE_MINUTES", str(queue.STALE_PROCESSING_MINUTES))),
        help="Через сколько минут вернуть в очередь зависшие задачи",
    )
    parser.add_argument(
        "--no-fail-on-error",
        action="store_true",
        help="Всегда выходить с кодом 0, даже если задачи упали",
    )
    parser.add_argument("--verbose", action="store_true", help="Подробный лог")
    return parser.parse_args()


def notify(payload: dict[str, Any]) -> None:
    """Отправить итог обработки на webhook (например, в n8n для Telegram)."""
    url = str(os.getenv("INGEST_NOTIFY_WEBHOOK") or "").strip()
    if not url:
        return
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("Не удалось отправить уведомление: %s", exc)


def process_task(task: dict[str, Any], work_dir: str) -> dict[str, Any]:
    """Обработать одну задачу очереди. Возвращает результат импорта."""
    task_id = str(task.get("task_id"))
    storage_path = str(task.get("storage_path") or "").strip()
    filename = str(task.get("original_filename") or "upload.xlsx")

    project_id, params, source_system = queue.resolve_task_target(task)
    LOG.info(
        "Задача %s: проект=%s источник=%s файл=%s",
        task_id,
        project_id,
        task.get("source_key") or "-",
        filename,
    )

    file_bytes = store.download_storage_file(storage_path)
    if not file_bytes:
        raise IngestError(
            f"Файл не найден в Supabase Storage по пути «{storage_path}»."
        )

    expected_hash = str(task.get("file_sha256") or "").strip()
    actual_hash = file_sha256(file_bytes)
    if expected_hash and expected_hash != actual_hash:
        LOG.warning(
            "Хеш файла не совпал (ожидали %s, получили %s) — обрабатываю фактический файл.",
            expected_hash[:12],
            actual_hash[:12],
        )

    replace = bool(params.get("replace", True))
    result = ingest_file_bytes(
        file_bytes,
        project_id=project_id,
        source_filename=filename,
        period_name=str(task.get("period_name") or ""),
        source_system=source_system,
        date_from=task.get("date_from"),
        date_to=task.get("date_to"),
        params=params,
        work_dir=work_dir,
        save_raw_file=False,  # файл уже лежит в Storage, второй раз не сохраняем
        replace=replace,
        extra_manifest={
            "storage_path": storage_path,
            "file_sha256": actual_hash,
            "ingest": {
                "mode": "auto",
                "task_id": task_id,
                "source_key": str(task.get("source_key") or ""),
                "context": task.get("context") or {},
                "processed_at": store.now_iso(),
            },
        },
    )
    result["storage_path"] = storage_path
    return result


def run(args: argparse.Namespace) -> int:
    worker_id = args.worker_id or f"{socket.gethostname()}:{os.getpid()}"
    if not store.supabase_configured():
        LOG.error(
            "Не заданы SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY — воркер не может работать."
        )
        return 2

    try:
        requeued = queue.requeue_stale_tasks(args.requeue_stale_minutes)
        if requeued:
            LOG.info("Возвращено в очередь зависших задач: %s", requeued)
    except Exception as exc:  # noqa: BLE001 - не критично для основного цикла
        LOG.warning("Не удалось проверить зависшие задачи: %s", exc)

    limit = 1 if args.once else max(1, int(args.max_tasks))
    processed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for _ in range(limit):
        try:
            task = queue.claim_next_task(worker_id)
        except Exception as exc:  # noqa: BLE001
            LOG.error("Не удалось получить задачу из очереди: %s", exc)
            return 2
        if not task:
            break

        task_id = str(task.get("task_id"))
        started = time.monotonic()
        try:
            result = process_task(task, args.work_dir)
        except IngestError as exc:
            LOG.error("Задача %s: %s", task_id, exc)
            queue.mark_error(task_id, str(exc), retry=False)
            failed.append({"task_id": task_id, "error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - технические сбои повторяем
            details = f"{exc}\n{traceback.format_exc(limit=5)}"
            LOG.error("Задача %s упала: %s", task_id, details)
            queue.mark_error(task_id, details, retry=True)
            failed.append({"task_id": task_id, "error": str(exc)})
            continue

        elapsed = round(time.monotonic() - started, 1)
        result["elapsed_sec"] = elapsed
        queue.mark_done(task_id, period_id=result["period_id"], result=result)
        LOG.info(
            "Задача %s готова за %s c: период «%s», сообщений %s, инфоповодов %s",
            task_id,
            elapsed,
            result["period_name"],
            result["messages"],
            result["events"],
        )
        processed.append({"task_id": task_id, **result})

    summary = {
        "worker_id": worker_id,
        "processed": len(processed),
        "failed": len(failed),
        "periods": [
            {
                "project_id": item.get("project_id"),
                "period_name": item.get("period_name"),
                "messages": item.get("messages"),
                "events": item.get("events"),
            }
            for item in processed
        ],
        "errors": failed,
        "finished_at": store.now_iso(),
    }
    LOG.info("Итог: %s", json.dumps(summary, ensure_ascii=False))
    if processed or failed:
        notify(summary)

    if failed and not args.no_fail_on_error:
        return 1
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
