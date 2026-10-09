# -*- coding: utf-8 -*-
"""Повторы ID сообщения: копии схлопываются, разные сообщения не теряются.

ID сообщения — хеш «ID сообщения», а без него — ссылки. Раньше повторы ID
никто не разбирал. Две строки с одним ID в одной пачке записи Postgres
отвергает целиком (21000: ON CONFLICT DO UPDATE command cannot affect row a
second time), и загрузка обрывалась; в обсуждениях повторы после сортировки
стоят рядом и попадают в одну пачку, даже если в файле они далеко. В разных
пачках вторая строка молча затирала первую — сообщение терялось без ошибки.
Повторы бывают, когда файл склеен из двух выгрузок, выгрузки перекрываются по
датам или у разных сообщений одна ссылка (комментарии без своего ID
приезжают со ссылкой на пост). Поддельный Supabase применял пачку построчно и
этого не ловил.

Правило владельца: настоящие повторы схлопнуть и сказать об этом в отчёте о
загрузке, разные сообщения не терять ни одного.

Разделы:
(а) копии одного сообщения схлопнуты, остаётся самая заполненная строка;
    копия без автора — повтор, даже если под ссылкой пишут несколько людей;
(б) разные сообщения под одной ссылкой сохранены все: самое раннее — с
    исходным ID, остальные — с ID из текста и даты; ID не зависят ни от
    порядка строк, ни от того, добавились ли в группу другие сообщения;
(в) сквозная загрузка через поддельный Supabase: без 21000, в messages и
    discussion_messages ровно уникальные сообщения, счётчики в манифесте,
    страховка записи ни разу не сработала;
(г) страховка в platform_store.dataframe_to_payload_records: последняя
    запись, предупреждение в лог, след в манифесте и сигнал владельцу;
(д) поддельный Supabase отвечает на повтор ключа в пачке как Postgres;
(е) строки отчёта о загрузке, страница «Загрузка файла», очередь
    автозагрузки.

Мутационные проверки (что ломает какой тест):
- в resolve_repeated_ids не убирать копии (drop.extend заменить на pass) ->
  краснеют (а), (б) и (в) «итог загрузки: сообщений столько, сколько
  уникальных», а обычная выгрузка падает на сборке инфоповодов
  (InvalidIndexError);
- не выдавать новый ID остальным сообщениям группы (if key != anchor: ->
  if False:) -> краснеют (б) «ID разные», «один текст, разные авторы», (в)
  «в messages все уникальные сообщения» (страховка оставляет одно из трёх),
  «discussion_messages: по строке на сообщение» и «страховка ни разу не
  сработала»;
- исходный ID — первому по файлу, а не самому раннему (anchor = next(iter(
  messages))) -> краснеют (б) «новые сверху», «перестановки»;
- автор и ссылка в ID всегда (условия в _derived_id_source -> True) ->
  краснеет (б) «новый комментарий другого автора не меняет ID»;
- автор различает сообщения по всей группе, а не по строкам с тем же текстом
  и датой (split_author считать по всем позициям группы) -> краснеет (а)
  «копия без автора при двух авторах под ссылкой — повтор»;
- ссылка никогда не различает сообщения (split_link = False) -> краснеет (б)
  «один текст, автор и дата, но разные ссылки»;
- убрать страховку (_last_record_per_row_id всегда возвращает records как
  есть) -> краснеют проверки (г), запись падает на 21000;
- не сообщать о срабатывании страховки (if dropped: _report_dropped_rows ->
  pass) -> краснеют (г) «след в манифесте» и «сигнал владельцу»;
- в fake_supabase _reject_duplicate_keys сразу выходит -> краснеют (д)
  «повтор ключа в пачке — APIError 21000», «пачка не записана», «21000 не
  считается временным сбоем», «исключение то же, что у postgrest»;
- при равной заполненности оставлять последнюю копию, а не первую ->
  краснеют (а) «при равной заполненности осталась первая копия B» и
  tests/test_message_normalize.py «разбор совпадает с прежним».

Без правки (старый разбор, этот фейк) выгрузка Brand Analytics из раздела
(в) падала на записи с APIError 21000 — и при пачке 400, и при пачке 30:
повторы в discussion_messages после сортировки стоят рядом.
"""

import logging
import os
import random
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

# Даты нарочно в разнобой: pandas предупреждает, что разбирает их по одной.
warnings.filterwarnings("ignore", message="Could not infer format")
warnings.filterwarnings("ignore", message="Parsing dates in")

