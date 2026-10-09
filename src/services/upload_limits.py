# -*- coding: utf-8 -*-
"""Пределы одной загрузки выгрузки: вес файла, число сообщений, колонок, знаков.

Зачем. Платформа на Streamlit Community Cloud — один процесс на всех
клиентов, памяти у него около гигабайта, и само приложение занимает из них
190–260 МБ. Загрузка разбирает файл в этом же процессе, и до версии 4.12.9 её
ничто не ограничивало: выгрузка полных текстов статей СМИ (3 000 статей по
11 000 знаков) занимала около гигабайта, и одна такая загрузка
перезапускала приложение у всех, кто в нём работал.

Память загрузки зависит прежде всего от длины текстов, а не от числа строк.
Замер настоящего пути (read_canonical_bytes → process_canonical, свежий
процесс, раскладка Brand Analytics; цифры — README, «Версия 4.12.9») дал пик
примерно 256 МБ + 9 МБ на каждую тысячу сообщений + 23 МБ на каждый миллион
знаков в ячейках, а широкая таблица добавляет 1–2 МБ на каждую колонку тегов
(на 10 000 сообщений); из 256 МБ 176 — сами библиотеки, они у приложения и так
загружены. Пределы ниже держат пик одной загрузки в 500–560 МБ даже на всех
пределах сразу (обычная выгрузка на пределах — 498 МБ, самая тяжёлая
допустимая — 557 МБ) — место остаётся и самому приложению, и второму
пользователю.

Проверки идут от дешёвой к дорогой, и все — до тяжёлой обработки:
1. вес файла — ещё до pandas (на странице загрузки такой файл раньше
   останавливает сам Streamlit: server.maxUploadSize в .streamlit/config.toml);
2. книга Excel — это zip-архив: текст в ней сжат в несколько раз, и файл в
   5 МБ разворачивается при чтении в сотню мегабайт. Поэтому до pandas
   смотрится оглавление архива — сколько данных внутри в распакованном виде.
   Сами данные при этом не распаковываются. Само чтение книги занимает на
   пике до 7–8 байт памяти на байт распакованных данных, отсюда предел;
3. ширина таблицы — по заголовку CSV или предпросмотру листа, до чтения
   данных: каждая колонка тегов Brand Analytics переживает всю обработку;
4. число сообщений — таблица читается не дальше потолка строк (scan_rows), и
   файл с сотнями тысяч мелких строк отказывает на потолке, а не после
   разбора целиком (минуты и гигабайт);
5. объём текста — знаки во всех ячейках прочитанной таблицы, ещё до сборки
   канонических колонок. Считаются все колонки, а не только «Сообщение»: тяжёлым
   бывает и «Примечание», и заголовок, и любая колонка, которую платформа
   потом сохранит. Байты здесь не годятся: кириллица в UTF-8 занимает два
   байта на знак, а в памяти — по-разному.

Владелец может изменить пределы секретами Streamlit или переменными окружения
UPLOAD_MAX_MB, UPLOAD_MAX_ROWS, UPLOAD_MAX_TEXT_CHARS, UPLOAD_MAX_COLS —
например, поднять их воркеру автозагрузки на GitHub Actions, где у задачи своя
машина и памяти больше. На Streamlit Cloud поднятый предел снова открывает
дорогу загрузке, которая перезапустит приложение у всех.
"""

from __future__ import annotations

import io
import logging
import math
import os
import zipfile
from dataclasses import dataclass, replace

import pandas as pd

from .formatting import plural

LOGGER = logging.getLogger("platform.upload_limits")

MB = 1024 * 1024

# Значения по умолчанию — из замера (см. docstring модуля). Вес файла должен
# совпадать с server.maxUploadSize в .streamlit/config.toml: это проверяет
# tests/test_upload_limits.py.
DEFAULT_MAX_MB = 20
DEFAULT_MAX_ROWS = 10_000
DEFAULT_MAX_TEXT_CHARS = 8_000_000
DEFAULT_MAX_COLS = 60

