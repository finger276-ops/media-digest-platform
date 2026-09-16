"""Import adapters for different monitoring-system exports.

The dashboard works with one canonical table that is close to the original
Mediologia CSV schema. This module reads CSV/XLSX files from different systems
and maps their columns to that canonical schema before preprocessing.
"""

from __future__ import annotations

import contextlib
import csv
import html
import logging
import re
import tempfile
import zipfile
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

MINIMAL_XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/><scheme val="minor"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium9" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>"""


LOGGER = logging.getLogger("platform.import_adapters")


def _excel_error_mentions_styles(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "stylesheet" in message
        or "styles.xml" in message
        or "could not read stylesheet" in message
    )


def _repair_xlsx_styles(path: Path) -> Path:
    """Return a temporary XLSX copy with a minimal valid styles.xml.

    Some monitoring-system exports contain broken style XML. The worksheet data
    is still valid, but openpyxl refuses to open the workbook before pandas can
    read it. Replacing only xl/styles.xml keeps cell values intact and lets the
    import continue. If the file is not a valid XLSX zip, the original exception
    will be raised by the caller.
    """
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError(
            "Автовосстановление стилей поддерживается только для .xlsx/.xlsm файлов."
        )

    tmp = tempfile.NamedTemporaryFile(
        prefix="xlsx_styles_repaired_", suffix=path.suffix.lower(), delete=False
    )
    tmp_path = Path(tmp.name)
    tmp.close()

    # Файл уже создан, а дальше всё может упасть: сюда попадают в том числе
    # файлы, которые вовсе не zip (чужой формат с расширением .xlsx). Без
    # уборки на этой ветке пустышка оставалась бы в temp после каждой такой
    # попытки — а вызывающий про неё даже не узнает, ему прилетит исключение.
    try:
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as zout:
            wrote_styles = False
            for item in zin.infolist():
                if item.filename == "xl/styles.xml":
                    zout.writestr(item, MINIMAL_XLSX_STYLES)
                    wrote_styles = True
                else:
                    zout.writestr(item, zin.read(item.filename))
            if not wrote_styles:
                zout.writestr("xl/styles.xml", MINIMAL_XLSX_STYLES)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Не удалось удалить временную копию %s", tmp_path)
        raise
    return tmp_path


@contextlib.contextmanager
def _open_excel_file_resilient(path: Path) -> Iterator[pd.ExcelFile]:
    """Открыть книгу, при необходимости починив стили, и прибрать за собой.

    Контекстный менеджер, а не обычная функция: починка делает временную копию
    файла, и без гарантированного выхода эта копия оставалась в temp навсегда —
    размером с исходную выгрузку, на каждую загрузку. Заодно закрывается сам
    pd.ExcelFile: на Windows незакрытый хендл не даёт удалить копию, так что
    одна утечка держала бы вторую.

    Наружу отдаётся книга, а не путь: читать нужно именно из неё, иначе pandas
    разбирает файл заново на каждый лист.
    """
    repaired: Path | None = None
    xls: pd.ExcelFile | None = None
    try:
        try:
            xls = pd.ExcelFile(path)
        except Exception as exc:
            # openpyxl may raise either a friendly "could not read stylesheet"
            # ValueError or a raw XMLSyntaxError while parsing xl/styles.xml. For
            # XLSX/XLSM files it is safe to try one repaired copy before failing.
            if path.suffix.lower() not in {".xlsx", ".xlsm"}:
                raise
            try:
                repaired = _repair_xlsx_styles(path)
                xls = pd.ExcelFile(repaired)
            except Exception as repair_exc:
                if _excel_error_mentions_styles(exc):
                    raise ValueError(
                        "Excel-файл не удалось прочитать из-за поврежденных стилей книги. "
                        "Попробуйте открыть файл в Excel/LibreOffice и сохранить заново как .xlsx или .csv. "
                        "Если это выгрузка Brand Analytics, лучше сохранить лист «Сообщения» отдельным CSV."
                    ) from repair_exc
                raise exc
        yield xls
    finally:
        if xls is not None:
            try:
                xls.close()
            except Exception:  # noqa: BLE001 — закрытие не должно ронять разбор
                LOGGER.warning("Не удалось закрыть книгу %s", path, exc_info=True)
        if repaired is not None:
            try:
                repaired.unlink(missing_ok=True)
            except OSError:
                # Файл мог остаться занятым антивирусом или индексатором:
                # мусор в temp лучше, чем упавшая загрузка выгрузки.
                LOGGER.warning("Не удалось удалить временную копию %s", repaired)