import httpx  # noqa: E402
import pandas as pd  # noqa: E402
from postgrest import SyncPostgrestClient  # noqa: E402
from postgrest.exceptions import APIError  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT
SLEEPS: list = []
store._retry_sleep = SLEEPS.append

from loadtest_pipeline import build_raw_export  # noqa: E402
from services.import_report import repeated_id_lines  # noqa: E402
from services.ingest import process_canonical, read_canonical_bytes  # noqa: E402
from services.message_normalize import (  # noqa: E402
    DUPLICATES_REMOVED,
    SHARED_ID_MESSAGES,
    normalize_messages,
)
from services.text_cleaning import stable_hash  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class LogRecords(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def normalize(rows):
    stats = {}
    messages, tags = normalize_messages(pd.DataFrame(rows), [], stats=stats)
    return messages, tags, stats


def row(message_id="", link="", text="", date="01.09.2026 10:00", author="", **extra):
    return {"Id сообщения": message_id, "Ссылка": link, "Сообщение": text, "Дата": date, "Автор": author, **extra}


print("1. (а) Копии одного сообщения схлопнуты")
# Файл склеен из двух выгрузок, которые перекрываются по датам: сообщение A
# пришло трижды, во второй выгрузке — с аудиторией и тональностью (строка
# полнее), в третьей — без колонки автора. Сообщение B — дважды, одинаково
# заполнено, но с разной тональностью: при равенстве остаётся первая.
rows = [
    row("A", "https://t.me/c/1", "Цена выросла", author="Иван", **{"Аудитория": "", "Тональность": ""}),
    row("B", "https://t.me/c/2", "Доставка задержалась", author="Мария", **{"Аудитория": "50", "Тональность": "Позитив"}),
    row("A", "https://t.me/c/1", "Цена выросла", author="Иван", **{"Аудитория": "1 200", "Тональность": "Негатив"}),
    row("C", "https://t.me/c/3", "Обычный текст", author="Пётр", **{"Аудитория": "7", "Тональность": ""}),
    row("B", "https://t.me/c/2", "Доставка задержалась", author="Мария", **{"Аудитория": "50", "Тональность": "Негатив"}),
    row("A", "https://t.me/c/1", "Цена выросла", author="", **{"Аудитория": "", "Тональность": ""}),
]
messages, tags, stats = normalize(rows)
by_text = {t: r for t, r in zip(messages["text_clean"], messages.to_dict("records"))}
check("копии убраны: три сообщения из шести строк", len(messages) == 3, str(len(messages)))
check("счётчик убранных копий", stats.get(DUPLICATES_REMOVED) == 3, str(stats))
check("разных сообщений под одним ID нет", stats.get(SHARED_ID_MESSAGES) == 0, str(stats))
check("осталась самая заполненная копия A (с аудиторией и тональностью)",
      by_text["Цена выросла"]["audience"] == 1200 and by_text["Цена выросла"]["sentiment"] == "Негатив",
      str(by_text.get("Цена выросла")))
check("при равной заполненности осталась первая копия B",
      by_text["Доставка задержалась"]["sentiment"] == "Позитив", str(by_text.get("Доставка задержалась")))
check("копия без автора — то же сообщение, а не другое", messages["text_clean"].tolist().count("Цена выросла") == 1)
# Под одной ссылкой пишут двое, а вторая выгрузка пришла без колонки автора.
# Копия комментария Олега без автора — всё равно повтор: у её текста с этой
# датой автор один.
THREAD = "https://vk.com/wall-1_555"
_m, _t, stats = normalize([
    row(link=THREAD, text="Комментарий Олега", date="02.09.2026 12:00", author="Олег"),
    row(link=THREAD, text="Комментарий Анны", date="02.09.2026 12:05", author="Анна"),
    row(link=THREAD, text="Комментарий Олега", date="02.09.2026 12:00", author=""),
])
check("копия без автора при двух авторах под ссылкой — повтор",
      len(_m) == 2 and stats == {DUPLICATES_REMOVED: 1, SHARED_ID_MESSAGES: 2}
      and _m["author"].tolist() == ["Олег", "Анна"], f"{len(_m)}, {stats}")
# А если один и тот же текст в ту же минуту написали двое, строку без автора
# не к кому отнести: сомнение решается в пользу «сохранить».
_m, _t, stats = normalize([
    row(link=THREAD, text="+", date="02.09.2026 12:10", author="Олег"),
    row(link=THREAD, text="+", date="02.09.2026 12:10", author="Анна"),
    row(link=THREAD, text="+", date="02.09.2026 12:10", author=""),
])
check("одинаковый текст двух авторов: строка без автора сохранена отдельно",
      len(_m) == 3 and _m["message_id"].is_unique and stats[DUPLICATES_REMOVED] == 0, f"{len(_m)}, {stats}")
check("ID исходные — хеш «ID сообщения»",
      sorted(messages["message_id"]) == sorted(stable_hash(v, prefix="m_") for v in "ABC"), str(messages["message_id"].tolist()))
check("порядок строк — как в файле, индекс без дыр",
      messages["text_clean"].tolist() == ["Доставка задержалась", "Цена выросла", "Обычный текст"]
      and messages.index.tolist() == [0, 1, 2], str(messages["text_clean"].tolist()))
check("теги — только у оставшихся сообщений, без повторов",
      set(tags["message_id"]) <= set(messages["message_id"]) and not tags.duplicated().any())
_m, _t, clean_stats = normalize([row("X", text="раз"), row("Y", text="два")])
check("выгрузка без повторов: счётчики нулевые", clean_stats == {DUPLICATES_REMOVED: 0, SHARED_ID_MESSAGES: 0}, str(clean_stats))

print("2. (б) Разные сообщения под одной ссылкой сохранены все")
# Комментарии выгружены без своего ID, со ссылкой на пост: ID у всех из
# ссылки. Это разные сообщения — терять нельзя ни одного.
POST = "https://vk.com/wall-1_777"
COMMENTS = [
    row(link=POST, text="Первый комментарий", date="02.09.2026 12:00", author="Олег"),
    row(link=POST, text="Второй комментарий", date="02.09.2026 12:05", author="Анна"),
    row(link=POST, text="Третий комментарий", date="02.09.2026 12:09", author="Олег"),
]
OTHER = [row("Z1", text="Не про пост"), row("Z2", text="Тоже не про пост", date="03.09.2026 09:00")]


def ids_by_text(rows):
    messages, _tags, stats = normalize(rows)
    return dict(zip(messages["text_clean"], messages["message_id"])), stats, len(messages)


def comment_ids(found):
    return {text: mid for text, mid in found.items() if "комментарий" in text}


ids, stats, count = ids_by_text(COMMENTS + [dict(COMMENTS[1])])
original = stable_hash(POST, prefix="m_")
check("все три комментария сохранены", count == 3 and len(ids) == 3, f"{count}: {ids}")
check("ID разные", len(set(ids.values())) == 3, str(ids))
check("самое раннее сохраняет исходный ID (хеш ссылки)", ids.get("Первый комментарий") == original, str(ids))
check("ID остальных — из ссылки, текста и даты, без номера строки",
      ids.get("Второй комментарий") == stable_hash("\x1f".join([POST, "Второй комментарий", "2026-09-02 12:05:00"]), prefix="m_"),
      str(ids))
check("точная копия комментария убрана", stats.get(DUPLICATES_REMOVED) == 1, str(stats))
check("счётчик разных сообщений под одним ID", stats.get(SHARED_ID_MESSAGES) == 3, str(stats))
# Та же выгрузка в другом порядке и со строками перед комментариями.
shifted, _stats, _count = ids_by_text(OTHER + [COMMENTS[0], COMMENTS[2], COMMENTS[1]])
check("другой порядок строк — те же ID у всех трёх", comment_ids(shifted) == ids, f"{shifted} против {ids}")
# Выгрузка «новые сверху»: первым в файле стоит самый поздний комментарий.
# Исходный ID всё равно у самого раннего — правки к нему не переезжают.
newest_first, _stats, _count = ids_by_text(list(reversed(COMMENTS)))
check("новые сверху — те же ID у всех трёх, исходный у самого раннего", newest_first == ids, f"{newest_first} против {ids}")
rng = random.Random(7)
stable = True
for _ in range(20):
    shuffled = COMMENTS + [dict(COMMENTS[2])] + OTHER
    rng.shuffle(shuffled)
    got, _stats, _count = ids_by_text(shuffled)
    stable = stable and comment_ids(got) == ids
check("20 случайных перестановок всех строк — ID комментариев те же", stable)
# Пост и комментарии под его ссылкой, без своих ID: исходный ID — у поста, в
# каком бы порядке ни шли строки.
POST_ROW = row(link=POST, text="Пост про цены", date="02.09.2026 11:00", author="Сообщество")
oldest_first, _stats, _count = ids_by_text([POST_ROW] + COMMENTS)
newest_first, _stats, _count = ids_by_text(list(reversed(COMMENTS)) + [POST_ROW])
check("пост сохраняет ID ссылки и при «старые сверху», и при «новые сверху»",
      oldest_first.get("Пост про цены") == original and oldest_first == newest_first,
      f"{oldest_first} против {newest_first}")
# Выгрузка того же периода дополнилась комментарием другого автора, или пришла
# без колонки автора: ID уже загруженных комментариев те же, иначе ручные
# правки к ним потеряли бы сообщение.
REPLY = row(link=POST, text="Ответ автора поста", date="02.09.2026 11:30", author="Сообщество")
LATE = row(link=POST, text="Поздний комментарий", date="02.09.2026 18:00", author="Анна")
before, _stats, _count = ids_by_text([POST_ROW, REPLY])
after, _stats, _count = ids_by_text([POST_ROW, REPLY, LATE])
no_author, _stats, _count = ids_by_text([{**r, "Автор": ""} for r in (POST_ROW, REPLY, LATE)])
check("новый комментарий другого автора не меняет ID остальных",
      after.get("Ответ автора поста") == before.get("Ответ автора поста")
      and after.get("Пост про цены") == before.get("Пост про цены"), f"{before} → {after}")
check("выгрузка без колонки автора — те же ID", no_author == after, f"{no_author} против {after}")

# Один ID, один текст и дата, но разные авторы: сообщения разные.
_m, _t, stats = normalize([row("Q", text="Да", author="Иван"), row("Q", text="Да", author="Мария")])
check("один текст, разные авторы — сохранены оба", len(_m) == 2 and _m["message_id"].is_unique, str(stats))
# «ID сообщения» у Brand Analytics — порядковый номер в теме: в склейке двух
# выгрузок под одним номером бывают разные публикации — с тем же текстом,
# автором и датой, но по разным ссылкам (одна заметка в двух сообществах).
_m, _t, stats = normalize([
    row("R7", link="https://vk.com/wall-1_10", text="Акция на утеплитель", author="Магазин"),
    row("R7", link="https://vk.com/wall-2_20", text="Акция на утеплитель", author="Магазин"),
    row("R7", link="https://vk.com/wall-2_20", text="Акция на утеплитель", author="Магазин"),
])
check("один текст, автор и дата, но разные ссылки — две публикации, копия убрана",
      len(_m) == 2 and _m["message_id"].is_unique
      and stats == {DUPLICATES_REMOVED: 1, SHARED_ID_MESSAGES: 2}, f"{len(_m)}, {stats}")
# Без ID и ссылки ID берётся из номера строки: такие строки правило не трогает.
_m, _t, stats = normalize([row(text="Без ссылки"), row(text="Без ссылки")])
check("строки без ID и ссылки не трогаются",
      len(_m) == 2 and stats == {DUPLICATES_REMOVED: 0, SHARED_ID_MESSAGES: 0}, str(stats))

print("3. (в) Сквозная загрузка файла с повторами через поддельный Supabase")
# Поддельный Supabase теперь отвечает на повтор ключа в пачке, как Postgres:
# 21000 на всю пачку. До правки выгрузка Brand Analytics с повторами
# обрывалась на записи (21000), а обычная — ещё раньше, на сборке
# инфоповодов: apply_event_quality_gate сопоставляет авторов по ID сообщения
# и на повторе ID падал (InvalidIndexError).
FILE_COMMENT_ROWS = [
    ("Первый комментарий под постом про цены", "author_1", "12:00"),
    ("Второй комментарий: доставка опять задержалась", "author_2", "12:05"),
    ("Третий комментарий про качество утеплителя", "author_3", "12:09"),
]


def export_with_repeats(branch):
    base = build_raw_export(messages=200, stories=6, chats=20, authors=60, days=3, seed=5, branch=branch)
    glued = base.iloc[:60]  # вторая выгрузка перекрывает первую по датам
    template = base.iloc[0].to_dict()
    comments = [
        {**template, "ID сообщения": "", "Hash сообщения": "", "Url": POST, "Сообщение": text, "Автор": author, "Время": time}
        for text, author, time in FILE_COMMENT_ROWS
    ]
    frame = pd.concat([base, glued, pd.DataFrame(comments + [dict(comments[1])])], ignore_index=True)
    return base, glued, frame.to_csv(index=False).encode("utf-8")


BASE, GLUED, FILE_BYTES = export_with_repeats("algo")
_ba_base, _ba_glued, BA_FILE_BYTES = export_with_repeats("ba")
FILE_ROWS = len(BASE) + len(GLUED) + len(FILE_COMMENT_ROWS) + 1
EXPECTED_MESSAGES = len(BASE) + len(FILE_COMMENT_ROWS)
COMMENT_TEXTS = [text for text, _author, _time in FILE_COMMENT_ROWS]
PROJECT = "dup_ids"
CLIENT.db["platform_projects"] = [{"project_id": PROJECT, "project_name": "Повторы ID", "status": "active", "settings": {}}]
WORK_DIR = tempfile.mkdtemp(prefix="dup_ids_")


def upload(file_bytes, source_system, chunk_size):
    CLIENT.db["platform_periods"] = []
    CLIENT.db["platform_table_rows"] = []
    store.CHUNK_SIZE = chunk_size
    canonical = read_canonical_bytes(file_bytes, "склейка.csv", source_system)
    try:
        return process_canonical(
            canonical, project_id=PROJECT, source_filename="склейка.csv", file_bytes=file_bytes,
            source_system=source_system, work_dir=WORK_DIR,
        ), None
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
        return None, exc
    finally:
        store.CHUNK_SIZE = 400


def stored(table):
    return [r for r in CLIENT.db["platform_table_rows"] if r["table_name"] == table]


# Страховка записи (раздел г) прячет повтор ключа, оставляя последнюю строку.
# При правильном разборе выгрузки ей нечего делать, поэтому её срабатывание
# здесь — провал: иначе ошибка в разборе прошла бы с потерей сообщения.
STORE_LOG = LogRecords()
logging.getLogger("platform.store").addHandler(STORE_LOG)
ALERTS: list = []
REAL_REPORT_FAILURE = store.report_failure
store.report_failure = lambda where, exc=None, **ctx: ALERTS.append((where, ctx)) or False


def net_fired():
    return [r.getMessage() for r in STORE_LOG.records if "повторяющимся ключом" in r.getMessage()] + [w for w, _ctx in ALERTS]


for label, file_bytes, source_system, chunk in [
    ("Brand Analytics, одна пачка", BA_FILE_BYTES, "brand_analytics", 400),
    ("Brand Analytics, повторы в разных пачках", BA_FILE_BYTES, "brand_analytics", 30),
    ("обычная выгрузка, одна пачка", FILE_BYTES, "generic", 400),
    ("обычная выгрузка, повторы в разных пачках", FILE_BYTES, "generic", 30),
]:
    where = label
    STORE_LOG.records.clear()
    ALERTS.clear()
    result, error = upload(file_bytes, source_system, chunk)
    check(f"[{where}] загрузка прошла, 21000 нет", error is None,
          f"{type(error).__name__} {getattr(error, 'code', '')}: {error}")
    if error is not None:
        continue
    messages_rows = stored("messages")
    texts = [r["payload"].get("text_clean") for r in messages_rows]
    message_ids = [r["payload"].get("message_id") for r in messages_rows]
    links = stored("discussion_messages")
    check(f"[{where}] итог загрузки: сообщений столько, сколько уникальных",
          result["messages"] == EXPECTED_MESSAGES, f"{result['messages']} вместо {EXPECTED_MESSAGES}")
    check(f"[{where}] в messages все уникальные сообщения, ни одно не потеряно",
          len(messages_rows) == EXPECTED_MESSAGES and len(set(message_ids)) == EXPECTED_MESSAGES
          and all(text in texts for text in COMMENT_TEXTS),
          f"{len(messages_rows)} строк, {len(set(message_ids))} ID")
    check(f"[{where}] discussion_messages: по строке на сообщение",
          len(links) == EXPECTED_MESSAGES
          and {r["payload"]["message_id"] for r in links} == set(message_ids),
          f"{len(links)} вместо {EXPECTED_MESSAGES}")
    check(f"[{where}] итог загрузки: счётчики повторов",
          result.get(DUPLICATES_REMOVED) == len(GLUED) + 1 and result.get(SHARED_ID_MESSAGES) == len(COMMENT_TEXTS),
          f"{result.get(DUPLICATES_REMOVED)}, {result.get(SHARED_ID_MESSAGES)}")
    manifest = (CLIENT.db["platform_periods"][0] if CLIENT.db["platform_periods"] else {}).get("manifest") or {}
    check(f"[{where}] счётчики — в манифесте периода",
          manifest.get(DUPLICATES_REMOVED) == len(GLUED) + 1 and manifest.get(SHARED_ID_MESSAGES) == len(COMMENT_TEXTS)
          and manifest.get("rows_source") == FILE_ROWS and manifest.get("rows_messages") == EXPECTED_MESSAGES,
          str({k: manifest.get(k) for k in (DUPLICATES_REMOVED, SHARED_ID_MESSAGES, "rows_source", "rows_messages")}))
    check(f"[{where}] страховка записи ни разу не сработала",
          not net_fired() and store.ROWS_DROPPED_DUPLICATE_KEY not in manifest,
          f"{net_fired()}, {manifest.get(store.ROWS_DROPPED_DUPLICATE_KEY)}")

print("4. (г) Страховка: один row_id — одна запись")
STORE_LOG.records.clear()
ALERTS.clear()
frame = pd.DataFrame(
    [
        {"message_id": "m_1", "text_clean": "секрет клиента первая версия"},
        {"message_id": "m_2", "text_clean": "другое"},
        {"message_id": "m_1", "text_clean": "секрет клиента вторая версия"},
        {"message_id": "m_1", "text_clean": "секрет клиента третья версия"},
    ]
)
records = store.dataframe_to_payload_records(frame, "messages", PROJECT, "p1")
check("повторы ключа убраны: две записи из четырёх", [r["row_id"] for r in records] == ["m_2", "m_1"],
      str([r["row_id"] for r in records]))
check("осталась последняя запись — как при записи по одной",
      next(r for r in records if r["row_id"] == "m_1")["payload"]["text_clean"] == "секрет клиента третья версия")
lines = [r.getMessage() for r in STORE_LOG.records if r.levelno == logging.WARNING]
check("в лог — предупреждение с таблицей и числом",
      len(lines) == 1 and "messages" in lines[0] and "2" in lines[0], str(lines))
check("в логе нет данных выгрузки", not any("секрет" in line or "m_1" in line for line in lines), str(lines))
STORE_LOG.records.clear()
pairs = pd.DataFrame(
    [
        {"discussion_id": "d1", "message_id": "m_1", "discussion_source": "time_window"},
        {"discussion_id": "d1", "message_id": "m_2", "discussion_source": "time_window"},
        {"discussion_id": "d2", "message_id": "m_1", "discussion_source": "time_window"},
        {"discussion_id": "d1", "message_id": "m_1", "discussion_source": "parent_link_segment"},
    ]
)
records = store.dataframe_to_payload_records(pairs, "discussion_messages", PROJECT, "p1")
check("составной ключ: повтор пары убран, разные пары на месте",
      [r["row_id"] for r in records] == ["d1::m_2", "d2::m_1", "d1::m_1"]
      and records[-1]["payload"]["discussion_source"] == "parent_link_segment", str([r["row_id"] for r in records]))
STORE_LOG.records.clear()
clean = store.dataframe_to_payload_records(frame.drop_duplicates("message_id"), "messages", PROJECT, "p1")
check("без повторов — все записи и без предупреждения", len(clean) == 2 and not STORE_LOG.records)
# Таблица с повтором ключа пишется через настоящий save_processed_tables:
# без страховки поддельный Supabase отверг бы пачку (21000).
CLIENT.db["platform_periods"] = []
CLIENT.db["platform_table_rows"] = []
try:
    store.save_processed_tables(
        project_id=PROJECT, period_id="p_net", period_name="Страховка", source_filename="x.csv",
        tables={"messages": frame, "discussion_messages": pairs}, keep_old_rows_until_written=True,
    )
    net_error = None
except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
    net_error = exc
check("запись таблиц с повтором ключа проходит", net_error is None, repr(net_error))
check("записано по строке на ключ", len(stored("messages")) == 2 and len(stored("discussion_messages")) == 3,
      f"{len(stored('messages'))}, {len(stored('discussion_messages'))}")
# Сюда повтор ключа доходит только из-за ошибки в коде платформы — одного
# предупреждения в логе сервера мало, его никто не читает.
DROPPED = {"messages": 2, "discussion_messages": 1}
period_row = CLIENT.db["platform_periods"][0] if CLIENT.db["platform_periods"] else {}
check("след в манифесте: сколько строк какой таблицы отброшено",
      (period_row.get("manifest") or {}).get(store.ROWS_DROPPED_DUPLICATE_KEY) == DROPPED
      and period_row.get("status") == "active", str(period_row.get("manifest")))
check("сигнал владельцу — один на запись, с таблицами и числами, без данных выгрузки",
      len(ALERTS) == 1 and "messages: 2" in ALERTS[0][1].get("tables", "")
      and "discussion_messages: 1" in ALERTS[0][1].get("tables", "")
      and "секрет" not in str(ALERTS) and "m_1" not in str(ALERTS), str(ALERTS))
# Прежний порядок записи (строка периода до строк) тоже получает след.
ALERTS.clear()
CLIENT.db["platform_periods"] = []
CLIENT.db["platform_table_rows"] = []
try:
    store.save_processed_tables(
        project_id=PROJECT, period_id="p_net_old", period_name="Страховка", source_filename="x.csv",
        tables={"messages": frame}, manifest={"rows_messages": 4},
    )
    old_error = None
except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
    old_error = exc
old_manifest = (CLIENT.db["platform_periods"][0] if CLIENT.db["platform_periods"] else {}).get("manifest") or {}
check("прежний порядок записи: след в манифесте, остальное на месте",
      old_error is None and old_manifest.get(store.ROWS_DROPPED_DUPLICATE_KEY) == {"messages": 2}
      and old_manifest.get("rows_messages") == 4 and len(ALERTS) == 1, f"{old_error!r}, {old_manifest}, {ALERTS}")
logging.getLogger("platform.store").removeHandler(STORE_LOG)
store.report_failure = REAL_REPORT_FAILURE

print("5. (д) Поддельный Supabase отвечает на повтор ключа в пачке как Postgres")
CLIENT.db["probe"] = [{"k": "old", "v": 0}]
batch = [{"k": "a", "v": 1}, {"k": "b", "v": 2}, {"k": "a", "v": 3}]
try:
    CLIENT.table("probe").upsert(batch, on_conflict="k").execute()
    fake_error = None
except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
    fake_error = exc
check("повтор ключа в пачке — APIError 21000",
      isinstance(fake_error, APIError) and fake_error.code == "21000"
      and "cannot affect row a second time" in str(fake_error.message), repr(fake_error))
check("пачка не записана ни одной строкой", CLIENT.db["probe"] == [{"k": "old", "v": 0}], str(CLIENT.db["probe"]))
check("21000 не считается временным сбоем — без повторов", fake_error is not None and not store.is_transient_write_error(fake_error))


def real_error(body):
    """Исключение настоящего postgrest на такой ответ — через MockTransport, без сети."""
    http = httpx.Client(
        base_url="https://x.supabase.co/rest/v1",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, json=body)),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pg = SyncPostgrestClient("https://x.supabase.co/rest/v1", http_client=http)
    try:
        pg.table("probe").upsert(batch, on_conflict="k").execute()
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат
        return exc
    raise AssertionError("ответ с ошибкой не дал исключения")


