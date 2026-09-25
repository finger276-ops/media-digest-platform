"""Проверка логики очереди на поддельном клиенте Supabase.

Тест повторяет форму API supabase-py (цепочки table().select().eq()...execute())
и проверяет: захват задачи, конкурентную гонку двух воркеров, ретраи,
резолв проекта по источнику, возврат зависших задач и ошибки настройки
источника (ключ не заведен, источник выключен, регистр и пробелы в ключе).
То, что воркер не повторяет такие задачи, проверяет tests/test_worker_e2e.py.

Мутационные проверки (каждая обязана покраснеть):
- убрать в resolve_task_target ветку «источник не найден» → раздел 10
  (текст скатывается в общий «непонятно, в какой проект», ключ не назван);
- бросать там голый ValueError вместо SourceConfigError → раздел 10;
- проверять is_active только когда в задаче нет project_id → раздел 11;
- убрать .strip() ключа в resolve_task_target и get_source → раздел 12;
- сравнивать ключ через .lower() → раздел 12 (регистр важен, как в PostgREST);
- claim_next_task не фильтрует по проекту → раздел 15, «аналитик альфы не
  берёт задачи беты»;
- upsert_source без проверки чужого ключа → раздел 15, «чужой источник не
  перепривязан»;
- delete_source без фильтра по проекту → раздел 15, «чужой источник не удалён».
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


def resolve(task):
    """Итог резолва: кортеж (проект, параметры, формат) или пойманная ошибка.

    Ошибку возвращаем, а не пробрасываем: иначе сломанный код ронял бы тест
    трассировкой вместо понятного «✗ такая-то проверка».
    """
    try:
        return queue.resolve_task_target(task)
    except ValueError as exc:  # SourceConfigError — наследник ValueError
        return exc


CONFIG_ERROR = queue.SourceConfigError
JARGON = ("platform_ingest_sources", "project_id", "source_key", "Traceback")

print("10. Незаведенный ключ источника")
got = resolve({"source_key": "ba-typo", "params": {}})
msg = str(got)
check(
    "задача без проекта с незаведенным ключом — ошибка настройки",
    isinstance(got, CONFIG_ERROR),
    f"{type(got).__name__}: {msg}",
)
# Без ветки «ключ не найден» задача все равно упала бы — на общей проверке
# «непонятно, в какой проект». Поэтому проверяем именно текст: он должен
# назвать ключ и сказать, что его нет.
check("в тексте назван ключ и сказано, что его нет", "«ba-typo»" in msg and "не найден" in msg, msg)
check("текст подсказывает, где завести источник", "Автозагрузка" in msg, msg)
check("в тексте нет имен таблиц и полей", not any(word in msg for word in JARGON), msg)
# Явный project_id важнее ключа: проект известен, задача идет со своими
# параметрами. Если это поведение поменяется — тест должен об этом сказать.
got = resolve({"source_key": "ba-typo", "project_id": "tn_project", "params": {"replace": False}})
check(
    "с явным проектом незаведенный ключ не мешает",
    got == ("tn_project", {"replace": False}, "auto"),
    str(got),
)

print("11. Выключенный источник")
got = resolve({"source_key": "ba-weekly", "params": {}})
check("выключенный источник — ошибка настройки", isinstance(got, CONFIG_ERROR), type(got).__name__)
check("текст подсказывает, где включить", "Автозагрузка" in str(got), str(got))
got = resolve({"source_key": "ba-weekly", "project_id": "tn_project", "params": {}})
check(
    "выключенный источник блокирует даже задачу с явным проектом",
    isinstance(got, CONFIG_ERROR) and "отключен" in str(got),
    str(got),
)

print("12. Как сравнивается ключ")
queue.upsert_source(source_key="  ba-daily \t", project_id="daily_project")
stored = [row["source_key"] for row in CLIENT.db[queue.SOURCES_TABLE]]
check("пробелы по краям срезаются при сохранении", "ba-daily" in stored, str(stored))
got = resolve({"source_key": " ba-daily\n"})
check(
    "пробелы по краям в задаче не мешают",
    isinstance(got, tuple) and got[0] == "daily_project",
    str(got),
)
got = resolve({"source_key": "BA-Daily"})
check(
    "большие и маленькие буквы различаются",
    isinstance(got, CONFIG_ERROR) and "«BA-Daily»" in str(got),
    str(got),
)
check("текст предупреждает про регистр", "большие и маленькие" in str(got), str(got))
got = resolve({"source_key": "   "})
check(
    "ключ из одних пробелов — как пустой: «не найден» не пишем",
    isinstance(got, CONFIG_ERROR) and "не найден" not in str(got),
    str(got),
)

print("13. Давность поступлений: молчание источника видно")
# Если письмо не пришло или n8n молча упал, задача просто не появится — очередь
# об этом не скажет. Единственный сигнал — от источника давно не было задач.
# Мутационные проверки: «>» → «>=» в сравнении с порогом роняет «ровно 8 дней»;
# order(desc=True) → без сортировки роняет «последняя, а не первая»; если не
# смотреть на is_active или params.stale_after_days — роняются «отключённый»,
# «0 — не следить» и «месячный»; если игнорировать created_at источника —
# роняется «только что заведённый».
import pandas as pd  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _ago(**delta):
    return (NOW - timedelta(**delta)).isoformat()


def _arrived(source_key, created_at):
    CLIENT.db[queue.QUEUE_TABLE].append(
        {
            "task_id": f"fresh_{len(CLIENT.db[queue.QUEUE_TABLE])}",
            "project_id": "tn_project",
            "source_key": source_key,
            "status": "done",
            "file_sha256": "",
            "created_at": created_at,
        }
    )


# Старый файл вставлен первым: если выборка возьмёт не последнюю задачу, а
# первую попавшуюся, свежий источник ложно окажется «замолчавшим».
_arrived("fresh-src", _ago(days=10))
_arrived("fresh-src", _ago(days=2))
_arrived("late-src", _ago(days=12))
_arrived("edge-src", _ago(days=8))
_arrived("edge-late-src", _ago(days=8, minutes=1))
_arrived("off-src", _ago(days=40))
_arrived("manual-src", _ago(days=40))
_arrived("monthly-src", _ago(days=20))
_arrived("typo-src", _ago(days=12))

fresh_sources = pd.DataFrame(
    [
        {"source_key": "fresh-src", "title": "Свежий", "is_active": True, "params": {}},
        {"source_key": "late-src", "title": "Замолчавший", "is_active": True, "params": {}},
        {"source_key": "edge-src", "title": "Ровно 8 дней", "is_active": True, "params": {}},
        {"source_key": "edge-late-src", "title": "8 дней и минута", "is_active": True, "params": {}},
        {"source_key": "off-src", "is_active": False, "params": {}},
        {"source_key": "manual-src", "title": "Без расписания", "is_active": True, "params": {"stale_after_days": 0}},
        {"source_key": "monthly-src", "title": "Месячный", "is_active": True, "params": {"stale_after_days": 31}},
        {"source_key": "typo-src", "title": "Опечатка в пороге", "is_active": True, "params": {"stale_after_days": "неделя"}},
        {"source_key": "never-src", "title": "Так и не заработал", "is_active": True, "params": {}, "created_at": _ago(days=30)},
        {"source_key": "new-src", "title": "Только что заведён", "is_active": True, "params": {}, "created_at": _ago(days=2)},
    ]
)
arrivals = queue.last_arrivals(fresh_sources["source_key"].tolist())
check(
    "берётся последняя задача источника, а не первая попавшаяся",
    arrivals.get("fresh-src") == _ago(days=2),
    str(arrivals.get("fresh-src")),
)
check("у источника без задач даты нет", "never-src" not in arrivals, str(arrivals))

rows = {
    row["source_key"]: row
    for row in queue.source_freshness(fresh_sources, arrivals, now=NOW)
}
fresh = rows["fresh-src"]
check(
    "свежий источник в порядке и без тревоги",
    fresh["state"] == queue.FRESH_OK and not fresh["alert"] and fresh["days_ago"] == 2,
    str(fresh),
)
late = rows["late-src"]
check(
    "12 дней тишины при пороге 8 — предупреждение",
    late["state"] == queue.FRESH_LATE and late["alert"] and late["days_ago"] == 12,
    str(late),
)
check(
    "ровно 8 дней — ещё не повод тревожиться (порог «больше 8 дней»)",
    not rows["edge-src"]["alert"],
    str(rows["edge-src"]),
)
check("8 дней и минута — уже предупреждение", rows["edge-late-src"]["alert"], str(rows["edge-late-src"]))
check(
    "отключённый источник не тревожит",
    rows["off-src"]["state"] == queue.FRESH_OFF and not rows["off-src"]["alert"],
    str(rows["off-src"]),
)
check(
    "без названия показывается ключ, а не «nan»",
    rows["off-src"]["title"] == "off-src",
    str(rows["off-src"]["title"]),
)
check(
    "порог 0 — источник без расписания, не следим",
    rows["manual-src"]["state"] == queue.FRESH_OFF and not rows["manual-src"]["alert"],
    str(rows["manual-src"]),
)
check(
    "свой порог источника (31 день) важнее общего",
    rows["monthly-src"]["state"] == queue.FRESH_OK and rows["monthly-src"]["limit_days"] == 31,
    str(rows["monthly-src"]),
)
check(
    "опечатка в пороге не выключает слежку — берётся общий порог",
    rows["typo-src"]["limit_days"] == queue.DEFAULT_STALE_AFTER_DAYS and rows["typo-src"]["alert"],
    str(rows["typo-src"]),
)
check(
    "источник настроен месяц назад, а файлов так и не было — предупреждение",
    rows["never-src"]["state"] == queue.FRESH_NEVER and rows["never-src"]["alert"],
    str(rows["never-src"]),
)
check(
    "только что заведённый источник без файлов — ожидание, не тревога",
    rows["new-src"]["state"] == queue.FRESH_NEVER and not rows["new-src"]["alert"],
    str(rows["new-src"]),
)

print("14. Давность: чужие проекты, пробелы в ключе, огромный порог")
# Мутационные проверки: без фильтра по проекту роняется «чужой проект»; без
# сравнения обрезанного ключа — «похожий ключ» и «подчёркивание»; поиск через
# eq вместо like — «ключ с переводом строки»; без потолка порога —
# «огромный порог».


def _arrived_raw(source_key, created_at, project_id):
    CLIENT.db[queue.QUEUE_TABLE].append(
        {
            "task_id": f"fresh_{len(CLIENT.db[queue.QUEUE_TABLE])}",
            "project_id": project_id,
            "source_key": source_key,
            "status": "done",
            "file_sha256": "",
            "created_at": created_at,
        }
    )


# Ключ зашит в шаблон n8n: другой проект с тем же ключом шлёт файлы каждый день,
# а наш источник молчит 12 дней. Чужие файлы не должны прятать молчание.
_arrived_raw("shared-src", _ago(days=12), "tn_project")
_arrived_raw("shared-src", _ago(days=1), "other_project")
# Задача без проекта раскладывается по источнику — это наш файл.
_arrived_raw("orphan-src", _ago(days=2), None)
_arrived_raw("blank-project-src", _ago(days=2), "")
# n8n кладёт ключ как есть: с пробелом или переводом строки. Обработка его
# обрезает и файл загружается — значит, файл пришёл.
_arrived_raw("spaced-src\n", _ago(days=1), "tn_project")
_arrived_raw("  padded-src ", _ago(days=1), "tn_project")
# Похожие ключи — не тот же источник.
_arrived_raw("prefix-src-weekly", _ago(days=1), "tn_project")
_arrived_raw("underXsrc", _ago(days=1), "tn_project")

arrivals = queue.last_arrivals(
    ["shared-src", "orphan-src", "blank-project-src", "spaced-src", "padded-src", "prefix-src", "under_src"],
    project_id="tn_project",
)
check(
    "файлы чужого проекта с тем же ключом не считаются",
    arrivals.get("shared-src") == _ago(days=12),
    str(arrivals.get("shared-src")),
)
check(
    "задача без проекта считается",
    arrivals.get("orphan-src") == _ago(days=2) and arrivals.get("blank-project-src") == _ago(days=2),
    str(arrivals),
)
check(
    "ключ с переводом строки или пробелами — тот же источник",
    arrivals.get("spaced-src") == _ago(days=1) and arrivals.get("padded-src") == _ago(days=1),
    str(arrivals),
)
check(
    "похожий ключ (prefix-src-weekly) не считается файлом prefix-src",
    "prefix-src" not in arrivals,
    str(arrivals),
)
check(
    "подчёркивание в ключе — обычный символ, а не «любой символ»",
    "under_src" not in arrivals,
    str(arrivals),
)
check(
    "без проекта (старые вызовы) берётся последняя задача по ключу",
    queue.last_arrivals(["shared-src"]).get("shared-src") == _ago(days=1),
    str(queue.last_arrivals(["shared-src"])),
)

huge = pd.DataFrame(
    [
        {"source_key": "huge-src", "title": "Огромный порог", "is_active": True, "params": {"stale_after_days": 10**30}},
        {"source_key": "neg-src", "title": "Отрицательный порог", "is_active": True, "params": {"stale_after_days": -5}},
    ]
)
try:
    huge_rows = {row["source_key"]: row for row in queue.source_freshness(huge, {}, now=NOW)}
    huge_error = ""
except Exception as exc:  # noqa: BLE001
    huge_rows, huge_error = {}, repr(exc)
check(
    "огромный порог в базе не роняет раздел — ограничен десятью годами",
    not huge_error
    and huge_rows["huge-src"]["limit_days"] == queue.MAX_STALE_AFTER_DAYS,
    huge_error or str(huge_rows.get("huge-src")),
)
check(
    "отрицательный порог — как 0, не следим",
    not huge_error and huge_rows["neg-src"]["state"] == queue.FRESH_OFF,
    huge_error or str(huge_rows.get("neg-src")),
)

stripped_id = queue.enqueue_task(storage_path="inbox/strip-key.xlsx", source_key=" strip-src\n")
stripped = [r for r in CLIENT.db[queue.QUEUE_TABLE] if r["task_id"] == stripped_id]
check(
    "задача из платформы ставится с обрезанным ключом",
    stripped and stripped[0]["source_key"] == "strip-src",
    str(stripped),
)

print("15. Кнопка раздела берёт только задачи своего проекта")
# Раньше «Обработать очередь сейчас» в разделе любого проекта забирала первые
# задачи всей очереди: аналитик одного заказчика обрабатывал выгрузки другого,
# а сохранение источника с чужим ключом молча перепривязывало его к себе.
CLIENT.db["platform_ingest_queue"] = []
CLIENT.db["platform_ingest_sources"] = []
queue.upsert_source(source_key="alpha-src", project_id="alpha")
queue.upsert_source(source_key="beta-src", project_id="beta")


def enqueue(name, *, project_id=None, source_key=""):
    return queue.enqueue_task(
        storage_path=f"inbox/{name}.xlsx",
        original_filename=f"{name}.xlsx",
        file_sha256=f"hash-{name}",
        project_id=project_id,
        source_key=source_key,
    )


beta_own = enqueue("beta-own", project_id="beta")
alpha_own = enqueue("alpha-own", project_id="alpha")
beta_by_source = enqueue("beta-by-source", source_key="beta-src")
alpha_by_source = enqueue("alpha-by-source", source_key="alpha-src")
unmapped = enqueue("typo", source_key="alpha-scr")


def claim_all(**kwargs):
    taken = []
    while True:
        task = queue.claim_next_task("ui", **kwargs)
        if not task:
            return taken
        taken.append(task["task_id"])


alpha_taken = claim_all(project_id="alpha")
check(
    "аналитик альфы берёт свои задачи — и с проектом, и по источнику",
    sorted(alpha_taken) == sorted([alpha_own, alpha_by_source]),
    str(alpha_taken),
)
check("аналитик альфы не берёт задачи беты", beta_own not in alpha_taken and beta_by_source not in alpha_taken)
check("и ничьи задачи не берёт", unmapped not in alpha_taken)
owner_taken = claim_all(project_id="beta", include_unmapped=True)
check(
    "владелец в разделе беты берёт задачи беты и ничьи",
    sorted(owner_taken) == sorted([beta_own, beta_by_source, unmapped]),
    str(owner_taken),
)
check("задачи без проекта опознаются по источнику", queue.task_belongs_to({"source_key": "alpha-src"}, "alpha"))
check("ничья — это незаведённый ключ", queue.task_is_unmapped({"source_key": "alpha-scr"}))
check("задача с проектом ничьей не бывает", not queue.task_is_unmapped({"project_id": "alpha", "source_key": "x"}))

CLIENT.db["platform_ingest_queue"] = []
first = enqueue("first", project_id="beta")
enqueue("second", project_id="alpha")
worker_task = queue.claim_next_task("worker")
check("воркер по-прежнему берёт любую задачу, старшую первой", worker_task and worker_task["task_id"] == first)

try:
    queue.upsert_source(source_key="alpha-src", project_id="beta")
    check("чужой источник не перепривязан", False, "ошибки не было")
except queue.SourceKeyTaken:
    check("чужой источник не перепривязан", queue.get_source("alpha-src")["project_id"] == "alpha")
queue.upsert_source(source_key="alpha-src", project_id="alpha", is_active=False)
check("свой источник меняется как раньше", queue.get_source("alpha-src")["is_active"] is False)
queue.delete_source("alpha-src", project_id="beta")
check("чужой источник не удалён", queue.get_source("alpha-src") is not None)
queue.delete_source("alpha-src", project_id="alpha")
check("свой источник удалён", queue.get_source("alpha-src") is None)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Все проверки очереди пройдены.")
