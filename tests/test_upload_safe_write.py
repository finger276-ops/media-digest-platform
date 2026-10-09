# -*- coding: utf-8 -*-
"""Безопасная запись загрузки: обрыв посреди записи не портит период.

Раньше загрузка файла сразу помечала период активным, удаляла все его старые
строки и писала новые пачками без транзакции и без повторов. Обрыв посреди
записи оставлял активный период пустым или неполным, а прежние данные — уже
стёртыми. Теперь загрузка пишет так же, как пересборка: новые строки поверх
старых, лишние удаляются после, метаданные периода — последними; новый период
до конца записи не виден; временные сбои повторяются.

Поддельный Supabase здесь проверяет внешний ключ строк периода, как настоящая
база: строку platform_table_rows без строки периода в platform_periods он не
примет. Без этой проверки фейк молча пропустил бы запись строк нового периода
раньше самого периода — а в Postgres она падала бы на первой же пачке.

Мутационные проверки (что ломает какой тест):
- в ingest.process_canonical убрать keep_old_rows_until_written=True ->
  краснеют «загрузка просит безопасную запись», «(а) новый период при обрыве
  не виден», «(б) старые строки на месте», «(б) метаданные прежние» и
  проверки раздела (в);
- в save_processed_tables не писать заготовку нового периода -> краснеют
  «чистая загрузка проходит», «пачек несколько» и всё, что загружает файл:
  внешний ключ не пускает строки (23503);
- _write_with_retry без повторов -> краснеют все проверки «повтор: …»;
- is_transient_write_error всегда True -> краснеют «постоянные ошибки не
  считаются временными», «постоянная ошибка не повторяется» и «обрыв виден
  наверху» в (а) и (б) — одиночный обрыв тихо повторяется;
- в upload_history_ui убрать finally с clear_platform_caches -> краснеют
  «кеш сброшен и при обрыве» и «кеш сброшен после успешной загрузки»;
- в upload_history_ui убрать новую ветку except Exception -> краснеют
  «страница отработала без исключения», «понятное сообщение об обрыве»,
  «владелец платформы получил сигнал»;
- в upload_history_ui убрать LOGGER.error с exc_info -> краснеет «трейсбек
  упавшей загрузки в логе»;
- в delete_project перебирать периоды через list_periods -> краснеют
  «удаление проекта убирает исходный файл оборванной загрузки» и «…строки и
  заготовку оборванного периода».
"""

import logging
import os
import sys
import tempfile
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

