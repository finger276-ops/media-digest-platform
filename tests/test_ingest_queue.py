"""Проверка логики очереди на поддельном клиенте Supabase.

Тест повторяет форму API supabase-py (цепочки table().select().eq()...execute())
и проверяет: захват задачи, конкурентную гонку двух воркеров, ретраи,
резолв проекта по источнику и возврат зависших задач.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from datetime import datetime, timedelta, timezone



from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services import ingest_queue as queue  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Постановка задачи и чтение очереди")
task_id = queue.enqueue_task(
    storage_path="inbox/demo/file.xlsx",
    source_key="ba-weekly",
    original_filename="file.xlsx",
    file_sha256="abc123",
    source_system="brand_analytics",
)
tasks = queue.list_tasks(limit=10)
check("задача создана", len(tasks) == 1 and tasks.iloc[0]["status"] == "pending")

print("2. Резолв проекта по источнику")
queue.upsert_source(
    source_key="ba-weekly",
    project_id="tn_project",
    title="Еженедельный BA",
    source_system="brand_analytics",
    params={"similarity_threshold": 0.35, "replace": True},
)
task = queue.get_task(task_id)
project_id, params, source_system = queue.resolve_task_target(task)
check("проект найден по source_key", project_id == "tn_project", project_id)
check("параметры источника подхвачены", params.get("similarity_threshold") == 0.35, str(params))
check("формат выгрузки из источника", source_system == "brand_analytics", source_system)

print("3. Захват задачи и защита от гонки")
first = queue.claim_next_task("worker-A")
second = queue.claim_next_task("worker-B")
check("первый воркер забрал задачу", first is not None and first["task_id"] == task_id)
check("второй воркер не получил ту же задачу", second is None)
check("счетчик попыток увеличен", queue.get_task(task_id)["attempts"] == 1)

print("4. Ошибка с повтором и без")
queue.mark_error(task_id, "временная ошибка сети", retry=True)
check("задача вернулась в очередь", queue.get_task(task_id)["status"] == "pending")
queue.claim_next_task("worker-A")
queue.mark_error(task_id, "битый файл", retry=False)
check("фатальная ошибка не повторяется", queue.get_task(task_id)["status"] == "error")

print("5. Ручной перезапуск")
queue.retry_task(task_id)
restored = queue.get_task(task_id)
check("статус сброшен", restored["status"] == "pending" and restored["attempts"] == 0)

print("6. Успешное завершение")
claimed = queue.claim_next_task("worker-A")
queue.mark_done(task_id, period_id="p_1", result={"messages": 60, "events": 4})
done = queue.get_task(task_id)
check("статус done и период записан", done["status"] == "done" and done["period_id"] == "p_1")

print("7. Возврат зависших задач")
CLIENT.db[queue.QUEUE_TABLE].append(
    {
        "task_id": "stuck",
        "status": "processing",
        "attempts": 1,
        "max_attempts": 3,
        "started_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(),
        "project_id": "tn_project",
        "file_sha256": "zzz",
    }
)
requeued = queue.requeue_stale_tasks(45)
check("зависшая задача возвращена", requeued == 1 and queue.get_task("stuck")["status"] == "pending")

print("8. Превышение числа попыток")
CLIENT.db[queue.QUEUE_TABLE].append(
    {
        "task_id": "exhausted",
        "status": "pending",
        "attempts": 3,
        "max_attempts": 3,
        "created_at": "2020-01-01T00:00:00+00:00",
        "project_id": "tn_project",
        "file_sha256": "yyy",
    }
)
queue.claim_next_task("worker-A")
check("исчерпанная задача помечена ошибкой", queue.get_task("exhausted")["status"] == "error")

print("9. Отключенный источник")
queue.upsert_source(source_key="ba-weekly", project_id="tn_project", is_active=False)
try:
    queue.resolve_task_target({"source_key": "ba-weekly", "params": {}})
    check("отключенный источник блокирует загрузку", False, "исключения не было")
except ValueError as exc:
    check("отключенный источник блокирует загрузку", "отключен" in str(exc))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Все проверки очереди пройдены.")
