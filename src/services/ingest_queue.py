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

# Сколько дней источник может молчать, прежде чем раздел «Автозагрузка»
# предупредит. Выгрузки в основном еженедельные: неделя плюс день запаса на
# задержку письма, чтобы не пугать владельца в сам день выгрузки. Порог
# переопределяется в источнике (params.stale_after_days); 0 — не следить.
DEFAULT_STALE_AFTER_DAYS = 8
MAX_STALE_AFTER_DAYS = 3650

FRESH_OK = "ok"  # последний файл пришёл в пределах порога
FRESH_LATE = "late"  # файлов нет дольше порога
FRESH_NEVER = "never"  # от источника ещё не было ни одного файла
FRESH_OFF = "off"  # источник отключён или порог 0 — перерывы не отслеживаем


class SourceConfigError(ValueError):
    """Задачу нельзя обработать из-за настроек источника автозагрузки.

    Ключ не заведен в платформе, источник выключен или проект не определен —
    повтор без правки настроек упадет точно так же. Поэтому воркер и кнопка
    «Обработать очередь сейчас» не ставят такую задачу на повтор, а сразу
    показывают ошибку: иначе она за один запуск прокручивалась все три попытки
    подряд и в тексте ошибки оседала трассировка Python.

    Наследник ValueError — чтобы старые `except ValueError` продолжали работать.
    """


class SourceKeyTaken(ValueError):
    """Ключ источника уже заведён в другом проекте.

    Источник ищется по ключу, и upsert по source_key молча перепривязал бы
    чужой источник к этому проекту: выгрузки другого заказчика поехали бы
    сюда. Перенести источник можно, только удалив его в старом проекте.
    """


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
    existing = get_source(source_key)
    if existing and str(existing.get("project_id") or "") != str(project_id):
        raise SourceKeyTaken(
            f"Ключ «{source_key}» уже занят источником другого проекта. "
            "Придумайте другой ключ или сначала удалите источник там, где он заведён."
        )
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


def delete_source(source_key: str, project_id: str | None = None) -> None:
    """Удалить источник. С project_id — только если он этого проекта."""
    client = get_supabase_client()
    query = client.table(SOURCES_TABLE).delete().eq("source_key", str(source_key))
    if project_id:
        query = query.eq("project_id", str(project_id))
    query.execute()