CANONICAL_COLUMNS = [
    "№",
    "Дата",
    "Сообщение",
    # Заголовок остаётся и отдельной колонкой, хотя приклеивается к «Сообщению»:
    # он нужен как материал для названия инфоповода. Заголовок новости или
    # видео — готовая формулировка, первая строка поста — нет.
    "Заголовок",
    "Автораспознанный текст",
    "Ссылка",
    "Площадка",
    "Автор",
    "Профиль автора",
    "Блог",
    "Профиль блога",
    "Тип",
    # Природа площадки (отзывы, СМИ, соцсети, видео) — отдельная ось от типа
    # сообщения (пост, репост, комментарий), которая занимает колонку «Тип».
    # Без неё отзывы с маркетплейсов не отличить от новостей, а это разные
    # вещи: жалоба покупателя не инфоповод.
    "Тип площадки",
    # Оценка товара покупателем. В выгрузках Brand Analytics заполнена почти у
    # всех отзывов и ровно ни у чего другого — это их собственная метрика, и
    # без неё раздел репутации товара показывать нечего.
    "Оценка",
    # Поля, которые система отдаёт, а платформа до сих пор выбрасывала. Сверка
    # csv и xlsx одной выгрузки показала, что мимо канона проходит девять
    # колонок с данными, и «Роль объекта» с «Языком» заполнены у всех строк.
    # Роль отвечает на вопрос, о бренде ли сообщение или он там упомянут
    # вскользь, — для конкурентного обзора это первая линия отсечения шума.
    "Роль объекта",
    "Язык",
    "Тип автора",
    "Пол",
    "Возраст",
    "Место",
    "Адрес",
    "Цитируемость СМИ",
    "Аудитория СМИ",
    # Поля Медиалогии. Система даёт две аудитории вместо одной, свой индекс
    # заметности и портрет автора подробнее, чем Brand Analytics. Обе аудитории
    # сохраняются раздельно: «Аудитория» сводится из них для сопоставимости
    # проектов, но исходные числа теряться не должны.
    # Hash сообщения у Brand Analytics — устойчивый идентификатор публикации,
    # который не меняется между выгрузками. «Id сообщения» занят порядковым
    # номером внутри темы, поэтому хеш до сих пор пропадал: диагностика импорта
    # его и обнаружила, пометив колонкой со стопроцентным заполнением, которую
    # платформа не читает.
    "Хеш сообщения",
    "Аудитория блога",
    "Аудитория автора",
    "Тип блога",
    "СМ Индекс",
    "Семейный статус",
    "Образование",
    "Статус на площадке",
    "Объекты",
    "Аспекты",
    "Мнения",
    "Спам",
    "Объявления",
    "Примечание",
    "Тональность",
    "Токсичность",
    "WOM",
    "Страна",
    "Регион",
    "Город",
    "Количество дублей",
    "Аудитория",
    "Просмотры",
    "Вовлечённость",
    "Лайки",
    "Комментарии",
    "Репосты",
    "Текст родительского поста",
    "Ссылка на родительский пост",
    "Дата публикации родительского поста",
    "Теги",
    "Категории",
    "Сюжет",
    "Id сообщения",
    "Основная тема",
    "Все темы",
    "Все темы (список)",
    "Релевантное",
    "source_system",
    "source_file",
    "source_tag_columns",
]

MEDIALOGIA_DEFAULT_TAGS = [
    "Коэффициент",
    "Законы и налоги",
    "яндекс",
    "WB Такси",
    "Фастен",
    "Приложение и сбои",
    "Яндекс Про",
    "Забастовка",
]


def _clean_col_name(value: object) -> str:
    value = "" if value is None else str(value)
    value = value.replace("\ufeff", "").replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if value.lower().startswith("unnamed"):
        return ""
    return value


NBSP = " "
# Excel экранирует возврат каретки внутри ячейки, а openpyxl оставляет
# экранирование как есть. В xlsx-выгрузке Медиалогии «_x000d_» стоит в тексте
# 1208 строк из 2234 — и дошло бы до карточки сообщения и до поиска.
EXCEL_CARRIAGE_RETURN = "_x000d_"
# Число с пробелом в разрядах: «2 786», «621 789». Так их пишет csv Медиалогии
# (4170 ячеек), тогда как xlsx той же выгрузки пишет «2786». Значение одно и то
# же, различается только запись, и от записи не должно зависеть ничего.
GROUPED_NUMBER = re.compile("^-?\\d{1,3}(?:[ " + NBSP + "]\\d{3})+(?:[.,]\\d+)?$")
# Длиннее этого числом быть нечему, а проверять каждую строку текста незачем.
MAX_NUMBER_LENGTH = 24


def _normalize_source_quirks(values: pd.Series) -> pd.Series:
    """Убрать различия записи, за которыми стоит одно и то же значение.

    Одна и та же выгрузка Медиалогии в двух форматах давала разный текст в 1208
    строках и разные числа в 4170 ячейках — при полностью совпадающих данных.
    Артефакты зеркальные: xlsx оставляет «_x000d_» вместо возврата каретки, csv
    пишет пробел в разрядах и настоящий «\\r». Пока это не выправлено, два
    проекта из одной системы ведут себя по-разному без всякой причины.
    """
    for marker in (EXCEL_CARRIAGE_RETURN, EXCEL_CARRIAGE_RETURN.upper()):
        hits = values.str.contains(marker, regex=False, na=False)
        if hits.any():
            _note_normalized("excel_cr", int(hits.sum()))
            values = values.str.replace(marker, "", regex=False)

    carriage = values.str.contains("\r", regex=False, na=False)
    if carriage.any():
        _note_normalized("line_endings", int(carriage.sum()))
        values = values.str.replace("\r\n", "\n", regex=False).str.replace(
            "\r", "\n", regex=False
        )

    # Пробел трогаем только в ячейке, которая целиком является числом: в тексте
    # «в 5 7 часов» склейка цифр была бы порчей данных.
    spaced = values.str.contains(" ", regex=False, na=False) | values.str.contains(
        NBSP, regex=False, na=False
    )
    candidates = spaced & values.str.len().le(MAX_NUMBER_LENGTH)
    if candidates.any():
        values = values.copy()
        fixed = 0
        repaired = []
        for value in values[candidates]:
            if GROUPED_NUMBER.match(value):
                fixed += 1
                repaired.append(value.replace(" ", "").replace(NBSP, ""))
            else:
                repaired.append(value)
        values.loc[candidates] = repaired
        _note_normalized("grouped_numbers", fixed)
    return values