# Распакованная книга Excel — не больше двух с половиной пределов веса файла
# (50 МБ при 20 МБ). Чтение книги держит в памяти около семи копий данных: на
# длинных текстах (Excel хранит кириллицу в два байта на знак, в памяти тоже
# два) это до 7,6 байт на байт распакованных данных — замер книги в 52,5 МБ с
# латинским текстом: пик 574 МБ ещё до отказа по знакам. Отсюда 50 МБ: чтение
# на пике около 550 МБ. Честная выгрузка на пределах по строкам и знакам
# распаковывается в 28 МБ (книга, записанная openpyxl, хранит кириллицу в
# семь байт на знак и разворачивается вдвое больше, но читается дешевле: 2 байта
# памяти на байт) и проходит.
UNPACKED_TO_FILE_RATIO = 2.5

# Сколько строк сверх предела читается ради точного ответа «сколько их». Файл,
# в котором строк больше, читается до потолка и отказывает «не меньше N»:
# число точнее знать незачем, а читать дальше — значит держать в памяти то, что
# всё равно не принимается. Потолок вдвое выше предела, но не ниже тысячи
# строк сверх него: пустые отформатированные строки в конце листа Excel — не
# редкость, и честный файл с ними не должен упираться в потолок.
MIN_SCAN_MARGIN_ROWS = 1000

SETTING_MAX_MB = "UPLOAD_MAX_MB"
SETTING_MAX_ROWS = "UPLOAD_MAX_ROWS"
SETTING_MAX_TEXT_CHARS = "UPLOAD_MAX_TEXT_CHARS"
SETTING_MAX_COLS = "UPLOAD_MAX_COLS"

SPLIT_ADVICE = "Разбейте выгрузку по неделям или месяцам и загрузите частями."
FULL_TEXTS_HINT = "Обычно так бывает с полными текстами статей СМИ."
# Данные категории записываются к периоду одним файлом, и следующая часть
# заменила бы предыдущую (category_store.save_benchmark): совет «загрузите
# частями» тут вредил бы тихой потерей данных.
CATEGORY_ADVICE = (
    "Данные категории записываются к периоду одним файлом, поэтому частями их "
    "загрузить нельзя: следующая часть заменит предыдущую. Оставьте в файле "
    "нужный период и только нужные колонки."
)
# Дописывается к отказу для файла автозагрузки (services.ingest.ingest_file_bytes).
AUTOLOAD_ADVICE = (
    "Это задача автозагрузки, и повторять её бесполезно: файл от этого меньше "
    "не станет. Выгрузите отчёт из системы мониторинга частями и загрузите их "
    "на странице «Загрузка файла» — или настройте в системе более частую "
    "рассылку отчёта."
)
COLUMNS_ADVICE = (
    "Оставьте в файле только нужные колонки (например, уберите пустые колонки "
    "с тегами) и загрузите его снова."
)

# Пределы блока «Выгрузка по категории»: файл читается так же, но дальше не
# идёт сборка таблиц и запись сообщений — только подсчёт по брендам, так что
# строк и знаков можно больше (замер — README, «Версия 4.12.9»).
CATEGORY_MIN_ROWS = 30_000
CATEGORY_MIN_TEXT_CHARS = 20_000_000

# Файл структуры тегов — таблица в несколько сотен строк: килобайты.
STRUCTURE_MAX_MB = 2
STRUCTURE_MAX_UNPACKED_MB = 20


@dataclass(frozen=True)
class UploadLimits:
    """Пределы одной загрузки. Значения — целые положительные числа."""

    max_mb: int = DEFAULT_MAX_MB
    max_rows: int = DEFAULT_MAX_ROWS
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    max_cols: int = DEFAULT_MAX_COLS
    # Можно ли советовать «загрузите частями». Для выгрузки проекта да, для
    # категории — нет (CATEGORY_ADVICE).
    can_split: bool = True

    @property
    def max_bytes(self) -> int:
        return self.max_mb * MB

    @property
    def max_unpacked_mb(self) -> int:
        return int(self.max_mb * UNPACKED_TO_FILE_RATIO)

    @property
    def max_unpacked_bytes(self) -> int:
        return self.max_unpacked_mb * MB

    @property
    def scan_rows(self) -> int:
        """Потолок чтения: сколько строк файла читается ради проверки."""
        return self.max_rows + max(self.max_rows, MIN_SCAN_MARGIN_ROWS)

    @property
    def advice(self) -> str:
        return SPLIT_ADVICE if self.can_split else CATEGORY_ADVICE


