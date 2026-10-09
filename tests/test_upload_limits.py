# -*- coding: utf-8 -*-
"""Пределы одной загрузки: вес файла, распакованная книга, колонки, сообщения, текст.

Приложение на Streamlit Cloud — один процесс на всех клиентов, памяти около
гигабайта. Загрузку раньше ничто не ограничивало, и одна выгрузка полных
текстов статей перезапускала приложение у всех. Теперь read_canonical_bytes —
через него идут страница загрузки, блок категории, пересборка и воркер
автозагрузки — отказывает слишком большой выгрузке понятным текстом:

- вес файла и распакованный размер книги Excel (zip) — до pandas;
- число колонок — по заголовку, до чтения данных;
- число сообщений — таблица читается не дальше потолка строк, и файл с
  сотнями тысяч мелких строк отказывает на потолке, не доходя до канонизации;
- объём текста — знаки во всех ячейках прочитанной таблицы.

Пределы по умолчанию — из замера памяти (services/upload_limits.py, README
«Версия 4.12.9»); владелец меняет их секретами или окружением.

Мутационные проверки (что ломает какой тест):
- убрать maxUploadSize из .streamlit/config.toml -> краснеет «config.toml:
  server.maxUploadSize совпадает с пределом веса»;
- в file_problem убрать проверку веса -> краснеют «вес: на байт больше
  предела — отказ», «…до pandas», тексты и «вес по умолчанию: 20 МБ + 1
  байт»;
- в read_canonical_bytes проверять вес после чтения, а не до -> краснеют
  «…отказ до pandas», «книга: то же под именем .xls» и «бомба по
  умолчанию»;
- unpacked_size всегда None -> краснеют все проверки «книга: …» и «бомба
  по умолчанию»; UNPACKED_TO_FILE_RATIO 2.5 -> 4 -> «распакованная книга — до
  50 МБ» и «бомба по умолчанию»;
- в table_problem убрать проверку строк -> краснеют «строки: …», «по
  умолчанию: 10 001 сообщение — отказ», проверки воркера и страницы;
- в table_problem убрать проверку текста -> краснеют «текст: …» и «по
  умолчанию: …знаков текста — отказ»;
- text_chars считает только первые колонки -> краснеют «текст: считаются
  все колонки…»;
- в _read_csv_any / _read_excel_sheets убрать потолок строк (nrows) ->
  краснеют «потолок: …» (таблица читается целиком) и «потолок: xlsx…»;
- убрать check_width в чтении -> краснеют «колонки: … до чтения данных»;
- убрать ветку пустых строк в table_problem -> «пустые строки внизу…»;
- _advice_with_parts без числа частей -> «две причины сразу: …»;
- _setting без st.secrets -> краснеют «секреты: …» и проверки страницы с
  секретами; _setting без окружения -> краснеют «окружение: …», «опечатка
  видна владельцу в логе» и проверки воркера; except без OverflowError ->
  краснеют «inf в секретах…»;
- в ingest_file_bytes убрать совет автозагрузки -> краснеют проверки
  воркера и «очередь в интерфейсе»; вернуть обрезку любого текста в
  _task_comment -> «комментарий задачи: понятный текст целиком»;
- в upload_history_ui убрать except UploadTooLargeError -> краснеют
  «страница: без общего „не удалось прочитать файл“», «аналитик видит…» и
  «владельцу тот же текст…»; вернуть «Загрузите файл.» -> «страница: без
  файла…»;
- убрать подпись upload_hint под полем -> краснеют «страница: подпись с
  пределами…»; убрать max_upload_size у поля -> краснеет «страница: предел
  веса из секретов — и у поля, и в подписи 5 МБ» (20 МБ поле берёт и само
  из server.maxUploadSize);
- блок категории читает с пределами проекта (current_limits вместо
  category_limits) -> краснеют «категория: …»;
- в period_rebuild убрать except UploadTooLargeError -> «пересборка: …»; в
  _run_rebuild передавать исключение и при отказе по пределам ->
  «пересборка на странице: отказ по пределам…»;
- в tag_hierarchy_ui убрать structure_file_problem -> «структура тегов: …».
"""

import io
import logging
import os
import re
import sys
import tempfile
import tomllib
import zipfile
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"
SETTINGS = ("UPLOAD_MAX_MB", "UPLOAD_MAX_ROWS", "UPLOAD_MAX_TEXT_CHARS", "UPLOAD_MAX_COLS")
for _name in SETTINGS:
    os.environ.pop(_name, None)

import pandas as pd  # noqa: E402
import streamlit  # noqa: E402
from streamlit.runtime.secrets import Secrets  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
STORAGE: dict[str, bytes] = {}
SAVED: list = []
store.get_supabase_client = lambda: CLIENT
store.download_storage_file = lambda path: STORAGE.get(str(path), b"")
store.save_processed_tables = lambda **kwargs: SAVED.append(kwargs)