def _decode_html_entities(values: pd.Series) -> pd.Series:
    """Вернуть тексту нормальные символы вместо HTML-мнемоник.

    Brand Analytics отдаёт часть текстов так, как они лежали в разметке
    страницы: «Свежее поступление&#33;» вместо восклицательного знака, «&gt;»
    вместо угловой скобки, «&nbsp;» вместо пробела. Это не особенность формата —
    мнемоники нашлись во всех проверенных выгрузках RUFLEX, и в xlsx, и в csv,
    в 3–5% строк.

    Чинится один раз на входе, а не при показе: иначе одно и то же сообщение
    выглядит по-разному в ленте, в заголовке инфоповода и в поиске, а склейка
    похожих заголовков считает «— Строительная газета» и «&#8212; Строительная
    газета» разными сюжетами. Один такой случай в выгрузке за август и был.
    """
    # Дешёвая проверка перед дорогим разбором: амперсанд есть у считанных
    # процентов ячеек, а кадр может быть на десятки тысяч строк.
    marked = values.str.contains("&", regex=False, na=False)
    if not marked.any():
        return values
    decoded = values.copy()
    changed = 0
    replacements = []
    for value in values[marked]:
        # Неразрывный пробел из &nbsp; заменяется на обычный: как символ он
        # ничем не помогает, зато ломает поиск и сравнение строк.
        fixed = html.unescape(value).replace(" ", " ")
        if fixed != value:
            changed += 1
        replacements.append(fixed)
    decoded.loc[marked] = replacements
    # Считаются изменённые ячейки, а не содержащие амперсанд: «Иванов &
    # Партнёры» мнемоник не содержит и в отчёт попадать не должен.
    _note_normalized("html_entities", changed)
    return decoded


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [_clean_col_name(c) for c in df.columns]
    df = df.loc[:, [bool(str(c).strip()) for c in df.columns]]
    df = df.dropna(how="all")
    for col in df.columns:
        df[col] = _normalize_source_quirks(
            _decode_html_entities(df[col].fillna("").astype(str))
        )
    df = df.loc[
        ~df.apply(lambda r: all(str(v).strip() == "" for v in r), axis=1)
    ].reset_index(drop=True)
    return df


def _sniff_delimiter(line: str) -> str:
    candidates = [";", ",", "\t"]
    counts = {sep: line.count(sep) for sep in candidates}
    return max(counts, key=counts.get) if max(counts.values()) else ";"


def _find_csv_header(path: Path) -> tuple[int, str]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        lines = [f.readline() for _ in range(50)]
    best_idx = 0
    best_score = -1
    best_sep = ";"
    header_tokens = [
        "дата",
        "текст",
        "сообщение",
        "id сообщения",
        "url",
        "ссылка",
        "источник",
        "автор",
        "тональность",
        "блог",
        "площадка",
        "основная тема",
        "все темы",
        "релевантное",
    ]
    for idx, line in enumerate(lines):
        if not line:
            continue
        sep = _sniff_delimiter(line)
        parts = [p.strip().strip('"').lower() for p in line.split(sep)]
        score = sum(any(token in part for part in parts) for token in header_tokens)
        if score > best_score and len(parts) >= 4:
            best_idx = idx
            best_score = score
            best_sep = sep
    return best_idx, best_sep


def _read_csv_any(path: Path) -> pd.DataFrame:
    header_idx, sep = _find_csv_header(path)
    try:
        df = pd.read_csv(
            path,
            sep=sep,
            encoding="utf-8-sig",
            skiprows=header_idx,
            dtype=str,
            keep_default_na=False,
            engine="python",
            quoting=csv.QUOTE_MINIMAL,
            on_bad_lines="warn",
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            path,
            sep=sep,
            encoding="cp1251",
            skiprows=header_idx,
            dtype=str,
            keep_default_na=False,
            engine="python",
            quoting=csv.QUOTE_MINIMAL,
            on_bad_lines="warn",
        )
    return _clean_dataframe(df)


def _find_excel_header(frame: pd.DataFrame) -> int:
    header_tokens = [
        "дата",
        "время публикации",
        "текст",
        "текст сообщения",
        "ссылка",
        "ссылка на сообщение",
        "кто пишет",
        "где пишет",
        "тональность",
        "id сообщения",
        "основная тема",
        "все темы",
        "релевантное",
    ]
    best_idx = 0
    best_score = -1
    scan = frame.head(40).fillna("").astype(str)
    for idx, row in scan.iterrows():
        values = [_clean_col_name(v).lower() for v in row.tolist()]
        score = sum(any(token in value for value in values) for token in header_tokens)
        non_empty = sum(1 for value in values if value)
        if score > best_score and non_empty >= 4:
            best_idx = int(idx)
            best_score = score
    return best_idx