from fake_supabase import DUPLICATE_IN_BATCH_ERROR  # noqa: E402

real = real_error(DUPLICATE_IN_BATCH_ERROR)
check("исключение то же, что у настоящего postgrest: класс, код, текст, подсказка",
      fake_error is not None and type(real) is type(fake_error) and real.json() == fake_error.json(),
      f"{real!r} против {fake_error!r}")
for label, payload, kwargs in [
    ("ignore_duplicates (ON CONFLICT DO NOTHING) — без ошибки", batch, {"on_conflict": "k", "ignore_duplicates": True}),
    ("составной ключ: разные по одной колонке — не повтор",
     [{"k": "a", "p": 1}, {"k": "a", "p": 2}], {"on_conflict": "k,p"}),
    ("NULL в ключе не совпадает ни с чем", [{"k": None, "v": 1}, {"k": None, "v": 2}], {"on_conflict": "k"}),
]:
    try:
        CLIENT.table("probe").upsert(payload, **kwargs).execute()
        check(label, True)
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
        check(label, False, repr(exc))

print("6. (е) Отчёт о загрузке")
check("строки отчёта: обе, простым языком",
      repeated_id_lines({DUPLICATES_REMOVED: 1500, SHARED_ID_MESSAGES: 3}) == [
          "Повторы одного и того же сообщения убраны: 1 500",
          "Разные сообщения с одинаковым ID или ссылкой: 3 — сохранены все",
      ], str(repeated_id_lines({DUPLICATES_REMOVED: 1500, SHARED_ID_MESSAGES: 3})))