import import_adapters  # noqa: E402
from services import ingest as ingest_module  # noqa: E402
from services import period_rebuild  # noqa: E402
from services import upload_limits as limits_module  # noqa: E402
from services.ingest import IngestError, UploadTooLargeError, ingest_file_bytes, read_canonical_bytes  # noqa: E402
from services.upload_limits import (  # noqa: E402
    AUTOLOAD_ADVICE,
    CATEGORY_ADVICE,
    DEFAULT_MAX_COLS,
    DEFAULT_MAX_MB,
    DEFAULT_MAX_ROWS,
    DEFAULT_MAX_TEXT_CHARS,
    MB,
    UploadLimits,
    category_limits,
    current_limits,
    file_problem,
    format_count,
    structure_file_problem,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def secrets(values):
    """Секреты Streamlit как у приложения: тем же путём их подменяет AppTest."""
    holder = Secrets()
    holder._secrets = dict(values)
    streamlit.secrets = holder


# Локальный secrets.toml разработчика в проверки не попадает.
REAL_SECRETS = streamlit.secrets
secrets({})

# Шпионы чтения: проверки веса обязаны срабатывать до pandas, а потолок
# строк — до канонизации.
REAL_READ = ingest_module.read_source_table
READS: list = []
REAL_CANON = import_adapters.canonicalize_table
CANONS: list = []
REAL_CLEAN = import_adapters._clean_dataframe
CLEANED: list = []  # сколько строк пришло в чистку от читателя таблицы


def spy_read(*args, **kwargs):
    READS.append(args[0] if args else kwargs.get("path"))
    return REAL_READ(*args, **kwargs)


def spy_canon(*args, **kwargs):
    CANONS.append(len(args[0]) if args else 0)
    return REAL_CANON(*args, **kwargs)


def spy_clean(frame):
    CLEANED.append(len(frame))
    return REAL_CLEAN(frame)


ingest_module.read_source_table = spy_read
import_adapters.canonicalize_table = spy_canon
import_adapters._clean_dataframe = spy_clean


def forget():
    READS.clear()
    CANONS.clear()
    CLEANED.clear()


SPLIT = "Разбейте выгрузку по неделям или месяцам и загрузите частями."
HEADER = "Дата;Текст;Ссылка;Автор;Площадка"


def csv_cells(texts, extra=None):
    """Ячейки обычной выгрузки CSV (не Brand Analytics): по строке на текст.

    extra — дополнительные колонки {название: [значения]}.
    """
    extra = extra or {}
    rows = []
    for i, text in enumerate(texts):
        cells = [f"0{1 + i % 9}.04.2026 10:{i % 60:02d}", text, f"https://t.me/c/{i}", f"Автор {i % 7}", "t.me"]
        cells += [values[i] for values in extra.values()]
        rows.append(cells)
    return rows


def csv_bytes(texts, extra=None):
    head = HEADER + "".join(f";{name}" for name in (extra or {}))
    lines = [head] + [";".join(cells) for cells in csv_cells(texts, extra)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def cells_total(texts, extra=None):
    """Сколько знаков в ячейках данных (без заголовка) — столько считает предел."""
    return sum(len(cell) for cells in csv_cells(texts, extra) for cell in cells)


def attempt(data, filename="выгрузка.csv", limits=None):
    """read_canonical_bytes: (таблица, None) или (None, исключение)."""
    try:
        return read_canonical_bytes(data, filename, "auto", limits=limits), None
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
        return None, exc


def too_large(error):
    return isinstance(error, UploadTooLargeError) and isinstance(error, IngestError)


LATIN_ALLOWED = {"Excel"}


def plain_russian(text, extra_latin=()):
    """Текст для человека: без английских слов (кроме «Excel»), без трассировки."""
    latin = set(re.findall(r"[A-Za-z]+", text)) - LATIN_ALLOWED - set(extra_latin)
    return not latin and "Traceback" not in text and "Error" not in text


def mb_zip(member_bytes, *, chunk=b"<c><v>1</v></c>" * 4096, name="xl/worksheets/sheet1.xml"):
    """Zip-архив с частью ровно member_bytes байт: сжимается почти в ноль."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        with archive.open(name, "w") as handle:
            left = member_bytes
            while left > 0:
                piece = chunk[: min(len(chunk), left)]
                handle.write(piece)
                left -= len(piece)
    return buffer.getvalue()


def xlsx_bytes(frame):
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False, sheet_name="Сообщения")
    return buffer.getvalue()


def simple_frame(rows, extra_cols=0):
    data = {
        "Дата": ["01.04.2026"] * rows,
        "Сообщение": [f"Сообщение {i}" for i in range(rows)],
        "Ссылка": [f"https://x.ru/{i}" for i in range(rows)],
        "Автор": ["Автор"] * rows,
    }
    for k in range(extra_cols):
        data[f"Колонка {k}"] = ["1"] * rows
    return pd.DataFrame(data)


print("1. Пределы по умолчанию и config.toml")
limits = current_limits()
check("по умолчанию: 20 МБ, 10 000 сообщений, 8 млн знаков, 60 колонок",
      (limits.max_mb, limits.max_rows, limits.max_text_chars, limits.max_cols) == (20, 10_000, 8_000_000, 60)
      and (DEFAULT_MAX_MB, DEFAULT_MAX_ROWS, DEFAULT_MAX_TEXT_CHARS, DEFAULT_MAX_COLS)
      == (20, 10_000, 8_000_000, 60), str(limits))
check("распакованная книга — до 50 МБ", limits.max_unpacked_mb == 50, str(limits.max_unpacked_mb))
check("потолок чтения строк: вдвое выше предела", limits.scan_rows == 20_000, str(limits.scan_rows))
config = tomllib.loads((REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
check("config.toml: server.maxUploadSize совпадает с пределом веса",
      config.get("server", {}).get("maxUploadSize") == DEFAULT_MAX_MB, str(config.get("server")))

print("2. Вес файла — до pandas")
small = UploadLimits(max_mb=1, max_rows=10**6, max_text_chars=10**9)
base = csv_bytes(["Обычный текст сообщения номер " + str(i) for i in range(20)])
# Ровно 1 МБ: строки по ~300 байт, последняя добивается латиницей в тексте.
# Одним огромным полем добить нельзя: поле длиннее 131 072 знаков модуль
# csv пропускает вместе со строкой.
filler = [f"Сообщение {i} " + "x" * 250 for i in range(3400)]
while len(csv_bytes(filler)) > MB - 300:
    filler.pop()
filler[-1] += "x" * (MB - len(csv_bytes(filler)))
exact = csv_bytes(filler)
check("подготовка: файл ровно 1 МБ", len(exact) == MB, str(len(exact)))
forget()
frame, error = attempt(exact, limits=small)
check("вес: ровно на пределе — проходит",
      error is None and frame is not None and len(frame) == len(filler), f"{error!r}, строк {len(filler)}")
forget()
_frame, error = attempt(exact + b"x", limits=small)
check("вес: на байт больше предела — отказ", too_large(error), repr(error))
check("вес: отказ до pandas — файл не читался", not READS, str(READS))
text = str(error)
check("вес: текст называет вес, предел и что делать",
      "1,1 МБ" in text and "не больше 1 МБ" in text and SPLIT in text, text)
check("вес: текст простой, по-русски", plain_russian(text), text)
forget()
_frame, error = attempt(b"x" * (DEFAULT_MAX_MB * MB + 1))
check("вес по умолчанию: 20 МБ + 1 байт — отказ до pandas",
      too_large(error) and "не больше 20 МБ" in str(error) and not READS, repr(error))
check("вес по умолчанию: ровно 20 МБ — проверку веса проходит",
      file_problem(b"x" * (DEFAULT_MAX_MB * MB), current_limits()) is None)

print("3. Книга Excel: сжатый файл маленький, внутри — много")
# Ячейка Excel держит до 32 767 знаков — как в настоящей книге, текст
# статьи укладывается в 30 000. Повторы внутри текста сжимаются почти в ноль,
# а тексты строк разные: одинаковые книга хранит один раз.
repetitive = pd.DataFrame({
    "Дата": ["01.04.2026 10:00"] * 200,
    "Текст": [(f"Статья {i}. " + "Один и тот же абзац статьи повторяется снова и снова. " * 600)[:30_000]
              for i in range(200)],
    "Ссылка": [f"https://example.ru/news/{i}" for i in range(200)],
    "Автор": ["Редакция"] * 200,
    "Площадка": ["example.ru"] * 200,
})
book = xlsx_bytes(repetitive)
unpacked = limits_module.unpacked_size(book)
check("подготовка: книга весит меньше 1 МБ, внутри больше 2 МБ",
      len(book) < MB and unpacked is not None and unpacked > 2 * MB, f"{len(book)} / {unpacked}")
forget()
_frame, error = attempt(book, "статьи.xlsx", limits=small)
check("книга: внутри больше предела — отказ", too_large(error), repr(error))
check("книга: отказ до pandas", not READS, str(READS))
text = str(error)
check("книга: текст называет вес, данные внутри, предел и что делать",
      "хранит данные сжатыми" in text and "не больше 2 МБ" in text and SPLIT in text
      and limits_module.format_mb(len(book)) in text and limits_module.format_mb(unpacked) in text, text)
check("книга: текст простой, по-русски", plain_russian(text), text)
forget()
_frame, error = attempt(book, "статьи.xls", limits=small)
check("книга: то же под именем .xls — отказ (смотрится содержимое, не имя)", too_large(error) and not READS, repr(error))
check("книга: ровно 2 МБ внутри — проходит, на байт больше — отказ",
      file_problem(mb_zip(2 * MB - len("<Types/>")), small) is None
      and file_problem(mb_zip(2 * MB - len("<Types/>") + 1), small) is not None)
check("книга: не архив (CSV) — распакованный размер не считается", limits_module.unpacked_size(base) is None)
bomb = mb_zip(51 * MB)
forget()
_frame, error = attempt(bomb, "бомба.xlsx")
check("бомба по умолчанию: 51 МБ внутри файла меньше мегабайта — отказ до pandas",
      len(bomb) < MB and too_large(error) and "не больше 50 МБ" in str(error) and not READS,
      f"{len(bomb)} байт, {error!r}")

print("4. Число сообщений")
rows_limits = UploadLimits(max_mb=20, max_rows=50, max_text_chars=10**9)
texts = [f"Сообщение номер {i} про цену и качество" for i in range(51)]
frame, error = attempt(csv_bytes(texts[:50]), limits=rows_limits)
check("строки: ровно на пределе — проходит", error is None and len(frame) == 50, repr(error))
forget()
_frame, error = attempt(csv_bytes(texts), limits=rows_limits)
text = str(error)
check("строки: на одну больше — отказ", too_large(error), repr(error))
check("строки: текст называет число, предел и что делать",
      "сообщений: 51" in text and "не больше 50" in text and SPLIT in text
      and "Понадобится не меньше 2 частей." in text, text)
check("строки: текст простой, по-русски", plain_russian(text), text)
check("строки: отказ до канонизации (35 колонок на строку не строились)", not CANONS, str(CANONS))
OVER_ROWS = csv_bytes([f"Пост {i}" for i in range(DEFAULT_MAX_ROWS + 1)])
_frame, error = attempt(OVER_ROWS)
check("по умолчанию: 10 001 сообщение — отказ",
      too_large(error) and "10 001" in str(error) and "не больше 10 000" in str(error), repr(error))

print("4а. Потолок чтения: огромный файл отказывает, не будучи прочитан целиком")
# Потолок для предела в 50 строк — 1 050: читается на одну больше.
forget()
_frame, error = attempt(csv_bytes([f"Пост {i}" for i in range(1050)]), limits=rows_limits)
check("потолок: ровно на потолке — число точное", too_large(error) and "сообщений: 1 050," in str(error), repr(error))
forget()
_frame, error = attempt(csv_bytes([f"Пост {i}" for i in range(1500)]), limits=rows_limits)
text = str(error)
check("потолок: CSV длиннее — «не меньше», прочитано на одну строку больше потолка",
      too_large(error) and "сообщений: не меньше 1 051," in text and CLEANED == [1051], f"{CLEANED} {text}")
check("потолок: до канонизации не дошло", not CANONS, str(CANONS))
check("потолок: совет с числом частей и простой текст",
      "Понадобится не меньше 22 частей." in text and plain_russian(text), text)
forget()
_frame, error = attempt(xlsx_bytes(simple_frame(1500)), "длинная.xlsx", limits=rows_limits)
check("потолок: xlsx длиннее — тоже читается до потолка, без канонизации",
      too_large(error) and "не меньше 1 051" in str(error) and CLEANED == [1051] and not CANONS,
      f"{CLEANED} {CANONS} {error!r}")
forget()
frame, error = attempt(xlsx_bytes(simple_frame(40)), "малая.xlsx", limits=rows_limits)
check("потолок: файл в пределах читается целиком", error is None and len(frame) == 40 and CLEANED[0] == 40, repr(error))
# Пустые отформатированные строки внизу таблицы: заполнено мало, а потолок
# достигнут — файл не читается дальше, и человеку объясняют, что поправить.
blank_tail = "Дата;Текст;Ссылка;Автор\n" + ";;;\n" * 3000 + "".join(f"01.04.2026;Пост {i};u{i};a\n" for i in range(5))
forget()
_frame, error = attempt(blank_tail.encode("utf-8"), limits=rows_limits)
text = str(error)
check("пустые строки внизу: отказ с понятным советом",
      too_large(error) and "больше 1 050 строк" in text and "Удалите их" in text and plain_russian(text), text)
blank_few = "Дата;Текст;Ссылка;Автор\n" + ";;;\n" * 500 + "".join(f"01.04.2026;Пост {i};u{i};a\n" for i in range(20))
frame, error = attempt(blank_few.encode("utf-8"), limits=rows_limits)
check("пустые строки: немного пустых между данными не мешают", error is None and len(frame) == 20, repr(error))

print("5. Объём текста в знаках — во всех ячейках")
cyr = ["а" * 100] * 10
total = cells_total(cyr)
text_limits = UploadLimits(max_mb=20, max_rows=10**6, max_text_chars=total)
frame, error = attempt(csv_bytes(cyr), limits=text_limits)
check("текст: ровно на пределе — проходит", error is None and len(frame) == 10, f"{error!r} / нужно {total}")
bytes_total = sum(len(cell.encode("utf-8")) for cells in csv_cells(cyr) for cell in cells)
check("текст: считаются знаки, а не байты (в UTF-8 тех же ячеек больше)",
      error is None and bytes_total > total, f"{bytes_total} / {total}")
forget()
tight = UploadLimits(max_mb=20, max_rows=10**6, max_text_chars=total - 1)
_frame, error = attempt(csv_bytes(cyr), limits=tight)
text = str(error)
check("текст: на знак больше — отказ", too_large(error), repr(error))
check("текст: называет знаки, предел, причину и что делать",
      f"слишком много текста: {format_count(total)} знаков," in text
      and f"не больше {format_count(total - 1)} знаков" in text
      and "полными текстами статей СМИ" in text and SPLIT in text
      and "Понадобится не меньше 2 частей." in text, text)
check("текст: простой, по-русски", plain_russian(text), text)
check("текст: отказ до канонизации", not CANONS, str(CANONS))
check("текст: «1 знак», «2 знака», «25 знаков» и «не меньше 21 знака» склоняются как положено",
      limits_module._chars_text(1) == "1 знак" and limits_module._chars_text(1001) == "1 001 знак"
      and limits_module._chars_text(2) == "2 знака" and limits_module._chars_text(25) == "25 знаков"
      and limits_module._chars_text(21, genitive=True) == "21 знака"
      and limits_module._chars_text(22, genitive=True) == "22 знаков")

# Считаются все колонки, а не список «текстовых»: тяжёлым бывает и примечание,
# и заголовок, и колонка, которую платформа даже не узнаёт.
plain = ["а" * 50] * 10
plain_total = cells_total(plain)
caps = UploadLimits(max_mb=20, max_rows=10**6, max_text_chars=plain_total + 300)
for column, label in (("Примечание", "примечание"), ("Заголовок", "заголовок"),
                      ("Заметка аналитика", "колонка, которую платформа не узнаёт"),
                      ("Основная тема", "основная тема"), ("Распознанный текст", "распознанный текст"),
                      ("Родительский пост", "текст родительского поста")):
    extra = {column: ["б" * 100] * 10}
    _frame, error = attempt(csv_bytes(plain, extra), limits=caps)
    check(f"текст: считаются все колонки — {label}",
          too_large(error) and f"{format_count(plain_total + 1000)} знаков" in str(error), repr(error))
_frame, error = attempt(csv_bytes(plain, {"Примечание": ["б" * 20] * 10}), limits=caps)
check("текст: колонка короче запаса — проходит", error is None, repr(error))

# Поле CSV длиннее 131 072 знаков не прочтёт сам модуль csv — статьи короче.
per_row = DEFAULT_MAX_TEXT_CHARS // 200 + 500
big_text = csv_bytes([("Full text of the media article from the export. " * 900)[:per_row]] * 200)
check("подготовка: больше 8 млн знаков текста в файле меньше 20 МБ",
      len(big_text) < 20 * MB, str(len(big_text)))
_frame, error = attempt(big_text)
check("по умолчанию: больше 8 млн знаков — отказ",
      too_large(error) and "не больше 8 млн знаков" in str(error) and "Понадобится" in str(error), repr(error))

print("5а. Две причины сразу: сообщений и текста")
# 12 000 статей по 5 600 знаков в месяц: делить пополам — получить второй
# отказ, уже по тексту. Совет называет число частей на обе причины сразу.
both_limits = UploadLimits(max_mb=20, max_rows=10, max_text_chars=4000)
both_texts = ["б" * 900] * 12
_frame, error = attempt(csv_bytes(both_texts), limits=both_limits)
text = str(error)
parts = max(-(-12 // 10), -(-cells_total(both_texts) // 4000))
check("две причины: называет и сообщения, и знаки, и полные тексты",
      too_large(error) and "слишком много сообщений и текста" in text and "сообщений — 12," in text
      and "не больше 10 сообщений и 4 000 знаков" in text and "полными текстами статей СМИ" in text, text)
check("две причины: частей столько, чтобы уложились обе",
      f"Понадобится не меньше {parts} частей." in text and parts > 2, f"{parts} / {text}")
check("две причины: простой текст", plain_russian(text), text)

print("6. Секреты и окружение меняют пределы")
secrets({"UPLOAD_MAX_ROWS": 20000})
check("секреты: UPLOAD_MAX_ROWS поднимает предел", current_limits().max_rows == 20000, str(current_limits()))
frame, error = attempt(OVER_ROWS)
check("секреты: 10 001 сообщение с поднятым пределом проходит",
      error is None and len(frame) == DEFAULT_MAX_ROWS + 1, repr(error))
os.environ["UPLOAD_MAX_ROWS"] = "300"
check("секреты важнее окружения, как в services.observability", current_limits().max_rows == 20000)
secrets({})
check("окружение: UPLOAD_MAX_ROWS действует без секретов", current_limits().max_rows == 300)
os.environ["UPLOAD_MAX_MB"] = "50"
os.environ["UPLOAD_MAX_TEXT_CHARS"] = "12 000 000"
os.environ["UPLOAD_MAX_COLS"] = "120"
check("окружение: вес, текст и колонки тоже меняются (пробелы в числе не мешают)",
      (current_limits().max_mb, current_limits().max_text_chars, current_limits().max_cols,
       current_limits().max_unpacked_mb) == (50, 12_000_000, 120, 125), str(current_limits()))
LOG = []
handler = logging.Handler()
handler.emit = LOG.append
logging.getLogger("platform.upload_limits").addHandler(handler)
BAD = ("много", "0", "-5", "inf", "-inf", "Infinity", "1e400", "nan")
for bad in BAD:
    os.environ["UPLOAD_MAX_ROWS"] = bad
    try:
        got = current_limits().max_rows
    except Exception as exc:  # noqa: BLE001 — падение и есть результат проверки
        got = repr(exc)
    check(f"опечатка «{bad}» — предел по умолчанию, не снятый, не нулевой и без падения",
          got == DEFAULT_MAX_ROWS, str(got))
secrets({"UPLOAD_MAX_ROWS": float("inf")})
os.environ.pop("UPLOAD_MAX_ROWS", None)
try:
    got = current_limits().max_rows
except Exception as exc:  # noqa: BLE001
    got = repr(exc)
check("inf в секретах (число в TOML) — предел по умолчанию, без падения", got == DEFAULT_MAX_ROWS, str(got))
secrets({})
logging.getLogger("platform.upload_limits").removeHandler(handler)
check("опечатка видна владельцу в логе", len(LOG) == len(BAD) + 1 and "UPLOAD_MAX_ROWS" in LOG[0].getMessage(),
      str([r.getMessage() for r in LOG]))
for _name in SETTINGS:
    os.environ.pop(_name, None)

print("7. Колонки — по заголовку, до чтения данных")
cols_limits = UploadLimits(max_mb=20, max_rows=10**6, max_text_chars=10**9, max_cols=8)
wide_ok = csv_bytes([f"Пост {i}" for i in range(5)], {f"Тег {k}": ["Да"] * 5 for k in range(3)})
frame, error = attempt(wide_ok, limits=cols_limits)
check("колонки: ровно на пределе (8) — проходит", error is None and frame is not None, repr(error))
wide_bad = csv_bytes([f"Пост {i}" for i in range(5)], {f"Тег {k}": ["Да"] * 5 for k in range(4)})
forget()
_frame, error = attempt(wide_bad, limits=cols_limits)
text = str(error)
check("колонки: на одну больше — отказ с числом, пределом и советом",
      too_large(error) and "слишком много колонок: 9," in text and "не больше 8" in text
      and "нужные колонки" in text and plain_russian(text), text)
check("колонки: отказ до чтения данных (чистка таблицы не начиналась)", not CLEANED and not CANONS, str(CLEANED))
forget()
_frame, error = attempt(xlsx_bytes(simple_frame(5, extra_cols=5)), "широкая.xlsx", limits=cols_limits)
check("колонки: xlsx шире предела — отказ до чтения данных листа",
      too_large(error) and "слишком много колонок: 9," in str(error) and not CLEANED, f"{CLEANED} {error!r}")
frame, error = attempt(xlsx_bytes(simple_frame(5, extra_cols=4)), "норма.xlsx", limits=cols_limits)
check("колонки: xlsx ровно на пределе — проходит", error is None and frame is not None, repr(error))
header_101 = ";".join(["Дата", "Сообщение", "Ссылка", "Автор"] + [f"К{k}" for k in range(DEFAULT_MAX_COLS - 3)])
row_101 = ";".join(["01.04.2026", "пост", "u", "a"] + ["1"] * (DEFAULT_MAX_COLS - 3))
_frame, error = attempt(f"{header_101}\n{row_101}\n".encode("utf-8"))
check("колонки по умолчанию: на одну больше предела — отказ",
      too_large(error) and f"колонок: {DEFAULT_MAX_COLS + 1}," in str(error), repr(error))

print("8. Воркер автозагрузки: тот же отказ")
import ingest_worker  # noqa: E402
import ingest_admin_ui  # noqa: E402
from services import ingest_queue as queue  # noqa: E402

REPORTED = []
ingest_worker.report_failure = lambda where, exc=None, **_ctx: REPORTED.append((where, exc)) or False
os.environ["UPLOAD_MAX_ROWS"] = "50"
page_text = str(attempt(csv_bytes(texts))[1])
expected = f"{page_text} {AUTOLOAD_ADVICE}"
queue.upsert_source(source_key="ba-weekly", project_id="limits_project", title="BA", source_system="auto")
STORAGE["inbox/ba-weekly/big.csv"] = csv_bytes(texts)
task_id = queue.enqueue_task(storage_path="inbox/ba-weekly/big.csv", source_key="ba-weekly",
                             original_filename="big.csv", file_sha256="hash-big")
task = queue.get_task(task_id)
try:
    ingest_worker.process_task(task, tempfile.mkdtemp(prefix="limits_worker_"))
    worker_error = None
except Exception as exc:  # noqa: BLE001 — исключение и есть результат проверки
    worker_error = exc
check("воркер: тот же UploadTooLargeError, текст как на странице плюс совет автозагрузки",
      too_large(worker_error) and str(worker_error) == expected, f"{worker_error!r} / {expected}")
check("воркер: совет автозагрузки простой, по-русски, без «разбейте и загрузите» впустую",
      "повторять её бесполезно" in str(worker_error) and "«Загрузка файла»" in str(worker_error)
      and plain_russian(AUTOLOAD_ADVICE), AUTOLOAD_ADVICE)
code = ingest_worker.run(Namespace(max_tasks=3, once=False, work_dir=tempfile.mkdtemp(prefix="limits_worker_"),
                                   worker_id="test", requeue_stale_minutes=45, no_fail_on_error=False, verbose=False))
done = queue.get_task(task_id)
check("воркер: задача сразу в «Ошибке», без повторов, текст для человека",
      done["status"] == "error" and done["attempts"] == 1 and done["error_message"] == expected,
      f'{done["status"]}, попыток {done["attempts"]}, {str(done.get("error_message"))[:120]}')
check("воркер: период не создан, код 1", not SAVED and code == 1, f"{len(SAVED)}, код {code}")

# Очередь в интерфейсе («Обработать очередь сейчас») идёт через тот же путь.
try:
    ingest_admin_ui._process_task_in_ui(queue.get_task(task_id), tempfile.mkdtemp(prefix="limits_queue_"))
    ui_error = None
except Exception as exc:  # noqa: BLE001
    ui_error = exc
check("очередь в интерфейсе: тот же отказ с советом автозагрузки",
      too_large(ui_error) and str(ui_error) == expected, repr(ui_error))
try:
    # work_dir во временной папке: если отказа не будет (проверка сломана),
    # результат не должен попасть в data/platform репозитория.
    ingest_file_bytes(csv_bytes(texts), project_id="limits_project", source_filename="big.csv",
                      work_dir=tempfile.mkdtemp(prefix="limits_direct_"))
    direct = None
except Exception as exc:  # noqa: BLE001
    direct = exc
check("ingest_file_bytes: отказ с советом автозагрузки", too_large(direct) and str(direct) == expected, repr(direct))
os.environ.pop("UPLOAD_MAX_ROWS", None)

long_plain = "Файл слишком большой: " + "очень " * 80 + "конец совета."
trace = "ValueError: x\nTraceback (most recent call last):\n  File \"a.py\", line 1\n" + "x" * 400
check("комментарий задачи: понятный текст целиком, не обрезается на полуслове",
      ingest_admin_ui._task_comment(long_plain, None, is_admin=False) == long_plain
      and ingest_admin_ui._task_comment(expected, None, is_admin=False).endswith("рассылку отчёта."))
check("комментарий задачи: трассировка владельцу обрезана",
      len(ingest_admin_ui._task_comment(trace, None, is_admin=True)) == ingest_admin_ui.TASK_ERROR_PREVIEW_CHARS)

print("9. Блок «Выгрузка по категории»: свои строки и знаки, без совета «частями»")
cat = category_limits(UploadLimits())
check("категория: вес и колонки те же, строк и знаков больше, частями нельзя",
      (cat.max_mb, cat.max_cols) == (DEFAULT_MAX_MB, DEFAULT_MAX_COLS)
      and cat.max_rows >= 30_000 and cat.max_text_chars >= 20_000_000 and not cat.can_split, str(cat))
raised = category_limits(UploadLimits(max_rows=100_000, max_text_chars=50_000_000))
check("категория: если владелец поднял пределы проекта выше, категория не строже",
      (raised.max_rows, raised.max_text_chars) == (100_000, 50_000_000), str(raised))
frame, error = attempt(OVER_ROWS, limits=category_limits(UploadLimits()))
check("категория: 10 001 строка проходит (у проекта — отказ)", error is None and len(frame) == DEFAULT_MAX_ROWS + 1, repr(error))
real_min_rows = limits_module.CATEGORY_MIN_ROWS
limits_module.CATEGORY_MIN_ROWS = 40
cat_small = category_limits(UploadLimits(max_rows=40))
_frame, error = attempt(csv_bytes(texts), limits=cat_small)
text = str(error)
check("категория: отказ без совета «загрузите частями» — следующая часть заменит предыдущую",
      too_large(error) and CATEGORY_ADVICE in text and "загрузите частями" not in text
      and "Понадобится" not in text and plain_russian(text), text)
_frame, error = attempt(b"x" * (DEFAULT_MAX_MB * MB + 1), limits=cat_small)
check("категория: и вес файла — без совета «частями»",
      too_large(error) and CATEGORY_ADVICE in str(error) and "загрузите частями" not in str(error), repr(error))
limits_module.CATEGORY_MIN_ROWS = real_min_rows
hint_cat = limits_module.upload_hint(category_limits(UploadLimits()))
check("категория: подпись предупреждает, что части заменят друг друга",
      "следующая часть заменит предыдущую" in hint_cat and "по неделям" not in hint_cat and "20 МБ" in hint_cat, hint_cat)

print("10. Пересборка периода из файла больше пределов")
rebuild_project = "limits_project"
period_row = {"period_id": "p_big", "period_name": "Большой", "source_filename": "big.csv",
              "manifest": {"storage_path": "inbox/big/rebuild.csv", "source_system": "auto"}}
real_period_row = period_rebuild._period_row
period_rebuild._period_row = lambda project_id, period_id: period_row
STORAGE["inbox/big/rebuild.csv"] = csv_bytes(texts)
os.environ["UPLOAD_MAX_ROWS"] = "50"
try:
    period_rebuild.rebuild_period(rebuild_project, "p_big", work_dir=tempfile.mkdtemp(prefix="limits_rb_"))
    rb_error = None
except Exception as exc:  # noqa: BLE001
    rb_error = exc
text = str(rb_error)
check("пересборка: RebuildError вместо общего отказа «загрузите частями»",
      isinstance(rb_error, period_rebuild.RebuildError) and isinstance(rb_error.__cause__, UploadTooLargeError), repr(rb_error))
check("пересборка: текст говорит, что пересобрать нельзя, и про удаление старого периода",
      "пересобрать его нельзя" in text and "удалите этот период" in text and "сообщений: 51" in text
      and plain_russian(text), text)
period_rebuild._period_row = real_period_row
os.environ.pop("UPLOAD_MAX_ROWS", None)

print("11. Структура тегов: большая книга останавливается до чтения")
check("структура тегов: маленький файл проходит", structure_file_problem(b"tag,tier,parent\nA,1,\n") is None)
bomb_structure = mb_zip(25 * MB)
text = structure_file_problem(bomb_structure) or ""
check("структура тегов: книга меньше мегабайта с 25 МБ внутри — отказ с понятным текстом",
      len(bomb_structure) < MB and "слишком большой" in text and "не больше 2 МБ" in text
      and plain_russian(text, extra_latin={"tag", "tier", "parent"}), text)
check("структура тегов: файл больше 2 МБ — отказ", structure_file_problem(b"x" * (2 * MB + 1)) is not None
      and structure_file_problem(b"x" * (2 * MB)) is None)

print("12. Страницы «Загрузка файла» и «Выгрузка по категории»")
from streamlit.testing.v1 import AppTest  # noqa: E402

import upload_history_ui as ui  # noqa: E402
from error_ui import DETAILS_KEY  # noqa: E402

PROCESSED = []
REAL_PROCESS = ui.process_canonical


def fake_process(*_args, **kwargs):
    PROCESSED.append(kwargs)
    return {"period_name": "Апрельская неделя", "messages": 50, "events": 0, "storage_error": ""}


ui.process_canonical = fake_process


def texts_on(at):
    out = []
    for group in (at.error, at.warning, at.info, at.success, at.caption, at.markdown, at.exception):
        out.extend(str(getattr(el, "value", "")) for el in group)
    out.extend(str(getattr(el, "label", "")) for el in at.expander)
    return out


def with_secrets(at, page_secrets):
    # Непустые секреты AppTest подставляет вместо st.secrets на время прогона:
    # так пределы читаются настоящим путём, а локальный secrets.toml не
    # участвует. Ключ-заглушка — чтобы подмена была и без пределов.
    at.secrets["PLATFORM_TEST_PLACEHOLDER"] = "1"
    for key, value in (page_secrets or {}).items():
        at.secrets[key] = value


def open_page(role, page_secrets=None, details=False):
    at = AppTest.from_file(str(REPO / "tests" / "pages_app.py"), default_timeout=180)
    with_secrets(at, page_secrets)
    at.session_state["page_name"] = "upload"
    at.session_state["page_role"] = role
    at.session_state["page_project_id"] = "limits_project"
    at.session_state["page_work_dir"] = tempfile.mkdtemp(prefix="limits_page_")
    if details:
        at.session_state[DETAILS_KEY] = True
    at.run()
    return at


def upload(at, data, filename="выгрузка.csv"):
    uploaders = [u for u in at.file_uploader if "CSV или Excel" in str(u.label)]
    buttons = [b for b in at.button if "Обработать и сохранить" in str(b.label)]
    if not uploaders or not buttons:
        return "нет поля загрузки или кнопки"
    uploaders[0].set_value((filename, data, "text/csv"))
    buttons[0].click().run()
    return ""


def category_page():
    # Страница блока категории: тело функции исполняет AppTest, поэтому
    # импорты внутри.
    import pandas as pd

    from brand_metrics_ui import render_category_upload

    periods = pd.DataFrame([{"period_id": "p1", "period_name": "Апрель"}])
    render_category_upload("limits_project", periods, ["p1"], True, read_only=False)


def rebuild_page_too_large():
    # Пересборка на странице «История периодов»: период загружен из файла
    # больше нынешних пределов.
    import upload_history_ui as ui_mod
    from services.ingest import UploadTooLargeError
    from services.period_rebuild import RebuildError

    def fake_rebuild(project_id, period_id, work_dir=None):
        try:
            raise UploadTooLargeError("Файл слишком большой.")
        except UploadTooLargeError as cause:
            raise RebuildError("Этот период загружен из файла больше нынешних пределов.") from cause

    ui_mod.rebuild_period = fake_rebuild
    ui_mod._run_rebuild("limits_project", ["p1"], "/tmp")


def rebuild_page_other_failure():
    # Контроль: другой отказ пересборки владелец видит с подробностями.
    import upload_history_ui as ui_mod
    from services.period_rebuild import RebuildError

    def fake_rebuild(project_id, period_id, work_dir=None):
        raise RebuildError("Исходный файл периода не найден в хранилище.")

    ui_mod.rebuild_period = fake_rebuild
    ui_mod._run_rebuild("limits_project", ["p1"], "/tmp")


REAL_REBUILD = ui.rebuild_period

try:
    at = open_page("editor")
    captions = [str(c.value) for c in at.caption]
    hint = [c for c in captions if "За один раз" in c]
    check("страница: подпись с пределами по-русски",
          len(hint) == 1 and "до 20 МБ" in hint[0] and "до 10 000 сообщений" in hint[0]
          and "до 60 колонок" in hint[0]
          and "до 8 млн знаков текста" in hint[0] and "по неделям или месяцам" in hint[0]
          and "полных статей СМИ" in hint[0], str(captions))
    fields = [u for u in at.file_uploader if "CSV или Excel" in str(u.label)]
    check("страница: у поля предел 20 МБ",
          bool(fields) and fields[0].proto.max_upload_size_mb == 20,
          str(fields[0].proto.max_upload_size_mb if fields else "нет поля"))

    # Предел ниже серверного у поля свой: без него поле пускало бы файл до
    # server.maxUploadSize, а подпись обещала бы меньше.
    at = open_page("editor", {"UPLOAD_MAX_MB": 5})
    fields = [u for u in at.file_uploader if "CSV или Excel" in str(u.label)]
    hint = [str(c.value) for c in at.caption if "За один раз" in str(c.value)]
    check("страница: предел веса из секретов — и у поля, и в подписи 5 МБ",
          bool(fields) and fields[0].proto.max_upload_size_mb == 5 and bool(hint) and "до 5 МБ" in hint[0],
          f"{fields[0].proto.max_upload_size_mb if fields else 'нет поля'} / {hint}")

    # Файл тяжелее предела поле не принимает вовсе, и со стороны страницы это
    # «файл не выбран»: сообщение должно подсказать причину.
    PROCESSED.clear()
    at = open_page("editor")
    buttons = [b for b in at.button if "Обработать и сохранить" in str(b.label)]
    buttons[0].click().run()
    errors = [str(e.value) for e in at.error]
    check("страница: без файла — не просто «Загрузите файл», а про предел поля",
          len(errors) == 1 and "Файл не выбран" in errors[0] and "тяжелее 20 МБ" in errors[0]
          and "по неделям или месяцам" in errors[0] and plain_russian(errors[0]), str(errors))

    PROCESSED.clear()
    at = open_page("editor", {"UPLOAD_MAX_ROWS": 50})
    hint = [str(c.value) for c in at.caption if "За один раз" in str(c.value)]
    check("страница: подпись с пределами из секретов", bool(hint) and "до 50 сообщений" in hint[0], str(hint))
    problem = upload(at, csv_bytes(texts))
    errors = [str(e.value) for e in at.error]
    shown = texts_on(at)
    check("страница: отработала без исключения", not problem and not at.exception, problem or str(at.exception))
    check("страница: аналитик видит, что велико, предел и что делать",
          errors == [page_text], str(errors))
    check("страница: без общего «не удалось прочитать файл»",
          not any("Не удалось прочитать файл" in t or "лист/таблицу" in t for t in shown), str(shown))
    check("страница: без трассировки и подробностей ошибки",
          not any("Traceback" in t or "UploadTooLargeError" in t or "Подробности ошибки" in t for t in shown),
          str(shown))
    check("страница: обработка не запускалась", not PROCESSED, str(len(PROCESSED)))

    at = open_page("owner", {"UPLOAD_MAX_ROWS": 50}, details=True)
    upload(at, csv_bytes(texts))
    check("страница: владельцу тот же текст, без раскрывашки с исключением",
          [str(e.value) for e in at.error] == [page_text]
          and not any("Подробности ошибки" in str(e.label) for e in at.expander),
          str([str(e.value) for e in at.error]))

    wide_file = csv_bytes(["Пост 1", "Пост 2"], {"Тег": ["Да", "Да"], "Тег 2": ["Да", "Да"]})
    expected_wide = str(attempt(wide_file, limits=UploadLimits(max_cols=6))[1])
    at = open_page("editor", {"UPLOAD_MAX_COLS": 6})
    upload(at, wide_file)
    check("страница: слишком широкая таблица — тот же понятный отказ без трассировки",
          "слишком много колонок" in expected_wide and [str(e.value) for e in at.error] == [expected_wide]
          and not at.exception, str([str(e.value) for e in at.error]))

    PROCESSED.clear()
    at = open_page("editor", {"UPLOAD_MAX_ROWS": 50})
    upload(at, csv_bytes(texts[:50]))
    successes = [str(s.value) for s in at.success]
    check("страница: файл в пределах уходит в обработку", len(PROCESSED) == 1 and not at.error,
          str([str(e.value) for e in at.error]))
    check("страница: в сообщении об успехе — имя файла", any("Файл «выгрузка.csv» прочитан: 50 строк" in s for s in successes),
          str(successes))

    # Структура тегов: книга-«бомба» останавливается до чтения.
    at = open_page("editor")
    structure = [u for u in at.file_uploader if "структуры тегов" in str(u.label)]
    if structure:
        structure[0].set_value(("структура.xlsx", bomb_structure, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        at.run()
    errors = [str(e.value) for e in at.error]
    check("структура тегов: страница показывает отказ без трассировки",
          bool(structure) and not at.exception and len(errors) == 1 and "Файл структуры тегов слишком большой" in errors[0]
          and not any("Не удалось прочитать файл структуры" in e for e in errors), str(errors))

    # Блок категории: подпись и отказ без совета «частями».
    at = AppTest.from_function(category_page, default_timeout=180)
    with_secrets(at, {"UPLOAD_MAX_COLS": 6})
    at.run()
    hint = [str(c.value) for c in at.caption if "За один раз" in str(c.value)]
    fields = [u for u in at.file_uploader if "по всей категории" in str(u.label)]
    check("категория: подпись про одну загрузку и замену частей, у поля предел 20 МБ",
          not at.exception and len(hint) == 1 and "следующая часть заменит предыдущую" in hint[0]
          and bool(fields) and fields[0].proto.max_upload_size_mb == 20, f"{at.exception} {hint}")
    if fields:
        fields[0].set_value(("категория.csv", csv_bytes(["Пост 1"], {"Тег": ["Да"], "Тег 2": ["Да"]}), "text/csv"))
        at.run()
    errors = [str(e.value) for e in at.error]
    check("категория: широкая таблица отклонена понятным текстом, без трассировки",
          not at.exception and len(errors) == 1 and "слишком много колонок" in errors[0]
          and "загрузите частями" not in errors[0], f"{at.exception} {errors}")
    # История периодов: отказ пересборки по пределам — не сбой, владельцу
    # раскрывашка с трассировкой не нужна (другой отказ — она есть).
    for function, label, expect_details in (
        (rebuild_page_too_large, "пересборка на странице: отказ по пределам", False),
        (rebuild_page_other_failure, "пересборка на странице: контроль — другой отказ", True),
    ):
        at = AppTest.from_function(function, default_timeout=60)
        at.session_state[DETAILS_KEY] = True
        at.run()
        details = [e for e in at.expander if "Подробности ошибки" in str(e.label)]
        # Трассировка в раскрывашке — это тоже элемент «исключение»: без неё их нет.
        check(f"{label}: владельцу {'есть' if expect_details else 'нет'} раскрывашки с трассировкой",
              len(at.error) == 1 and bool(details) == expect_details and bool(at.exception) == expect_details,
              f"{len(at.exception)} {[str(e.value) for e in at.error]} {len(details)}")
finally:
    ui.process_canonical = REAL_PROCESS
    ui.rebuild_period = REAL_REBUILD
    streamlit.secrets = REAL_SECRETS
    ingest_module.read_source_table = REAL_READ
    import_adapters.canonicalize_table = REAL_CANON
    import_adapters._clean_dataframe = REAL_CLEAN

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Слишком большая выгрузка останавливается до обработки понятным текстом.")