def resolve_task_target(task: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Определить проект и параметры обработки для задачи.

    Возвращает (project_id, params, source_system). Если в задаче не указан
    project_id, проект берется из маппинга источников по source_key.

    Ключ сравнивается как есть, только без пробелов по краям: большие и
    маленькие буквы различаются (так же сравнивает PostgREST в `eq`).
    Незаведенный ключ при явном project_id — не ошибка: проект уже известен,
    задача идет со своими параметрами. Выключенный источник блокирует задачу
    всегда, даже с project_id: выключение — явный запрет владельца.

    Ошибки настройки — SourceConfigError: повтором они не лечатся.
    """
    project_id = str(task.get("project_id") or "").strip()
    params = _json_field(task.get("params"))
    source_system = str(task.get("source_system") or "auto").strip() or "auto"

    source_key = str(task.get("source_key") or "").strip()
    if source_key:
        source = get_source(source_key)
        if source:
            if not source.get("is_active", True):
                raise SourceConfigError(
                    f"Источник «{source_key}» отключен. Включите его в разделе "
                    "«Автозагрузка» и нажмите «Повторить обработку»."
                )
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
            # Без этой ветки задача упала бы на общей проверке ниже с текстом
            # «непонятно, в какой проект» — и было бы неясно, что чинить.
            # Здесь называем ключ и подсказываем, где его завести.
            raise SourceConfigError(
                f"Источник «{source_key}» не найден. Заведите его в разделе "
                "«Автозагрузка» или проверьте, что ключ в n8n совпадает с ключом "
                "в платформе буква в букву (большие и маленькие буквы "
                "различаются), затем нажмите «Повторить обработку»."
            )

    if not project_id:
        raise SourceConfigError(
            "Непонятно, в какой проект загружать файл: в задаче нет ни проекта, "
            "ни ключа источника."
        )
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
            "source_key": str(source_key or "").strip(),
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


# Сколько ожидающих задач без проекта просматривать, отыскивая среди них
# задачи своего проекта (их проект определяется по ключу источника).
ORPHAN_SCAN_LIMIT = 200


def task_belongs_to(task: dict[str, Any], project_id: str, sources: dict | None = None) -> bool:
    """Задача этого проекта: проект указан в ней или следует из её источника.

    sources — кеш «ключ → источник» на время одного прохода, чтобы не ходить
    в базу за одним и тем же источником на каждую задачу.
    """
    own = str(task.get("project_id") or "").strip()
    if own:
        return own == str(project_id)
    key = str(task.get("source_key") or "").strip()
    if not key:
        return False
    if sources is None:
        sources = {}
    if key not in sources:
        sources[key] = get_source(key)
    source = sources[key]
    return bool(source) and str(source.get("project_id") or "") == str(project_id)


def task_is_unmapped(task: dict[str, Any], sources: dict | None = None) -> bool:
    """Ничья задача: ни проекта в ней, ни заведённого источника по ключу."""
    if str(task.get("project_id") or "").strip():
        return False
    key = str(task.get("source_key") or "").strip()
    if not key:
        return True
    if sources is None:
        sources = {}
    if key not in sources:
        sources[key] = get_source(key)
    return sources[key] is None


def _pending_candidates(
    client, candidates: int, project_id: str | None, include_unmapped: bool
) -> list[dict[str, Any]]:
    base = client.table(QUEUE_TABLE).select("*").eq("status", STATUS_PENDING)
    if not project_id:
        resp = base.order("created_at").limit(int(candidates)).execute()
        return list(getattr(resp, "data", None) or [])
    own = (
        client.table(QUEUE_TABLE)
        .select("*")
        .eq("status", STATUS_PENDING)
        .eq("project_id", str(project_id))
        .order("created_at")
        .limit(int(candidates))
        .execute()
    )
    rows = list(getattr(own, "data", None) or [])
    scan = base.order("created_at").limit(ORPHAN_SCAN_LIMIT).execute()
    sources: dict[str, Any] = {}
    for row in getattr(scan, "data", None) or []:
        if str(row.get("project_id") or "").strip():
            continue
        if task_belongs_to(row, project_id, sources) or (
            include_unmapped and task_is_unmapped(row, sources)
        ):
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return rows[: int(candidates)]


def claim_next_task(
    worker_id: str,
    candidates: int = 10,
    *,
    project_id: str | None = None,
    include_unmapped: bool = False,
) -> dict[str, Any] | None:
    """Атомарно захватить следующую задачу.

    Конкурентная безопасность обеспечивается условием `status = 'pending'`
    в UPDATE: если задачу уже забрал другой воркер, обновление вернет 0 строк.

    project_id — брать только задачи этого проекта (кнопка «Обработать
    очередь сейчас» в разделе проекта). Без него — любые, как воркер
    автозагрузки: раньше кнопка тоже брала любые, и аналитик одного
    заказчика обрабатывал из своего раздела выгрузки другого.

    include_unmapped — вместе с задачами проекта брать и ничьи (ключ не
    заведён ни в одном проекте). Это кнопка владельца платформы: такая задача
    сразу получит понятную ошибку настройки, а не будет ждать воркер.
    """
    client = get_supabase_client()
    for row in _pending_candidates(client, candidates, project_id, include_unmapped):
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


# ---------------------------------------------------------------------------
# Давность поступлений по источникам
# ---------------------------------------------------------------------------
#
# Если письмо не пришло или n8n молча упал, задача просто не появляется в
# очереди — платформе не о чем сообщать. Единственное, что она может заметить,
# — что от источника давно не было новых задач.


# Сколько последних задач с похожим ключом просматривать, чтобы найти свою.
ARRIVALS_SCAN_LIMIT = 200


def last_arrivals(source_keys: Any, project_id: str | None = None) -> dict[str, str]:
    """Когда от каждого источника пришла последняя задача (её created_at).

    Статус задачи не важен: упавшая обработка — это тоже «файл дошёл», про нее
    и так говорит очередь. Запрос отдельный на каждый ключ: источников у проекта
    единицы.

    Считаются только задачи этого проекта и задачи без проекта — так же, как
    в очереди рядом. Иначе проект с тем же ключом (он зашит в шаблон n8n)
    прятал бы молчание чужого источника своими файлами. Ключ сравнивается без
    пробелов по краям, как при обработке: n8n кладёт его в очередь как есть, и
    «ba-daily\\n» — тот же источник. Поэтому поиск по шаблону, а точное
    совпадение проверяется уже здесь.
    """
    client = get_supabase_client()
    project = str(project_id or "").strip()
    arrivals: dict[str, str] = {}
    for key in dict.fromkeys(str(k or "").strip() for k in source_keys):
        if not key:
            continue
        resp = (
            client.table(QUEUE_TABLE)
            .select("created_at,source_key,project_id")
            .like("source_key", f"%{key}%")
            .order("created_at", desc=True)
            .limit(ARRIVALS_SCAN_LIMIT)
            .execute()
        )
        for row in getattr(resp, "data", None) or []:
            if str(row.get("source_key") or "").strip() != key:
                continue
            owner = str(row.get("project_id") or "").strip()
            if project and owner and owner != project:
                continue
            if row.get("created_at"):
                arrivals[key] = str(row["created_at"])
                break
    return arrivals


def stale_after_days(source: dict[str, Any]) -> int:
    """Порог тишины источника в днях; 0 — источник без расписания."""
    raw = _json_field(source.get("params")).get("stale_after_days")
    if raw is None or raw == "":
        return DEFAULT_STALE_AFTER_DAYS
    try:
        # Потолок — десять лет: огромное число, вписанное руками, ронило бы
        # timedelta и вместе с ним весь раздел, включая форму, где порог правят.
        return min(max(0, int(raw)), MAX_STALE_AFTER_DAYS)
    except (TypeError, ValueError, OverflowError):
        # Порог правят руками в базе — опечатка не должна выключать слежку.
        return DEFAULT_STALE_AFTER_DAYS


def _parse_utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def source_freshness(
    sources: pd.DataFrame | list[dict[str, Any]],
    arrivals: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Давность последнего файла по каждому источнику.

    Возвращает по строке на источник: source_key, title, last_at (UTC или None),
    days_ago (полных суток с последнего файла), limit_days, state (FRESH_*) и
    alert — нужно ли предупредить. Функция чистая: `now` передается снаружи,
    поэтому ее можно проверить без часов и без базы.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if isinstance(sources, pd.DataFrame):
        records = [] if sources.empty else sources.to_dict("records")
    else:
        records = list(sources or [])

    result: list[dict[str, Any]] = []
    for source in records:
        key = str(source.get("source_key") or "").strip()
        limit_days = stale_after_days(source)
        last_at = _parse_utc(arrivals.get(key))
        days_ago = None if last_at is None else max(0, (now - last_at).days)
        if not bool(source.get("is_active", True)) or limit_days == 0:
            state, alert = FRESH_OFF, False
        elif last_at is None:
            state = FRESH_NEVER
            # Только что заведенный источник без файлов — это ожидание, а не
            # сбой. Тревожимся, если после настройки прошел целый срок.
            configured_at = _parse_utc(source.get("created_at"))
            alert = configured_at is not None and now - configured_at > timedelta(
                days=limit_days
            )
        elif now - last_at > timedelta(days=limit_days):
            state, alert = FRESH_LATE, True
        else:
            state, alert = FRESH_OK, False
        title = source.get("title")
        result.append(
            {
                "source_key": key,
                # В DataFrame пропуск — NaN, а str(NaN) дал бы «nan» в интерфейсе.
                "title": (str(title).strip() if pd.notna(title) else "") or key,
                "is_active": bool(source.get("is_active", True)),
                "last_at": last_at,
                "days_ago": days_ago,
                "limit_days": limit_days,
                "state": state,
                "alert": bool(alert),
            }
        )
    return result