check("нулевые счётчики — строк нет", repeated_id_lines({DUPLICATES_REMOVED: 0, SHARED_ID_MESSAGES: 0}) == [])
check("старый манифест без счётчиков — строк нет", repeated_id_lines({"rows_messages": 10}) == [] and repeated_id_lines({}) == [])
check("только одна строка, когда второй нечего сказать",
      repeated_id_lines({DUPLICATES_REMOVED: 0, SHARED_ID_MESSAGES: 2}) == [
          "Разные сообщения с одинаковым ID или ссылкой: 2 — сохранены все"])

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import upload_history_ui as ui  # noqa: E402

summary = ui._rebuild_summary({"period_name": "Неделя", "messages": 5, "events_before": 1, "events": 1,
                               DUPLICATES_REMOVED: 2, SHARED_ID_MESSAGES: 0})
check("сводка пересборки называет убранные повторы", "Повторы одного и того же сообщения убраны: 2." in summary, summary)

# Файл из автозагрузки страница «Загрузка файла» не показывает: те же строки
# видны в очереди «Автозагрузки», в столбце «Комментарий» загруженной задачи.
import ingest_admin_ui  # noqa: E402

TASKS = pd.DataFrame([
    {"status": "done", "original_filename": "a.xlsx", "error_message": "",
     "result": {"messages": 203, DUPLICATES_REMOVED: 61, SHARED_ID_MESSAGES: 3}},
    {"status": "done", "original_filename": "b.xlsx", "error_message": "",
     "result": {"messages": 10, DUPLICATES_REMOVED: 0, SHARED_ID_MESSAGES: 0}},
    {"status": "error", "original_filename": "c.xlsx", "error_message": "Файл не найден", "result": None},
    {"status": "pending", "original_filename": "d.xlsx"},
])
queue_comments = ingest_admin_ui._tasks_view(TASKS)["Комментарий"].tolist()
check("очередь автозагрузки: у загруженной задачи — строки о повторах, у остальных как было",
      queue_comments == [
          "Повторы одного и того же сообщения убраны: 61; "
          "Разные сообщения с одинаковым ID или ссылкой: 3 — сохранены все",
          "", "Файл не найден", "",
      ], str(queue_comments))