import httpx  # noqa: E402
from postgrest import SyncPostgrestClient  # noqa: E402
from postgrest.exceptions import APIError  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class LogRecords(logging.Handler):
    """Записи лога: что платформа оставила владельцу в локальном логе."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _Query:
    """Запрос поддельного клиента с точкой перед выполнением."""

    def __init__(self, client, name, op, inner, payload=None):
        self._client, self._name, self._op = client, name, op
        self._inner, self._payload = inner, payload

    def __getattr__(self, attr):
        method = getattr(self._inner, attr)

        def call(*args, **kwargs):
            result = method(*args, **kwargs)
            return self if result is self._inner else result

        return call

    def execute(self):
        self._client.before_execute(self._name, self._op, self._payload)
        return self._inner.execute()


class _Table:
    def __init__(self, client, name, inner):
        self._client, self._name, self._inner = client, name, inner

    def select(self, *args, **kwargs):
        return _Query(self._client, self._name, "select", self._inner.select(*args, **kwargs))

    def update(self, payload):
        return _Query(self._client, self._name, "update", self._inner.update(payload), payload)

    def insert(self, payload):
        return _Query(self._client, self._name, "insert", self._inner.insert(payload), payload)

    def upsert(self, payload, **kwargs):
        return _Query(self._client, self._name, "upsert", self._inner.upsert(payload, **kwargs), payload)

    def delete(self):
        return _Query(self._client, self._name, "delete", self._inner.delete())


class GuardedClient(FakeClient):
    """Поддельный Supabase с внешним ключом строк периода и точкой обрыва.

    hook(таблица, операция, payload) вызывается перед каждым запросом и
    может бросить исключение — так моделируется обрыв на нужном запросе.
    """

    def __init__(self):
        super().__init__()
        self.hook = None

    def table(self, name):
        return _Table(self, name, super().table(name))

    def before_execute(self, name, op, payload):
        if self.hook is not None:
            self.hook(name, op, payload)
        if name == "platform_table_rows" and op in ("upsert", "insert"):
            periods = {
                (row.get("project_id"), row.get("period_id"))
                for row in self.db.get("platform_periods", [])
            }
            for row in payload if isinstance(payload, list) else [payload]:
                if (row.get("project_id"), row.get("period_id")) not in periods:
                    raise APIError(
                        {
                            "code": "23503",
                            "message": "insert or update on table \"platform_table_rows\" "
                            "violates foreign key constraint",
                            "hint": None,
                            "details": None,
                        }
                    )


CLIENT = GuardedClient()
store.get_supabase_client = lambda: CLIENT
SLEEPS: list = []
store._retry_sleep = SLEEPS.append
# Маленькая пачка: обрыв должен попадать между пачками каждой таблицы.
store.CHUNK_SIZE = 30

# Шпион записи: какие таблицы загрузка отдала на запись и с каким флагом.
REAL_SAVE = store.save_processed_tables
CAPTURED: list = []


def spy_save(**kwargs):
    CAPTURED.append(kwargs)
    return REAL_SAVE(**kwargs)


store.save_processed_tables = spy_save

from loadtest_pipeline import build_raw_export  # noqa: E402
from services import ingest as ingest_module  # noqa: E402
from services.ingest import build_period_name, file_sha256, process_canonical, read_canonical_bytes  # noqa: E402

# Проверяется запись, а не сборка: конвейер детерминирован, поэтому таблицы
# каждого файла собираются один раз и раздаются копиями. Иначе перебор всех
# точек обрыва — сотня прогонов конвейера и минуты в CI.
REAL_BUILD = ingest_module.build_period_tables
BUILT: dict = {}


def cached_build(canonical, *, project_id, source_filename, params, output_dir):
    if source_filename not in BUILT:
        BUILT[source_filename] = REAL_BUILD(
            canonical, project_id=project_id, source_filename=source_filename, params=params, output_dir=output_dir
        )
    manifest, tables, algo = BUILT[source_filename]
    return dict(manifest), {name: df.copy() for name, df in tables.items()}, dict(algo)


ingest_module.build_period_tables = cached_build

PROJECT = "safe_write"
WORK_DIR = tempfile.mkdtemp(prefix="safe_write_")
NOW = store.now_iso()
PROJECT_ROW = {"project_id": PROJECT, "project_name": "Безопасная запись", "status": "active",
               "settings": {}, "created_at": NOW, "updated_at": NOW}
CLIENT.db["platform_projects"] = [dict(PROJECT_ROW)]


def export(seed, messages):
    raw = build_raw_export(messages=messages, stories=8, chats=30, authors=90, days=4, seed=seed, branch="algo")
    data = raw.to_csv(index=False).encode("utf-8")
    return read_canonical_bytes(data, "export.csv", "generic"), data


# Одна и та же неделя, два файла: первая версия и исправленная — с другими
# сообщениями. Даты заданы явно, название периода строится по ним, поэтому
# обе версии попадают в один период, как исправленный отчёт той же недели.
FILES = {}
for _name, _seed, _messages, _filename in [("A", 7, 120, "неделя.csv"), ("B", 21, 100, "неделя_исправлено.csv")]:
    _canonical, _data = export(_seed, _messages)
    FILES[_name] = (_canonical, _data, _filename)
DATES = {"date_from": "2026-04-01", "date_to": "2026-04-05"}


def upload(which):
    canonical, data, filename = FILES[which]
    return process_canonical(
        canonical.copy(), project_id=PROJECT, source_filename=filename, file_bytes=data,
        source_system="generic", work_dir=WORK_DIR, **DATES,
    )


def try_upload(which):
    """Загрузка, которая не роняет тест: сломанная запись — это красная проверка."""
    try:
        return upload(which), None
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
        return None, exc


def is_break(error):
    return isinstance(error, RuntimeError) and "обрыв связи" in str(error)


# Период обеих версий — по датам, как его назовёт загрузка без названия.
PID = store.make_period_id(PROJECT, build_period_name(DATES["date_from"], DATES["date_to"]), "")


def reset():
    CLIENT.hook = None
    CLIENT.db["platform_periods"] = []
    CLIENT.db["platform_table_rows"] = []
    CLIENT.files.clear()
    CAPTURED.clear()
    SLEEPS.clear()


def expected_rows(saved):
    rows = {}
    for table_name in store.TABLES:
        for record in store.dataframe_to_payload_records(
            saved["tables"].get(table_name), table_name, PROJECT, saved["period_id"]
        ):
            rows[(table_name, record["row_id"])] = record["payload"]
    return rows


def stored_rows(period_id):
    raw = [r for r in CLIENT.db["platform_table_rows"] if r["project_id"] == PROJECT and r["period_id"] == period_id]
    return {(r["table_name"], r["row_id"]): r["payload"] for r in raw}, len(raw)


def period_row(period_id):
    rows = [dict(p) for p in CLIENT.db["platform_periods"] if p["period_id"] == period_id]
    return rows[0] if rows else None


def listed(include_inactive):
    periods = store.list_periods(PROJECT, include_inactive=include_inactive)
    return [] if periods.empty else periods["period_id"].astype(str).tolist()


class Counter:
    """Обрыв на n-м запросе нужного вида; остальные проходят."""

    def __init__(self, table, op, fail_at=None, error=None, times=1):
        self.table, self.op, self.fail_at = table, op, fail_at
        self.error = error or RuntimeError("обрыв связи")
        self.times, self.seen, self.raised = times, 0, 0

    def __call__(self, name, op, _payload):
        if name != self.table or op != self.op:
            return
        self.seen += 1
        if self.fail_at is not None and self.seen >= self.fail_at and self.raised < self.times:
            self.raised += 1
            raise self.error


def batches_of(which):
    """Сколько пачек строк пишет чистая загрузка этого файла."""
    reset()
    counter = Counter("platform_table_rows", "upsert")
    CLIENT.hook = counter
    _result, error = try_upload(which)
    reset()
    return counter.seen if error is None else 0


print("1. Загрузка просит безопасную запись")
reset()
first, error = try_upload("A")
check("чистая загрузка проходит", error is None and first["period_id"] == PID, repr(error))
check("загрузка просит безопасную запись", bool(CAPTURED) and CAPTURED[-1].get("keep_old_rows_until_written") is True,
      str(CAPTURED[-1].get("keep_old_rows_until_written") if CAPTURED else "нет записи"))
stored, raw_count = stored_rows(PID)
check("чистая загрузка: строки ровно как в сборке",
      bool(CAPTURED) and stored == expected_rows(CAPTURED[-1]) and raw_count == len(stored), f"{raw_count} строк")
check("чистая загрузка: период активен и виден",
      listed(False) == [PID] and (period_row(PID) or {}).get("status") == "active", str(period_row(PID)))
BATCHES_A, BATCHES_B = batches_of("A"), batches_of("B")
check("пачек несколько у обоих файлов", BATCHES_A >= 5 and BATCHES_B >= 5, f"{BATCHES_A}, {BATCHES_B}")
print(f"     пачек строк: A — {BATCHES_A}, B — {BATCHES_B} (CHUNK_SIZE={store.CHUNK_SIZE})")

print("2. (а) Новый период: обрыв на k-й пачке строк, затем повтор")
bad = {name: [] for name in ("raised", "hidden", "retry_listed", "retry_rows", "retry_meta")}
for k in range(1, BATCHES_A + 1):
    reset()
    CLIENT.hook = Counter("platform_table_rows", "upsert", fail_at=k)
    _result, error = try_upload("A")
    if not is_break(error):
        bad["raised"].append(k)
    if listed(True) or listed(False):
        bad["hidden"].append(k)
    CLIENT.hook = None
    result, error = try_upload("A")
    if error is not None or listed(False) != [PID] or result["period_id"] != PID:
        bad["retry_listed"].append(k)
    stored, raw_count = stored_rows(PID)
    if error is not None or stored != expected_rows(CAPTURED[-1]) or raw_count != len(stored):
        bad["retry_rows"].append(k)
    row = period_row(PID) or {}
    if row.get("status") != "active" or (row.get("manifest") or {}).get("file_sha256") != file_sha256(FILES["A"][1]):
        bad["retry_meta"].append(k)
check("(а) обрыв виден наверху", not bad["raised"], f"k={bad['raised']}")
check("(а) новый период при обрыве не виден — ни в выборе, ни в истории", not bad["hidden"], f"k={bad['hidden']}")
check("(а) повторная загрузка открывает период", not bad["retry_listed"], f"k={bad['retry_listed']}")
check("(а) строки ровно как в новой сборке: без дублей и лишних", not bad["retry_rows"], f"k={bad['retry_rows']}")
check("(а) метаданные периода — от загрузки, статус «активен»", not bad["retry_meta"], f"k={bad['retry_meta']}")

print("3. (б) Существующий период: обрыв на k-й пачке новой версии, затем повтор")
bad = {name: [] for name in ("setup", "raised", "old_rows", "meta", "visible", "retry_rows", "retry_meta")}
for k in range(1, BATCHES_B + 1):
    reset()
    _result, error = try_upload("A")
    if error is not None:
        bad["setup"].append(k)
    old_keys = set(stored_rows(PID)[0])
    old_meta = period_row(PID)
    CLIENT.hook = Counter("platform_table_rows", "upsert", fail_at=k)
    _result, error = try_upload("B")
    if not is_break(error):
        bad["raised"].append(k)
    CLIENT.hook = None
    if not old_keys <= set(stored_rows(PID)[0]):
        bad["old_rows"].append(k)
    if period_row(PID) != old_meta:
        bad["meta"].append(k)
    if listed(False) != [PID]:
        bad["visible"].append(k)
    _result, error = try_upload("B")
    stored, raw_count = stored_rows(PID)
    if error is not None or stored != expected_rows(CAPTURED[-1]) or raw_count != len(stored):
        bad["retry_rows"].append(k)
    row = period_row(PID) or {}
    if row.get("source_filename") != FILES["B"][2] or (row.get("manifest") or {}).get("file_sha256") != file_sha256(FILES["B"][1]):
        bad["retry_meta"].append(k)
check("(б) подготовка: первая версия загружается", not bad["setup"], f"k={bad['setup']}")
check("(б) обрыв виден наверху", not bad["raised"], f"k={bad['raised']}")
check("(б) старые строки на месте — все до одной", not bad["old_rows"], f"k={bad['old_rows']}")
check("(б) метаданные прежние: манифест, дата загрузки, название, файл", not bad["meta"], f"k={bad['meta']}")
check("(б) период остаётся в выборе периодов", not bad["visible"], f"k={bad['visible']}")
check("(б) повтор: ровно новые строки, устаревшие удалены", not bad["retry_rows"], f"k={bad['retry_rows']}")
check("(б) повтор: метаданные новой версии", not bad["retry_meta"], f"k={bad['retry_meta']}")

print("4. (в) Обрыв при удалении устаревших строк")
reset()
deletes = Counter("platform_table_rows", "delete")
_result, setup_a = try_upload("A")
CLIENT.hook = deletes
_result, setup_b = try_upload("B")
total_deletes = deletes.seen
reset()
_result, setup_a2 = try_upload("A")
check("(в) подготовка: обе версии загружаются", setup_a is None and setup_b is None and setup_a2 is None,
      repr(setup_a or setup_b or setup_a2))
old_meta = period_row(PID)
CLIENT.hook = Counter("platform_table_rows", "delete", fail_at=2)
_result, error = try_upload("B")
check("(в) обрыв при удалении виден наверху", is_break(error), repr(error))
CLIENT.hook = None
expected_b = expected_rows(CAPTURED[-1]) if CAPTURED else {}
stored = set(stored_rows(PID)[0])
check("(в) удалений устаревших строк несколько", total_deletes >= 2, str(total_deletes))
check("(в) новые строки записаны целиком", bool(expected_b) and set(expected_b) <= stored)
check("(в) часть устаревших строк ещё лежит", bool(stored - set(expected_b)))
check("(в) метаданные периода прежние", period_row(PID) == old_meta)
_result, error = try_upload("B")
stored, raw_count = stored_rows(PID)
check("(в) повтор дочищает: ровно новые строки",
      error is None and stored == expected_rows(CAPTURED[-1]) and raw_count == len(stored), repr(error))
check("(в) повтор: метаданные новой версии", (period_row(PID) or {}).get("source_filename") == FILES["B"][2])

print("4б. Удаление проекта после оборванной загрузки нового периода")
# Оборванный новый период невидим (list_periods его прячет), но его исходный
# файл уже лежит в Storage. Удаление проекта обещает убрать исходные файлы
# выгрузок — и этот тоже, иначе файл с данными клиента переживёт проект.
reset()
CLIENT.hook = Counter("platform_table_rows", "upsert", fail_at=3)
_result, error = try_upload("A")
CLIENT.hook = None
broken_files = sorted(CLIENT.files)
check("подготовка: загрузка оборвана, период не виден, исходный файл в хранилище",
      is_break(error) and not listed(True) and len(broken_files) == 1, f"{error!r}, файлов {len(broken_files)}")
deleted = store.delete_project(PROJECT, delete_storage=True)
check("удаление проекта убирает исходный файл оборванной загрузки",
      not CLIENT.files and deleted.get("storage_files_deleted") == 1 and deleted.get("periods_deleted") == 1,
      f"осталось {sorted(CLIENT.files)}, итог {deleted.get('periods_deleted')}/{deleted.get('storage_files_deleted')}")
check("удаление проекта убирает строки и заготовку оборванного периода",
      not [r for r in CLIENT.db["platform_table_rows"] if r["project_id"] == PROJECT]
      and not [p for p in CLIENT.db["platform_periods"] if p["project_id"] == PROJECT])
CLIENT.db["platform_projects"] = [dict(PROJECT_ROW)]

print("5. (г) Повтор временных сбоев")


def real_error(status=None, body=None, raise_exc=None):
    """Исключение, которое бросает установленный postgrest на такой ответ.

    Ответ отдаёт httpx.MockTransport — без сети. Так классификация проверяется
    на настоящих исключениях, а не на угаданных их формах.
    """

    def handler(_request):
        if raise_exc is not None:
            raise raise_exc
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body or "", headers={"content-type": "text/html"})

    http = httpx.Client(base_url="https://x.supabase.co/rest/v1", transport=httpx.MockTransport(handler))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pg = SyncPostgrestClient("https://x.supabase.co/rest/v1", http_client=http)
    try:
        pg.table("platform_table_rows").upsert([{"row_id": "1"}], on_conflict="row_id").execute()
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат
        return exc
    raise AssertionError("ответ с ошибкой не дал исключения")


def pg_json(code, message="x"):
    return {"code": code, "message": message, "hint": None, "details": None}


cloudflare = real_error(520, "<html><body>Web server is returning an unknown error</body></html>")
check("postgrest: на HTML-ответ — APIError с HTTP-статусом числом", isinstance(cloudflare, APIError) and cloudflare.code == 520,
      repr(getattr(cloudflare, "code", cloudflare)))
duplicate = real_error(409, pg_json("23505", "duplicate key"))
check("postgrest: на JSON PostgREST — APIError с кодом Postgres строкой", isinstance(duplicate, APIError) and duplicate.code == "23505",
      repr(getattr(duplicate, "code", duplicate)))

TRANSIENT = {
    "Cloudflare 520 (HTML)": real_error(520, "<html>unknown error</html>"),
    "Cloudflare 521 (HTML)": real_error(521, "<html>web server is down</html>"),
    "Cloudflare 522 (HTML)": real_error(522, "<html>connection timed out</html>"),
    "Cloudflare 523 (HTML)": real_error(523, "<html>origin is unreachable</html>"),
    "Cloudflare 524 (HTML)": real_error(524, "<html>a timeout occurred</html>"),
    "шлюз 502 ({\"message\"})": real_error(502, {"message": "An invalid response was received from the upstream server"}),
    "шлюз 503 (пустой ответ)": real_error(503, ""),
    "шлюз 504": real_error(504, "<html>Gateway Timeout</html>"),
    "500 не от PostgREST": real_error(500, "<html>Internal Server Error</html>"),
    "429 лимит частоты": real_error(429, "Too Many Requests"),
    "408 таймаут запроса": real_error(408, "Request Timeout"),
    "PGRST000 нет связи с базой": real_error(503, pg_json("PGRST000", "Could not connect with the database")),
    "PGRST003 пул подключений": real_error(504, pg_json("PGRST003", "Timed out acquiring connection from connection pool")),
    "08006 обрыв соединения с базой": real_error(503, pg_json("08006", "connection failure")),
    "40P01 взаимная блокировка": real_error(500, pg_json("40P01", "deadlock detected")),
    "53300 кончились подключения": real_error(503, pg_json("53300", "too many connections")),
    "57P03 база перезапускается": real_error(500, pg_json("57P03", "the database system is starting up")),
    "httpx.ConnectError": real_error(raise_exc=httpx.ConnectError("getaddrinfo failed")),
    "httpx.ReadTimeout": real_error(raise_exc=httpx.ReadTimeout("The read operation timed out")),
    "httpx.WriteTimeout": real_error(raise_exc=httpx.WriteTimeout("The write operation timed out")),
    "httpx.PoolTimeout": real_error(raise_exc=httpx.PoolTimeout("pool")),
    "httpx.ReadError": real_error(raise_exc=httpx.ReadError("connection reset")),
    "httpx.RemoteProtocolError": real_error(raise_exc=httpx.RemoteProtocolError("Server disconnected without sending a response.")),
    "ConnectionResetError": ConnectionResetError("reset"),
    "TimeoutError": TimeoutError("timed out"),
}
PERMANENT = {
    "540 проект на паузе (JSON)": real_error(540, {"message": "Project paused"}),
    "540 проект на паузе (HTML)": real_error(540, "<html>Project paused</html>"),
    "401 неверный ключ": real_error(401, {"message": "Invalid API key"}),
    "403": real_error(403, "<html>Forbidden</html>"),
    "404": real_error(404, "<html>Not Found</html>"),
    "501": real_error(501, "<html>Not Implemented</html>"),
    "PGRST204 нет колонки": real_error(400, pg_json("PGRST204", "Could not find the column")),
    "PGRST301 просроченный токен": real_error(401, pg_json("PGRST301", "JWT expired")),
    "21000 дубль ключа в пачке": real_error(500, pg_json("21000", "ON CONFLICT DO UPDATE command cannot affect row a second time")),
    "23505 дубль ключа": duplicate,
    "23503 внешний ключ": real_error(409, pg_json("23503", "violates foreign key constraint")),
    "25006 база только для чтения": real_error(405, pg_json("25006", "cannot execute INSERT in a read-only transaction")),
    "57014 лимит времени запроса": real_error(500, pg_json("57014", "canceling statement due to statement timeout")),
    "XX000 внутренняя ошибка базы": real_error(500, pg_json("XX000", "internal error")),
    "RuntimeError": RuntimeError("обрыв связи"),
    "ValueError": ValueError("плохие данные"),
    "httpx.UnsupportedProtocol": real_error(raise_exc=httpx.UnsupportedProtocol("no scheme")),
}
wrong = [name for name, exc in TRANSIENT.items() if not store.is_transient_write_error(exc)]
check(f"временные сбои распознаны ({len(TRANSIENT)})", not wrong, str(wrong))
wrong = [name for name, exc in PERMANENT.items() if store.is_transient_write_error(exc)]
check(f"постоянные ошибки не считаются временными ({len(PERMANENT)})", not wrong, str(wrong))


def flaky(errors, value="ok"):
    calls = []

    def request():
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return value

    return request, calls


def run_write(request):
    try:
        return store._write_with_retry("проверки", request), None
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
        return None, exc


SLEEPS.clear()
request, calls = flaky([TRANSIENT["Cloudflare 522 (HTML)"]])
result, error = run_write(request)
check("повтор: один временный сбой — запрос повторён и прошёл", error is None and result == "ok" and len(calls) == 2,
      f"вызовов {len(calls)}, {error!r}")
check("повтор: пауза перед повтором 1 с", SLEEPS == [1], str(SLEEPS))

SLEEPS.clear()
request, calls = flaky([PERMANENT["23505 дубль ключа"]])
_result, error = run_write(request)
check("постоянная ошибка не повторяется", error is PERMANENT["23505 дубль ключа"] and len(calls) == 1 and SLEEPS == [],
      f"вызовов {len(calls)}, пауз {SLEEPS}")

SLEEPS.clear()
STORE_LOG = LogRecords()
logging.getLogger("platform.store").addHandler(STORE_LOG)
request, calls = flaky([TRANSIENT["httpx.ReadTimeout"]] * 10)
_result, error = run_write(request)
check("повтор: число повторов ограничено — три повтора, паузы 1, 2, 4 с",
      isinstance(error, httpx.ReadTimeout) and len(calls) == 4 and SLEEPS == [1, 2, 4], f"вызовов {len(calls)}, пауз {SLEEPS}")
request, _calls = flaky([TRANSIENT["Cloudflare 522 (HTML)"]])
run_write(request)
logging.getLogger("platform.store").removeHandler(STORE_LOG)
retry_lines = [r.getMessage() for r in STORE_LOG.records]
check("повтор: в логе класс и код ошибки, без текста ответа",
      len(retry_lines) == 4 and all("(ReadTimeout)" in line for line in retry_lines[:3])
      and "(APIError 522)" in retry_lines[3] and not any("<html>" in line for line in retry_lines),
      str(retry_lines))

reset()
_result, setup = try_upload("A")
CLIENT.hook = Counter("platform_table_rows", "upsert", fail_at=2, error=TRANSIENT["Cloudflare 520 (HTML)"])
SLEEPS.clear()
_result, error = try_upload("B")
stored, raw_count = stored_rows(PID)
check("повтор: сбой 520 на пачке не обрывает загрузку",
      setup is None and error is None and stored == expected_rows(CAPTURED[-1]) and raw_count == len(stored)
      and (period_row(PID) or {}).get("source_filename") == FILES["B"][2] and SLEEPS == [1],
      f"пауз {SLEEPS}, {setup or error!r}")

reset()
_result, setup = try_upload("A")
CLIENT.hook = Counter("platform_table_rows", "delete", fail_at=1, error=TRANSIENT["httpx.ConnectError"], times=2)
SLEEPS.clear()
_result, error = try_upload("B")
check("повтор: сбой связи при удалении лишних строк повторён",
      setup is None and error is None and stored_rows(PID)[0] == expected_rows(CAPTURED[-1]) and SLEEPS == [1, 2],
      f"пауз {SLEEPS}, {setup or error!r}")

reset()
_result, setup = try_upload("A")
old_keys = set(stored_rows(PID)[0])
old_meta = period_row(PID)
CLIENT.hook = Counter("platform_table_rows", "upsert", fail_at=3, error=TRANSIENT["шлюз 502 ({\"message\"})"], times=100)
SLEEPS.clear()
_result, error = try_upload("B")
CLIENT.hook = None
check("повтор: затяжной сбой — ошибка наверху после трёх повторов",
      setup is None and isinstance(error, APIError) and error.code == 502 and SLEEPS == [1, 2, 4],
      f"пауз {SLEEPS}, {setup or error!r}")
check("повтор: после затяжного сбоя старые строки и метаданные на месте",
      bool(old_keys) and old_keys <= set(stored_rows(PID)[0]) and period_row(PID) == old_meta)

print("6. (д) Страница «Загрузка файла»: обрыв записи")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import upload_history_ui as ui  # noqa: E402
from error_ui import DETAILS_KEY  # noqa: E402

SMALL_CSV = build_raw_export(messages=30, stories=3, chats=6, authors=12, days=2, seed=3, branch="algo") \
    .to_csv(index=False).encode("utf-8")
CACHE_CLEARS: list = []
ALERTS: list = []
REAL_UI = (ui.process_canonical, ui.clear_platform_caches, ui.report_failure)
# Трейсбек упавшей загрузки должен остаться в логе, как у границы раздела.
APP_LOG = LogRecords()
logging.getLogger("platform.app").addHandler(APP_LOG)


def boom(*_args, **_kwargs):
    raise RuntimeError("connection refused 10.0.0.5:5432")


def fine(*_args, **_kwargs):
    return {"period_name": "Апрельская неделя", "messages": 30, "events": 2, "storage_error": ""}


def texts(at):
    out = []
    for group in (at.error, at.warning, at.info, at.success, at.caption, at.markdown, at.exception):
        out.extend(str(getattr(el, "value", "")) for el in group)
    out.extend(str(getattr(el, "label", "")) for el in at.expander)
    return out


def submit_upload(role, *, details=False):
    CACHE_CLEARS.clear()
    ALERTS.clear()
    APP_LOG.records.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "tests" / "pages_app.py"), default_timeout=180)
    at.session_state["page_name"] = "upload"
    at.session_state["page_role"] = role
    at.session_state["page_project_id"] = PROJECT
    at.session_state["page_work_dir"] = WORK_DIR
    if details:
        at.session_state[DETAILS_KEY] = True
    at.run()
    # Первое поле файла на странице — структура тегов; выгрузка — в форме ниже.
    uploaders = [u for u in at.file_uploader if "CSV или Excel" in str(u.label)]
    if not uploaders:
        return at, "нет поля загрузки выгрузки"
    uploaders[0].set_value(("неделя.csv", SMALL_CSV, "text/csv"))
    submit = [b for b in at.button if "Обработать и сохранить" in str(b.label)]
    if not submit:
        return at, "нет кнопки «Обработать и сохранить»"
    submit[0].click().run()
    return at, ""


ui.clear_platform_caches = lambda project_id=None: CACHE_CLEARS.append(project_id)
ui.report_failure = lambda where, exc=None, **ctx: ALERTS.append((where, exc, ctx)) or False
try:
    ui.process_canonical = boom
    at, problem = submit_upload("editor")
    check("страница отработала без исключения", not problem and not at.exception, problem or str(at.exception))
    shown = texts(at)
    errors = [str(e.value) for e in at.error]
    check("понятное сообщение об обрыве", any("Загрузка не завершилась" in e and "Загрузите тот же файл ещё раз" in e for e in errors),
          str(errors))
    check("сообщение не обещает, что прежние данные периода целы",
          any("смесь прежних и новых данных" in e for e in errors) and not any("не удалены" in e for e in errors),
          str(errors))
    check("трейсбек упавшей загрузки в логе",
          any(r.exc_info and isinstance(r.exc_info[1], RuntimeError) for r in APP_LOG.records),
          str([(r.getMessage(), bool(r.exc_info)) for r in APP_LOG.records]))
    check("аналитику — без технических подробностей", not any("10.0.0.5" in t or "Traceback" in t or "RuntimeError" in t for t in shown),
          str([t for t in shown if "10.0.0.5" in t or "Traceback" in t][:3]))
    check("аналитику — без раскрывашки «Подробности ошибки»", not any("Подробности ошибки" in str(e.label) for e in at.expander))
    check("кеш сброшен и при обрыве", CACHE_CLEARS == [PROJECT], str(CACHE_CLEARS))
    check("владелец платформы получил сигнал", len(ALERTS) == 1 and isinstance(ALERTS[0][1], RuntimeError)
          and ALERTS[0][2].get("project_id") == PROJECT, str([(w, c) for w, _e, c in ALERTS]))
    check("об успешном сохранении не сказано", not any("сохранен" in str(s.value) for s in at.success),
          str([s.value for s in at.success]))

    at, problem = submit_upload("owner", details=True)
    check("владельцу подробности доступны", not problem and any("Подробности ошибки" in str(e.label) for e in at.expander),
          problem or str([e.label for e in at.expander]))

    ui.process_canonical = fine
    at, problem = submit_upload("editor")
    check("успешная загрузка: сообщение об успехе", not problem and any("Апрельская неделя" in str(s.value) for s in at.success),
          problem or str([s.value for s in at.success]))
    check("кеш сброшен после успешной загрузки — один раз", CACHE_CLEARS == [PROJECT], str(CACHE_CLEARS))
    check("успешная загрузка без сигнала владельцу", not ALERTS, str(ALERTS))
finally:
    ui.process_canonical, ui.clear_platform_caches, ui.report_failure = REAL_UI

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Обрыв загрузки не оставляет период пустым: повтор дописывает и чистит.")
