# -*- coding: utf-8 -*-
"""Резервные копии данных платформы: снимок таблиц и восстановление.

Все данные платформы — проекты, периоды, обработанные таблицы, ручные правки
аналитиков — живут в одной базе Supabase. Отдельной копии у них не было, а
бесплатный тариф Supabase хранит свои бэкапы недолго и восстанавливает их
только целиком.

Что делает скрипт:

    backup   — снимок всех таблиц platform_* в один zip (по файлу JSON Lines на
               таблицу + manifest.json), загрузка в хранилище
               (backups/platform_ГГГГ-ММ-ДД_ЧЧММСС.zip) и удаление копий старше
               последних --keep. С --out копия ещё и пишется на диск — так
               GitHub Actions сохраняет её вне Supabase (.github/workflows/backup.yml).
    restore  — восстановление из копии: целиком или одного проекта (--project).
               Строки записываются upsert по первичному ключу: существующие
               перезаписываются, лишние не удаляются. --dry-run только считает.
    list     — какие копии лежат в хранилище.

Исходные файлы выгрузок уже лежат в хранилище, в копию базы они не входят.

Примеры:

    python scripts/backup_platform.py backup --keep 14
    python scripts/backup_platform.py list
    python scripts/backup_platform.py restore --storage backups/platform_2026-09-27_031700.zip --dry-run
    python scripts/backup_platform.py restore --file backup.zip --project taxi_project --yes
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import platform_store as store  # noqa: E402
from services.backups import (  # noqa: E402
    DEFAULT_KEEP,
    BackupError,
    list_backups,
    make_backup,
    restore_archive,
)

LOG = logging.getLogger("platform.backup")


def _read_source(args: argparse.Namespace, client: Any) -> bytes:
    if args.file:
        return Path(args.file).read_bytes()
    if args.storage:
        return store.download_storage_file(args.storage)
    backups = list_backups(client)
    if not backups:
        raise BackupError("В хранилище нет резервных копий.")
    return store.download_storage_file(backups[0]["path"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="сделать копию")
    backup.add_argument("--out", help="ещё и записать копию в файл")
    backup.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="сколько копий хранить в хранилище")
    backup.add_argument("--no-upload", action="store_true", help="не загружать в хранилище")
    sub.add_parser("list", help="копии в хранилище")
    restore = sub.add_parser("restore", help="восстановить из копии")
    restore.add_argument("--file", help="zip на диске")
    restore.add_argument("--storage", help="путь копии в хранилище (по умолчанию — последняя)")
    restore.add_argument("--project", help="восстановить только этот проект")
    restore.add_argument("--dry-run", action="store_true", help="только посчитать строки")
    restore.add_argument("--yes", action="store_true", help="без подтверждения")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = store.get_supabase_client()

    if args.command == "backup":
        result = make_backup(client, upload=not args.no_upload, keep=args.keep)
        if args.out:
            Path(args.out).write_bytes(result["bytes"])
        LOG.info("Копия: %s, %.1f МБ", result["path"] or args.out or "(не сохранена)", result["size"] / 1e6)
        for table, count in result["tables"].items():
            LOG.info("  %s: %s строк", table, count)
        if result["skipped"]:
            LOG.warning("Пропущены (таблиц нет в базе): %s", ", ".join(result["skipped"]))
        if result["removed"]:
            LOG.info("Удалены старые копии: %s", ", ".join(result["removed"]))
        return 0
    if args.command == "list":
        for item in list_backups(client):
            LOG.info("%s  %.1f МБ", item["path"], item["size"] / 1e6)
        return 0
    payload = _read_source(args, client)
    counts = restore_archive(client, payload, project_id=args.project, dry_run=True)
    for table, count in counts.items():
        LOG.info("  %s: %s строк", table, count)
    if args.dry_run:
        return 0
    if not args.yes:
        answer = input("Записать эти строки в базу поверх текущих? Введите «да»: ")
        if answer.strip().lower() != "да":
            LOG.info("Отменено.")
            return 1
    restore_archive(client, payload, project_id=args.project)
    LOG.info("Восстановлено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