CLEAN_BYTES = BASE.to_csv(index=False).encode("utf-8")


def submit(file_bytes):
    CLIENT.db["platform_periods"] = []
    CLIENT.db["platform_table_rows"] = []
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "tests" / "pages_app.py"), default_timeout=180)
    at.session_state["page_name"] = "upload"
    at.session_state["page_role"] = "editor"
    at.session_state["page_project_id"] = PROJECT
    at.session_state["page_work_dir"] = WORK_DIR
    at.run()
    uploaders = [u for u in at.file_uploader if "CSV или Excel" in str(u.label)]
    buttons = [b for b in at.button if "Обработать и сохранить" in str(b.label)]
    if not uploaders or not buttons:
        return at, "нет поля загрузки или кнопки"
    uploaders[0].set_value(("склейка.csv", file_bytes, "text/csv"))
    buttons[0].click().run()
    return at, ""


at, problem = submit(FILE_BYTES)
infos = [str(i.value) for i in at.info]
check("страница: загрузка файла с повторами прошла",
      not problem and not at.exception and not at.error and any("сохранен" in str(s.value) for s in at.success),
      problem or str([e.value for e in at.error] or at.exception))
check("страница: строка об убранных повторах",
      any(f"Повторы одного и того же сообщения убраны: {len(GLUED) + 1}" in i for i in infos), str(infos))
check("страница: строка о разных сообщениях с одной ссылкой",
      any(f"Разные сообщения с одинаковым ID или ссылкой: {len(COMMENT_TEXTS)} — сохранены все" in i for i in infos),
      str(infos))
at, problem = submit(CLEAN_BYTES)
check("страница: файл без повторов — строк о повторах нет",
      not problem and any("сохранен" in str(s.value) for s in at.success)
      and not any("Повторы" in str(i.value) or "одинаковым ID" in str(i.value) for i in at.info),
      problem or str([i.value for i in at.info]))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Повторы ID: копии схлопнуты, разные сообщения сохранены, пачка записи без повторов ключа.")