def _setting(name: str) -> str:
    """Значение из секретов Streamlit или окружения — как в services.observability.

    Воркеру автозагрузки Streamlit не ставится (requirements-worker.txt), а
    без файла секретов st.secrets бросает исключение — ни то ни другое не
    повод падать: тогда читается окружение.
    """
    try:
        import streamlit as st

        value = st.secrets.get(name)  # type: ignore[attr-defined]
        if value not in (None, ""):
            return str(value).strip()
    except Exception:  # noqa: BLE001 — секретов или самого Streamlit может не быть
        pass
    return str(os.environ.get(name) or "").strip()


def _positive_int(name: str, default: int) -> int:
    raw = _setting(name)
    if not raw:
        return default
    try:
        value = int(float(raw.replace(" ", "").replace("_", "")))
    except (ValueError, OverflowError):
        # OverflowError — «inf» и «1e400»: их легко записать с мыслью «без
        # предела», а упасть на этом должна не страница загрузки.
        value = 0
    if value <= 0:
        # Опечатка в секрете не должна ни снять предел, ни закрыть загрузку.
        LOGGER.warning(
            "%s=%r — не положительное число, действует предел по умолчанию %s",
            name,
            raw,
            default,
        )
        return default
    return value


def current_limits() -> UploadLimits:
    """Пределы с учётом секретов и окружения; читаются при каждом вызове."""
    return UploadLimits(
        max_mb=_positive_int(SETTING_MAX_MB, DEFAULT_MAX_MB),
        max_rows=_positive_int(SETTING_MAX_ROWS, DEFAULT_MAX_ROWS),
        max_text_chars=_positive_int(SETTING_MAX_TEXT_CHARS, DEFAULT_MAX_TEXT_CHARS),
        max_cols=_positive_int(SETTING_MAX_COLS, DEFAULT_MAX_COLS),
    )


def category_limits(limits: UploadLimits | None = None) -> UploadLimits:
    """Пределы для файла категории: те же вес и колонки, строк и знаков больше.

    Не ниже пределов выгрузки проекта: если владелец их поднял, категория
    не должна оказаться строже.
    """
    limits = limits or current_limits()
    return replace(
        limits,
        max_rows=max(limits.max_rows, CATEGORY_MIN_ROWS),
        max_text_chars=max(limits.max_text_chars, CATEGORY_MIN_TEXT_CHARS),
        can_split=False,
    )


def format_count(value: int) -> str:
    """12345 → «12 345», ровные миллионы — «5 млн»."""
    value = int(value)
    if value >= 1_000_000 and value % 1_000_000 == 0:
        return f"{value // 1_000_000} млн"
    return f"{value:,}".replace(",", " ")


def format_mb(size_bytes: int) -> str:
    """Мегабайты с одним знаком, округление вверх: файл на байт больше
    предела в 20 МБ не должен называться «20,0 МБ»."""
    tenths = math.ceil(size_bytes * 10 / MB)
    whole, part = divmod(tenths, 10)
    return f"{whole},{part}"