def _read_excel_any(path: Path, sheet_name: str | int | None = None) -> pd.DataFrame:
    """Read Excel and choose the best non-empty sheet.

    Monitoring exports often contain cover sheets, empty technical sheets or
    analytical tabs. We prefer the raw-message sheet named "Сообщения" and
    skip empty sheets instead of crashing on preview.iloc[0]. If the workbook
    has broken styles.xml, we automatically read a temporary repaired copy.
    """
    with _open_excel_file_resilient(path) as xls:
        return _read_excel_sheets(xls, sheet_name)


def _read_excel_sheets(
    xls: pd.ExcelFile, sheet_name: str | int | None
) -> pd.DataFrame:
    """Выбрать лист с сообщениями и прочитать его.

    Вынесено из _read_excel_any, чтобы выбор листа целиком помещался внутрь
    контекста открытой книги: после выхода из него временной копии уже нет.

    Все чтения идут из xls, а не по пути на диске. Выгрузки мониторинга часто
    многолистовые (обложка, сводка, динамика, источники, сообщения), а выбор
    нужного листа требует заглянуть в каждый: чтение по пути заставляло pandas
    распаковывать и разбирать всю книгу заново на каждую такую заглядку.
    """
    sheets = xls.sheet_names
    if not sheets:
        raise ValueError("В Excel-файле не найдено листов.")

    def safe_preview(sheet):
        try:
            preview_df = pd.read_excel(
                xls,
                sheet_name=sheet,
                header=None,
                dtype=str,
                nrows=60,
                keep_default_na=False,
            )
            if preview_df is None or preview_df.empty:
                return pd.DataFrame()
            preview_df = preview_df.dropna(how="all")
            if preview_df.empty:
                return pd.DataFrame()
            return preview_df
        except Exception:  # noqa: BLE001 — нечитаемый лист выбывает из выбора
            # Молчать полностью нельзя: если «выпал» лист с сообщениями,
            # платформа возьмёт не тот — след в логе объяснит, почему.
            LOGGER.warning("Лист Excel не прочитался при предпросмотре", exc_info=True)
            return pd.DataFrame()

    preferred_sheet_names = {"сообщения", "messages", "публикации", "mentions"}
    selected_sheet = None
    selected_header_idx = 0
    best_score = -1

    candidates = [sheet_name] if sheet_name is not None else sheets
    for candidate in candidates:
        preview = safe_preview(candidate)
        if preview.empty:
            continue
        row_idx = _find_excel_header(preview)
        if row_idx < 0 or row_idx >= len(preview):
            row_idx = 0
        row_values = [
            _clean_col_name(v).lower()
            for v in preview.iloc[row_idx].fillna("").astype(str).tolist()
        ]
        tokens = [
            "текст",
            "сообщ",
            "дата",
            "время",
            "ссылка",
            "url",
            "автор",
            "источник",
            "тональность",
            "сюжет",
            "hash сообщения",
            "id сообщения",
            "место публикации",
            "тип источника",
            "основная тема",
            "все темы",
            "релевантное",
        ]
        score = sum(any(token in value for value in row_values) for token in tokens)
        sheet_key = str(candidate).strip().lower().replace("ё", "е")
        if sheet_key in preferred_sheet_names:
            score += 10
        if score > best_score:
            selected_sheet = candidate
            selected_header_idx = row_idx
            best_score = score

    if selected_sheet is None:
        raise ValueError(
            "Не удалось найти непустой лист с таблицей сообщений. "
            "Проверьте, что в Excel есть лист с колонками: дата, текст/сообщение, url/ссылка, автор или источник."
        )

    df = pd.read_excel(
        xls,
        sheet_name=selected_sheet,
        header=selected_header_idx,
        dtype=str,
        keep_default_na=False,
    )
    df = _clean_dataframe(df)
    if df.empty:
        raise ValueError(f"На листе Excel «{selected_sheet}» не найдено данных.")
    return df


def detect_source_system(df: pd.DataFrame) -> str:
    cols = {str(c).strip().lower() for c in df.columns}
    if {
        "hash сообщения",
        "источник",
        "url",
        "тип источника",
    } & cols and "id сообщения" in cols:
        return "brand_analytics"
    if "кто пишет" in cols or "где пишет" in cols or "время публикации" in cols:
        return "mediologia_excel"
    if "автораспознанный текст" in cols or "профиль блога" in cols or "блог" in cols:
        return "mediologia"
    return "generic"


