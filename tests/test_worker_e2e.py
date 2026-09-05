"""Сквозной прогон воркера: очередь → файл из Storage → период в платформе.

Supabase подменен поддельным клиентом, Storage — словарем в памяти.
Проверяется реальный код scripts/ingest_worker.py целиком.
"""

import datetime as dt
import os
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from argparse import Namespace

# Клиент Supabase подменен, но проверка конфигурации в воркере должна пройти.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")


import pandas as pd  # noqa: E402
from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
STORAGE: dict[str, bytes] = {}
SAVED_PERIODS: list[dict] = []

store.get_supabase_client = lambda: CLIENT
store.download_storage_file = lambda path: STORAGE.get(str(path), b"")
store.save_processed_tables = lambda **kwargs: SAVED_PERIODS.append(kwargs)

import ingest_worker  # noqa: E402
from services import ingest_queue as queue  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def make_ba_export(rows: int = 40, start_day: int = 24) -> bytes:
    """Синтетическая выгрузка Brand Analytics с сюжетами и теговыми колонками."""
    random.seed(11)
    topics = ["Рост цен на утеплитель", "Новый завод", "Отзывы о монтаже"]
    sources = ["vk.com", "t.me", "rbc.ru"]
    base = dt.datetime(2026, 4, start_day, 9, 0)
    data = []
    for i in range(rows):
        ts = base + dt.timedelta(hours=i * 3)
        data.append(
            {
                "ID сообщения": f"m{i}",
                "Hash сообщения": f"h{i}",
                "Дата": ts.strftime("%d.%m.%Y"),
                "Время": ts.strftime("%H:%M"),
                "Сообщение": f"{topics[i % 3]}. Комментарий {i} про цену и качество.",
                "Автор": f"user{i % 8}",
                "Url": f"https://{sources[i % 3]}/p/{i}",
                "Источник": sources[i % 3],
                "Тип источника": "Соцсети",
                "Тональность": ["позитив", "нейтрал", "негатив"][i % 3],
                "Аудитория": f"{random.randint(1000, 50000):,}".replace(",", " "),
                "Просмотры": str(random.randint(100, 4000)),
                "Вовлеченность": str(random.randint(0, 200)),
                "Сюжет": topics[i % 3],
                "Обработано": "да",
                "ТЕХНОНИКОЛЬ": "ТЕХНОНИКОЛЬ" if i % 2 == 0 else "",
                "отзыв": "отзыв" if i % 4 == 0 else "",
            }
        )
    path = f"/tmp/ba_worker_{start_day}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(data).to_excel(writer, sheet_name="Сообщения", index=False)
    with open(path, "rb") as handle:
        return handle.read()


ARGS = Namespace(
    max_tasks=5,
    once=False,
    work_dir="/tmp/worker_work",
    worker_id="test-worker",
    requeue_stale_minutes=45,
    no_fail_on_error=False,
    verbose=False,
)

print("1. Настройка источника и постановка задач")
queue.upsert_source(
    source_key="ba-weekly",
    project_id="tn_project",
    title="BA еженедельно",
    source_system="brand_analytics",
    params={"similarity_threshold": 0.3, "replace": True},
)
STORAGE["inbox/ba-weekly/week1.xlsx"] = make_ba_export(40, 24)
ok_task = queue.enqueue_task(
    storage_path="inbox/ba-weekly/week1.xlsx",
    source_key="ba-weekly",
    original_filename="Выгрузка недели.xlsx",
    file_sha256="hash-week1",
)
missing_task = queue.enqueue_task(
    storage_path="inbox/ba-weekly/потерян.xlsx",
    source_key="ba-weekly",
    original_filename="потерян.xlsx",
    file_sha256="hash-missing",
)
check("две задачи в очереди", len(queue.list_tasks(limit=10)) == 2)

print("2. Запуск воркера")
exit_code = ingest_worker.run(ARGS)
check("воркер вернул код ошибки из-за битой задачи", exit_code == 1, str(exit_code))
check("период сохранен ровно один", len(SAVED_PERIODS) == 1, str(len(SAVED_PERIODS)))

saved = SAVED_PERIODS[0] if SAVED_PERIODS else {}
messages = saved.get("tables", {}).get("messages", pd.DataFrame())
events = saved.get("tables", {}).get("events", pd.DataFrame())
check("сообщения обработаны", len(messages) == 40, str(len(messages)))
check("инфоповоды по сюжетам BA", len(events) == 3, str(len(events)))
check(
    "название периода из дат сообщений",
    str(saved.get("period_name", "")).startswith("24.04.2026"),
    str(saved.get("period_name")),
)
check("даты периода проставлены", bool(saved.get("date_from")) and bool(saved.get("date_to")))
manifest = saved.get("manifest", {})
check("в манифесте отмечен автоматический импорт", manifest.get("ingest", {}).get("mode") == "auto")
check("в манифесте сохранен путь файла", manifest.get("storage_path") == "inbox/ba-weekly/week1.xlsx")

print("3. Статусы задач после прогона")
done = queue.get_task(ok_task)
failed = queue.get_task(missing_task)
check("успешная задача помечена done", done["status"] == "done", done["status"])
check("в задаче записан period_id", bool(done.get("period_id")))
check("в результате есть статистика", done.get("result", {}).get("messages") == 40)
check("задача с пропавшим файлом — error", failed["status"] == "error", failed["status"])
check("текст ошибки понятный", "Storage" in str(failed.get("error_message")), str(failed.get("error_message"))[:80])

print("4. Повторный запуск не создает дублей")
before = len(SAVED_PERIODS)
exit_code_2 = ingest_worker.run(ARGS)
check("новых периодов нет", len(SAVED_PERIODS) == before)
check("пустая очередь — код 0", exit_code_2 == 0, str(exit_code_2))

print("5. Исправленный отчет за тот же период перезаписывает период, а не дублирует")
queue.retry_task(missing_task)
# Тот же диапазон дат, другое имя файла (BA прислал исправленную выгрузку).
STORAGE["inbox/ba-weekly/потерян.xlsx"] = make_ba_export(40, 24)
ingest_worker.run(ARGS)
period_ids = {item["period_id"] for item in SAVED_PERIODS}
period_names = {item["period_name"] for item in SAVED_PERIODS}
check(
    "тот же период — тот же period_id",
    len(SAVED_PERIODS) == 2 and len(period_ids) == 1,
    f"периодов {len(SAVED_PERIODS)}, id {period_ids}",
)
check("название периода не изменилось", len(period_names) == 1, str(period_names))
check("replace=True при перезаписи", all(item.get("replace") for item in SAVED_PERIODS))

print("6. Другой диапазон дат создает отдельный период")
STORAGE["inbox/ba-weekly/week2.xlsx"] = make_ba_export(30, 1)
queue.enqueue_task(
    storage_path="inbox/ba-weekly/week2.xlsx",
    source_key="ba-weekly",
    original_filename="Выгрузка недели 2.xlsx",
    file_sha256="hash-week2",
)
ingest_worker.run(ARGS)
check("периодов стало три", len(SAVED_PERIODS) == 3, str(len(SAVED_PERIODS)))
check(
    "новый период с отдельным id",
    len({item["period_id"] for item in SAVED_PERIODS}) == 2,
    str({item["period_name"] for item in SAVED_PERIODS}),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Сквозной прогон воркера пройден.")