def unpacked_size(file_bytes: bytes) -> int | None:
    """Сколько займут данные zip-архива (xlsx/xlsm) в распакованном виде.

    Читается только оглавление архива: размеры частей записаны в нём, а
    zipfile, которым книгу потом читает openpyxl, больше записанного не
    распакует. Для не-архива (csv, xls) — None. Архив с мусором впереди
    zipfile тоже открывает — как и при чтении книги.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            return sum(max(0, int(item.file_size)) for item in archive.infolist())
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
        return None


def text_chars(frame: pd.DataFrame) -> int:
    """Знаков во всех ячейках таблицы.

    Считаются все колонки, а не список «текстовых»: платформа сохраняет
    «Примечание», темы, объекты, теги и ещё десятки колонок, и в любой из них
    выгрузка может принести тысячи знаков на строку. Пределы выбраны по этой
    сумме (замер — README, «Версия 4.12.9»): обычная выгрузка Brand Analytics
    несёт кроме текста сообщения ещё около 240 знаков на строку — ссылки,
    авторы, площадки, — и они уже учтены в пределе.
    """
    total = 0
    for position in range(frame.shape[1]):
        column = frame.iloc[:, position]
        total += int(column.fillna("").astype(str).str.len().sum())
    return total


def file_problem(file_bytes: bytes, limits: UploadLimits) -> str | None:
    """Что не так с весом файла — текст для человека; None, если всё в пределах."""
    size = len(file_bytes)
    if size > limits.max_bytes:
        LOGGER.info("Выгрузка отклонена: файл %s байт, предел %s МБ", size, limits.max_mb)
        return (
            f"Файл слишком большой: {format_mb(size)} МБ, а за один раз можно "
            f"загрузить не больше {limits.max_mb} МБ. {limits.advice}"
        )
    unpacked = unpacked_size(file_bytes)
    if unpacked is not None and unpacked > limits.max_unpacked_bytes:
        LOGGER.info(
            "Выгрузка отклонена: архив %s байт, распакованных %s байт, предел %s МБ",
            size,
            unpacked,
            limits.max_unpacked_mb,
        )
        return (
            f"В файле Excel слишком много данных: сам файл весит {format_mb(size)} МБ, "
            f"но Excel хранит данные сжатыми, а без сжатия их {format_mb(unpacked)} МБ. "
            f"За один раз можно загрузить не больше {limits.max_unpacked_mb} МБ "
            f"данных. {limits.advice}"
        )
    return None


def width_problem(columns: int, limits: UploadLimits) -> str | None:
    """Слишком много колонок — текст для человека."""
    if columns <= limits.max_cols:
        return None
    LOGGER.info("Выгрузка отклонена: колонок %s, предел %s", columns, limits.max_cols)
    return (
        f"В таблице слишком много колонок: {format_count(columns)}, а за один "
        f"раз можно загрузить не больше {format_count(limits.max_cols)}. "
        f"{COLUMNS_ADVICE}"
    )


def table_problem(
    table: pd.DataFrame, limits: UploadLimits, *, complete: bool = True
) -> str | None:
    """Что не так с прочитанной таблицей — колонки, сообщения или текст.

    complete=False — файл длиннее потолка чтения (limits.scan_rows), прочитано
    только его начало: числа тогда нижние границы («не меньше»).
    """
    problem = width_problem(table.shape[1], limits)
    if problem:
        return problem
    rows = len(table)
    chars = text_chars(table)
    too_many_rows = rows > limits.max_rows
    too_much_text = chars > limits.max_text_chars
    if not (too_many_rows or too_much_text):
        if complete:
            return None
        # Потолок строк достигнут, а заполненных строк в пределах: внизу
        # таблицы пустые отформатированные строки.
        LOGGER.info(
            "Выгрузка отклонена: прочитано %s строк (потолок), заполнено %s",
            limits.scan_rows,
            rows,
        )
        return (
            f"В файле больше {format_count(limits.scan_rows)} строк, а заполнена "
            f"из них только часть: внизу таблицы, судя по всему, остались пустые "
            f"строки. Удалите их и загрузите файл снова."
        )

    LOGGER.info(
        "Выгрузка отклонена: строк %s (предел %s), знаков %s (предел %s), полностью прочитана: %s",
        rows,
        limits.max_rows,
        chars,
        limits.max_text_chars,
        complete,
    )
    at_least = "" if complete else "не меньше "
    if too_many_rows and too_much_text:
        what = (
            f"В выгрузке слишком много сообщений и текста: сообщений — "
            f"{at_least}{format_count(rows)}, знаков — {at_least}{format_count(chars)}, "
            f"а за один раз можно загрузить не больше {format_count(limits.max_rows)} "
            f"сообщений и {_chars_text(limits.max_text_chars)}."
        )
    elif too_many_rows:
        what = (
            f"В выгрузке слишком много сообщений: {at_least}{format_count(rows)}, а за "
            f"один раз можно загрузить не больше {format_count(limits.max_rows)}."
        )
    else:
        what = (
            f"В выгрузке слишком много текста: {at_least}{_chars_text(chars, genitive=not complete)}, "
            f"а за один раз можно загрузить не больше {_chars_text(limits.max_text_chars)}."
        )
    hint = f" {FULL_TEXTS_HINT}" if too_much_text else ""
    return f"{what}{hint} {_advice_with_parts(rows, chars, limits)}"


def _advice_with_parts(rows: int, chars: int, limits: UploadLimits) -> str:
    """Совет разбить выгрузку — с числом частей, если их больше одной.

    Частей нужно столько, чтобы в каждой уложились и сообщения, и знаки: если
    называть только то, что отказало первым, аналитик делит файл пополам и
    получает второй отказ — по тексту.
    """
    if not limits.can_split:
        return limits.advice
    parts = max(
        math.ceil(rows / limits.max_rows),
        math.ceil(chars / limits.max_text_chars),
    )
    if parts < 2:
        return limits.advice
    return f"{limits.advice} Понадобится не меньше {parts} {plural(parts, 'части', 'частей', 'частей')}."


def _chars_text(count: int, *, genitive: bool = False) -> str:
    """«16 800 000 знаков», «5 млн знаков», «1 001 знак».

    genitive — после «не меньше»: «не меньше 21 знака», «не меньше 22 знаков».
    """
    shown = format_count(count)
    if shown.endswith("млн"):
        return f"{shown} знаков"
    if genitive:
        return f"{shown} {plural(count, 'знака', 'знаков', 'знаков')}"
    return f"{shown} {plural(count, 'знак', 'знака', 'знаков')}"


def field_max_mb(limits: UploadLimits) -> int:
    """Предел веса для поля загрузки выгрузки, МБ.

    Не больше server.maxUploadSize (.streamlit/config.toml): файл тяжелее
    сервер Streamlit не примет, сколько ни подними UPLOAD_MAX_MB, — и подпись
    под полем должна называть предел, который действует на деле.
    """
    try:
        import streamlit as st

        server_mb = int(st.get_option("server.maxUploadSize"))
    except (ImportError, RuntimeError, TypeError, ValueError):
        server_mb = limits.max_mb
    return max(1, min(limits.max_mb, server_mb))


def upload_hint(limits: UploadLimits, max_mb: int | None = None) -> str:
    """Подпись под полем загрузки.

    Само поле Streamlit пишет предел по-английски («20MB per file») и так же
    отказывает тяжёлому файлу, поэтому пределы повторяются здесь по-русски.
    max_mb — предел самого поля (field_max_mb), если он меньше.
    """
    mb = limits.max_mb if max_mb is None else min(limits.max_mb, int(max_mb))
    if not limits.can_split:
        return (
            f"За один раз — файл до {mb} МБ и до {format_count(limits.max_rows)} "
            "строк. Данные категории записываются к периоду одним файлом: если "
            "загрузить их частями, следующая часть заменит предыдущую."
        )
    return (
        f"За один раз — файл до {mb} МБ, до {format_count(limits.max_rows)} "
        f"сообщений, до {limits.max_cols} колонок и до "
        f"{format_count(limits.max_text_chars)} знаков текста "
        f"(это примерно {_articles(limits.max_text_chars)} полных статей СМИ). "
        "Если выгрузка больше, разбейте её по неделям или месяцам и загрузите "
        "частями."
    )


def _articles(chars: int) -> str:
    """Сколько полных статей СМИ в таком объёме: одна — около 5 600 знаков
    текста и ещё несколько сотен в соседних ячейках."""
    return format_count(max(100, round(chars / 6000, -2)))


def structure_file_problem(file_bytes: bytes) -> str | None:
    """Файл структуры тегов слишком велик — текст для человека.

    Это таблица из колонок tag, tier и parent — килобайты. Общий предел
    загрузки (20 МБ) для неё велик: книга Excel в 2 МБ разворачивается в
    сотни мегабайт данных, а читается она целиком.
    """
    size = len(file_bytes)
    unpacked = unpacked_size(file_bytes)
    if size <= STRUCTURE_MAX_MB * MB and (
        unpacked is None or unpacked <= STRUCTURE_MAX_UNPACKED_MB * MB
    ):
        return None
    LOGGER.info("Файл структуры тегов отклонён: %s байт, внутри %s", size, unpacked)
    return (
        f"Файл структуры тегов слишком большой: он весит {format_mb(size)} МБ"
        + (f", а данных в нём {format_mb(unpacked)} МБ" if unpacked is not None else "")
        + f". Структура — небольшая таблица с колонками tag, tier и parent: "
        f"файл должен весить не больше {STRUCTURE_MAX_MB} МБ. Проверьте, что "
        "загружен нужный файл."
    )