def first_existing(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lower_map:
            _note_consumed(lower_map[key])
            return df[lower_map[key]].fillna("").astype(str)
    return pd.Series([""] * len(df), index=df.index, dtype="object")


# Какие исходные колонки платформа действительно использовала при разборе.
#
# Отчёт строится из того, что произошло, а не из отдельного списка синонимов.
# Список пришлось бы держать рядом с шестью десятками вызовов first_existing и
# следить, чтобы он не разошёлся с ними; разошедшийся список врёт, а врущая
# диагностика хуже отсутствующей.
_CONSUMED_COLUMNS: ContextVar[set[str] | None] = ContextVar(
    "import_consumed_columns", default=None
)
_NORMALIZED_CELLS: ContextVar[dict[str, int] | None] = ContextVar(
    "import_normalized_cells", default=None
)


@contextlib.contextmanager
def _recording_import() -> Iterator[tuple[set[str], dict[str, int]]]:
    """Собрать, что было прочитано, пока идёт разбор выгрузки.

    Вложенный вызов переиспользует уже открытый сбор. Чистка кадра случается
    дважды — при чтении файла и при канонизации, — и без этого счётчики
    выправленных ячеек оказывались нулевыми: к моменту второй чистки чинить
    было уже нечего.
    """
    existing_consumed = _CONSUMED_COLUMNS.get()
    existing_normalized = _NORMALIZED_CELLS.get()
    if existing_consumed is not None and existing_normalized is not None:
        yield existing_consumed, existing_normalized
        return

    consumed: set[str] = set()
    normalized: dict[str, int] = {}
    consumed_token = _CONSUMED_COLUMNS.set(consumed)
    normalized_token = _NORMALIZED_CELLS.set(normalized)
    try:
        yield consumed, normalized
    finally:
        _CONSUMED_COLUMNS.reset(consumed_token)
        _NORMALIZED_CELLS.reset(normalized_token)


def _note_consumed(column: object) -> None:
    consumed = _CONSUMED_COLUMNS.get()
    if consumed is not None:
        consumed.add(str(column))


def _note_normalized(kind: str, count: int) -> None:
    if count <= 0:
        return
    normalized = _NORMALIZED_CELLS.get()
    if normalized is not None:
        normalized[kind] = normalized.get(kind, 0) + int(count)


def _coalesce(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    """Первое непустое значение по строке, а не первая непустая колонка.

    first_existing выбирает колонку целиком: если она есть, остальные не
    рассматриваются. Для аудитории этого мало. Медиалогия отдаёт «Аудиторию
    блога» и «Аудиторию автора», и в проверенной выгрузке первая заполнена у
    2130 строк из 2234 — оставшиеся 104 получили бы ноль вместо авторской
    аудитории, которая там есть.
    """
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    result: pd.Series | None = None
    for candidate in candidates:
        column = lower_map.get(candidate.strip().lower())
        if column is None:
            continue
        values = df[column].fillna("").astype(str)
        _note_consumed(column)
        result = values if result is None else result.where(result.str.strip() != "", values)
        if result.str.strip().ne("").all():
            break
    if result is None:
        return pd.Series([""] * len(df), index=df.index, dtype="object")
    return result


def _join_text_parts(*parts: pd.Series) -> pd.Series:
    result = (
        pd.Series([""] * len(parts[0]), index=parts[0].index, dtype="object")
        if parts
        else pd.Series(dtype="object")
    )
    for part in parts:
        result = [
            "\n".join([x for x in [str(a).strip(), str(b).strip()] if x])
            for a, b in zip(result, part.fillna("").astype(str))
        ]
        result = pd.Series(result, index=part.index, dtype="object")
    return result


def _normalize_sentiment(series: pd.Series) -> pd.Series:
    def convert(value: object) -> str:
        s = str(value or "").strip().lower().replace("ё", "е")
        if not s:
            return ""
        if "нег" in s or s in {"negative", "-", "минус"}:
            return "негативная"
        if "позит" in s or "полож" in s or s in {"positive", "+", "плюс"}:
            return "позитивная"
        if "нейтр" in s or s in {"neutral", "0"}:
            return "нейтральная"
        return str(value).strip()

    return series.apply(convert)


def _normalize_topics_list(value: object) -> str:
    """Normalize list-like topic values to semicolon-separated text."""
    s = "" if value is None else str(value).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return ""
    s = s.replace("\ufeff", "").replace("\xa0", " ")
    # Values may arrive as: ['A', 'B'], A; B, A|B, or multiline.
    s = s.strip("[]")
    s = s.replace("'", "").replace('"', "")
    parts = re.split(r"\s*[;|,\n]\s*", s)
    seen = []
    for part in parts:
        part = re.sub(r"\s+", " ", str(part).strip())
        if part and part not in seen:
            seen.append(part)
    return "; ".join(seen)


def _normalize_bool_text(value: object) -> str:
    s = "" if value is None else str(value).strip().lower().replace("ё", "е")
    if s in {"true", "1", "да", "yes", "+", "истина", "верно"}:
        return "True"
    if s in {
        "false",
        "0",
        "нет",
        "no",
        "-",
        "ложь",
        "неверно",
        "нерелевантно",
        "не релевантно",
        "irrelevant",
    }:
        return "False"
    return str(value).strip() if value is not None else ""


# Собственные поля Brand Analytics, которые тегами не являются никогда.
#
# Позиция маркера «Обработано» не фиксирована: в xlsx-выгрузке RUFLEX после него
# шли только теги, а в csv-выгрузке того же периода за ним оказались «Место» и
# «Адрес» — география публикации. Без этого списка они попадали в статистику
# тегов как теги проекта, и заказчик видел в одном ряду с «Docke» и «Кровля»
# ярлыки «Руфлекс» и «Россия, Московская область».
#
# Список, а не проверка формы значений. У настоящих колонок-тегов ячейка
# содержит имя самой колонки — в обеих выгрузках RUFLEX это выполняется ровно
# в 100% случаев, а у «Места» и «Адреса» ровно в 0%. Соблазнительно сделать
# правилом именно это, но проект, где Brand Analytics пишет в такую ячейку «да»
# вместо названия, потерял бы разом все теги. Молча потерять тег хуже, чем
# показать лишнюю колонку.
BRAND_ANALYTICS_FIELD_COLUMNS = frozenset(
    {
        "дата", "время", "hash сообщения", "id сообщения", "заголовок", "текст",
        "источник", "url", "тип источника", "тип сообщения", "сюжет", "автор",
        "url автора", "тип автора", "место публикации", "url места публикации",
        "пол", "возраст", "аудитория", "комментариев", "комментарии",
        "цитируемость сми", "репостов", "репосты", "лайков", "лайки",
        "вовлеченность", "просмотры", "просмотров", "оценка", "дублей",
        "аудитория сми", "тональность", "роль объекта", "агрессия", "страна",
        "регион", "город", "место", "адрес", "язык", "wom", "обработано",
    }
)


def _brand_analytics_tag_columns(df: pd.DataFrame) -> list[str]:
    """Return Brand Analytics tag columns located after the `Обработано` marker.

    Brand Analytics exports place user/system tags as separate columns after
    the service column `Обработано`. In those columns a non-empty cell usually
    contains the tag label itself. These columns are essential for topic
    grouping and analytics in non-taxi projects.

    Порядок колонок различается между xlsx и csv одной и той же выгрузки,
    поэтому за маркером могут оказаться и собственные поля системы — их
    отсеивает BRAND_ANALYTICS_FIELD_COLUMNS.
    """
    if df is None or df.empty:
        return []
    columns = list(df.columns)
    marker_idx = -1
    for idx, col in enumerate(columns):
        key = _clean_col_name(col).lower().replace("ё", "е")
        if key in {"обработано", "processed", "processed?", "is processed"}:
            marker_idx = idx
            break
    if marker_idx < 0:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for col in columns[marker_idx + 1 :]:
        label = _clean_col_name(col)
        key = label.lower().replace("ё", "е")
        if not label or key in seen or key.startswith("unnamed"):
            continue
        if key in BRAND_ANALYTICS_FIELD_COLUMNS:
            continue
        values = (
            df[col].fillna("").astype(str).str.strip()
            if col in df.columns
            else pd.Series(dtype=str)
        )
        if values.empty or values.eq("").all():
            continue
        seen.add(key)
        result.append(col)
    return result


def canonicalize_table(
    raw: pd.DataFrame,
    source_file: str = "",
    source_system: str = "auto",
    report: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Привести выгрузку к каноническому виду платформы.

    Если передать `report`, в него попадёт сводка разбора: сколько колонок
    распознано, какие остались непонятыми и что пришлось выправить в тексте.
    Считается попутно, второго прохода по данным не требует.
    """
    with _recording_import() as (consumed, normalized):
        # Чистка идёт здесь, а не внутри: она же считает выправленные ячейки,
        # и второй прогон удвоил бы счётчики отчёта.
        df = _clean_dataframe(raw)
        result = _canonicalize_cleaned(
            df, source_file=source_file, source_system=source_system
        )
        if report is not None:
            from services.import_report import build_import_report

            detected = (
                detect_source_system(df)
                if source_system in {"", "auto", None}
                else str(source_system)
            )
            report.update(
                build_import_report(
                    df,
                    consumed=consumed,
                    normalized=normalized,
                    detected_system=detected,
                    tag_columns=(
                        _brand_analytics_tag_columns(df)
                        if detected == "brand_analytics"
                        else []
                    ),
                )
            )
    return result


def _canonicalize_cleaned(
    df: pd.DataFrame, source_file: str = "", source_system: str = "auto"
) -> pd.DataFrame:
    """Сама канонизация. Кадр приходит уже вычищенным."""
    detected = (
        detect_source_system(df)
        if source_system in {"", "auto", None}
        else str(source_system)
    )
    out = pd.DataFrame(index=df.index)

    out["№"] = first_existing(df, ["№", "N", "ID", "ID сообщения"])
    out["Дата"] = first_existing(
        df,
        [
            "Дата",
            "Время публикации",
            "Дата публикации",
            "Дата сообщения",
            "Date",
            "Published",
        ],
    )
    time_part = first_existing(df, ["Время", "Time", "Published time"])
    # Some exports, for example Knauf/Brand Analytics Excel, split date and time.
    if time_part.str.strip().ne("").any() and out["Дата"].str.strip().ne("").any():
        out["Дата"] = [
            (
                f"{str(d).strip()} {str(t).strip()}".strip()
                if str(t).strip()
                else str(d).strip()
            )
            for d, t in zip(out["Дата"], time_part)
        ]

    message = first_existing(
        df,
        [
            "Сообщение",
            "Текст сообщения",
            "Текст",
            "Message",
            "Text",
            "Содержание",
            "Описание",
            "Content",
        ],
    )
    recognized = first_existing(
        df, ["Автораспознанный текст", "Распознанный текст", "OCR", "Расшифровка"]
    )
    title = first_existing(df, ["Заголовок", "Title"])
    if detected == "brand_analytics":
        out["Сообщение"] = _join_text_parts(title, message)
    else:
        out["Сообщение"] = message
    # Склейка с текстом остаётся как была — она кормит поиск и саммари. Но
    # заголовок нужен и сам по себе: склеенный, он неотличим от первой строки.
    out["Заголовок"] = title
    out["Автораспознанный текст"] = recognized

    out["Ссылка"] = first_existing(
        df, ["Ссылка", "Ссылка на сообщение", "Url", "URL", "url", "Link"]
    )
    out["Площадка"] = first_existing(df, ["Площадка", "Источник", "Система", "Source"])
    out["Автор"] = first_existing(
        df, ["Автор", "Кто пишет", "Author", "Пользователь", "User"]
    )
    out["Профиль автора"] = first_existing(
        df,
        [
            "Профиль автора",
            "Ссылка на автора",
            "Url автора",
            "URL автора",
            "Author URL",
        ],
    )

    blog = first_existing(
        df,
        ["Блог", "Где пишет", "Место публикации", "Источник", "Канал", "Группа", "Чат"],
    )
    blog_profile = first_existing(
        df,
        [
            "Профиль блога",
            "Ссылка на блог",
            "Url места публикации",
            "URL места публикации",
            "Url источника",
        ],
    )
    out["Блог"] = blog
    out["Профиль блога"] = blog_profile

    out["Тип"] = first_existing(
        df, ["Тип", "Тип сообщения", "Тип источника", "Message type", "Source type"]
    )
    # Brand Analytics различает «Тип сообщения» (пост, репост, комментарий) и
    # «Тип источника» (соцсети, отзывы, видео, СМИ). Первое занимает «Тип»,
    # второе до сих пор терялось — а именно оно отделяет отзыв от новости.
    out["Тип площадки"] = first_existing(
        df, ["Тип площадки", "Тип источника", "Source type", "Тип ресурса"]
    )
    # Дробные значения вроде «4.8» — это сводный рейтинг карточки, а не ошибка,
    # поэтому колонка остаётся текстовой и разбирается числом уже в аналитике.
    out["Оценка"] = first_existing(
        df, ["Оценка", "Оценка от 1 до 5", "Рейтинг", "Оценка товара", "Rating", "Score"]
    )
    out["Роль объекта"] = first_existing(
        df, ["Роль объекта", "Роль", "Object role", "Object Role"]
    )
    out["Язык"] = first_existing(df, ["Язык", "Language", "lang"])
    out["Тип автора"] = first_existing(df, ["Тип автора", "Author type", "Тип аккаунта"])
    out["Пол"] = first_existing(df, ["Пол", "Gender", "Sex"])
    out["Возраст"] = first_existing(df, ["Возраст", "Age"])
    out["Место"] = first_existing(df, ["Место", "Place", "Локация"])
    out["Адрес"] = first_existing(df, ["Адрес", "Address"])
    out["Цитируемость СМИ"] = first_existing(
        df, ["Цитируемость СМИ", "Цитируемость", "Media citation"]
    )
    out["Аудитория СМИ"] = first_existing(
        df, ["Аудитория СМИ", "Media audience", "Аудитория издания"]
    )
    # Поля Медиалогии. Обе аудитории сохраняются как есть: сводная «Аудитория»
    # выше собрана из них, но подмена исходных чисел одним сводным была бы
    # потерей — у площадки и у автора это разные величины.
    out["Хеш сообщения"] = first_existing(
        df, ["Хеш сообщения", "Hash сообщения", "Hash", "hash"]
    )
    out["Аудитория блога"] = first_existing(df, ["Аудитория блога"])
    out["Аудитория автора"] = first_existing(df, ["Аудитория автора"])
    out["Тип блога"] = first_existing(df, ["Тип блога", "Тип сообщества"])
    out["СМ Индекс"] = first_existing(df, ["СМ Индекс", "СМИндекс", "MLG Index"])
    out["Семейный статус"] = first_existing(df, ["Семейный статус"])
    out["Образование"] = first_existing(df, ["Образование"])
    out["Статус на площадке"] = first_existing(df, ["Статус на площадке"])
    out["Объекты"] = first_existing(df, ["Объекты"])
    out["Аспекты"] = first_existing(df, ["Аспекты"])
    out["Мнения"] = first_existing(df, ["Мнения"])
    out["Спам"] = first_existing(df, ["Спам"])
    out["Объявления"] = first_existing(df, ["Объявления"])
    out["Примечание"] = first_existing(df, ["Примечание", "Комментарий аналитика"])
    out["Тональность"] = _normalize_sentiment(
        first_existing(df, ["Тональность", "Sentiment", "Окраска", "Тон"])
    )
    out["Токсичность"] = first_existing(
        df, ["Токсичность", "Агрессия", "Toxicity", "Aggression"]
    )
    out["WOM"] = first_existing(df, ["WOM", "Мнения"])
    out["Страна"] = first_existing(df, ["Страна", "Country"])
    out["Регион"] = first_existing(df, ["Регион", "Region"])
    out["Город"] = first_existing(df, ["Город", "City"])
    out["Количество дублей"] = first_existing(
        df, ["Количество дублей", "Дублей", "Duplicates"]
    )
    # Медиалогия не отдаёт «Аудиторию» одной колонкой — у неё их две, блога и
    # автора. Без этих синонимов аудитория у любого проекта на Медиалогии
    # оказывалась нулевой, а вместе с ней и ER, который делится на неё.
    # Аудитория блога идёт первой: это подписчики площадки, тот же смысл, что у
    # «Аудитории» Brand Analytics. В проверенной выгрузке она заполнена у 2130
    # строк из 2234 против 1841 у авторской и не меньше её в 446 случаях.
    out["Аудитория"] = _coalesce(
        df,
        [
            "Аудитория",
            "Аудитория блога",
            "Аудитория автора",
            "Audience",
            "audience",
        ],
    )
    out["Просмотры"] = first_existing(
        df, ["Просмотры", "Просмотров", "Views", "views", "Охват", "Reach", "reach"]
    )
    out["Вовлечённость"] = first_existing(
        df, ["Вовлечённость", "Вовлеченность", "Engagement", "engagement"]
    )
    # Родительный падеж — как Brand Analytics называет эти колонки в выгрузке:
    # «Лайков», «Репостов», наравне с «Комментариев», «Просмотров», «Дублей».
    # Без него лайки и репосты молча обнулялись на каждой выгрузке BA, хотя
    # общая вовлечённость приезжала отдельной колонкой и выглядела правдоподобно.
    out["Лайки"] = first_existing(df, ["Лайки", "Лайков", "Likes"])
    out["Комментарии"] = first_existing(df, ["Комментарии", "Комментариев", "Comments"])
    out["Репосты"] = first_existing(df, ["Репосты", "Репостов", "Reposts", "Shares"])
    out["Текст родительского поста"] = first_existing(
        df, ["Текст родительского поста", "Родительский пост", "Parent text"]
    )
    out["Ссылка на родительский пост"] = first_existing(
        df, ["Ссылка на родительский пост", "Parent URL", "Parent link"]
    )
    out["Дата публикации родительского поста"] = first_existing(
        df, ["Дата публикации родительского поста", "Parent date"]
    )

    out["Теги"] = first_existing(df, ["Теги", "Tags", "Метки", "Tag"])
    out["Категории"] = first_existing(df, ["Категории", "Category", "Categories"])
    out["Сюжет"] = first_existing(df, ["Сюжет", "Topic", "Theme", "Тема"])
    out["Id сообщения"] = first_existing(
        df, ["Id сообщения", "ID сообщения", "message_id", "id", "Hash сообщения"]
    )

    # Optional human/topic markup columns. If present, they become a top-level
    # boundary for clustering, but do not replace information events.
    main_topic = first_existing(
        df,
        [
            "Основная тема",
            "Главная тема",
            "Main topic",
            "Primary topic",
            "Topic main",
            "Сюжет",
            "Topic",
            "Theme",
        ],
    )
    all_topics_raw = first_existing(df, ["Все темы", "Темы", "Topics", "All topics"])
    all_topics_list = first_existing(
        df, ["Все темы (список)", "Список тем", "Topics list", "All topics list"]
    )
    relevant = first_existing(
        df, ["Релевантное", "Релевантность", "Relevant", "Is relevant"]
    )

    out["Основная тема"] = main_topic.apply(
        lambda x: re.sub(r"\s+", " ", str(x).strip())
    )
    out["Все темы"] = all_topics_raw.apply(_normalize_topics_list)
    out["Все темы (список)"] = all_topics_list.apply(_normalize_topics_list)
    out.loc[out["Все темы (список)"].str.strip() == "", "Все темы (список)"] = out.loc[
        out["Все темы (список)"].str.strip() == "", "Все темы"
    ]
    out["Релевантное"] = relevant.apply(_normalize_bool_text)

    # Brand Analytics specificity: all non-empty columns after `Обработано`
    # are tag columns. Keep them in the canonical table so preprocess can use
    # them as first-class topic signals instead of falling back to generic words.
    ba_tag_columns = (
        _brand_analytics_tag_columns(df) if detected == "brand_analytics" else []
    )
    for tag_col in ba_tag_columns:
        if tag_col not in out.columns and tag_col in df.columns:
            out[tag_col] = df[tag_col].fillna("").astype(str)
    out["source_tag_columns"] = "|".join(str(c) for c in ba_tag_columns)

    for tag in MEDIALOGIA_DEFAULT_TAGS:
        if tag in df.columns and tag not in out.columns:
            out[tag] = first_existing(df, [tag])

    out["source_system"] = detected
    out["source_file"] = Path(source_file).name if source_file else ""

    for col in CANONICAL_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    out = out[
        [c for c in CANONICAL_COLUMNS if c in out.columns]
        + [c for c in out.columns if c not in CANONICAL_COLUMNS]
    ]
    out = _clean_dataframe(out)
    return out


def read_source_table(
    path: str | Path,
    source_system: str = "auto",
    sheet_name: str | int | None = None,
    report: dict[str, Any] | None = None,
) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    # Сбор открывается до чтения файла: выправление ячеек происходит уже там,
    # и без этого счётчики отчёта не увидели бы ничего.
    with _recording_import():
        if suffix in {".xlsx", ".xls", ".xlsm"}:
            raw = _read_excel_any(path, sheet_name=sheet_name)
        else:
            raw = _read_csv_any(path)
        return canonicalize_table(
            raw, source_file=str(path), source_system=source_system, report=report
        )


def get_excel_sheet_names(path: str | Path) -> list[str]:
    path = Path(path)
    if path.suffix.lower() not in {".xlsx", ".xls", ".xlsm"}:
        return []
    try:
        with _open_excel_file_resilient(path) as xls:
            return list(xls.sheet_names)
    except Exception:  # noqa: BLE001 — файл прочитается (или нет) дальше по конвейеру
        LOGGER.warning("Не удалось перечислить листы Excel: %s", path.name)
        return []
