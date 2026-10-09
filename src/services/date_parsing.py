"""Даты выгрузки: один разбор на весь импорт.

Раньше колонка дат разбиралась одним вызовом pd.to_datetime(..., dayfirst=True).
Для «24.04.2026 10:00» это верно, для даты, записанной с года, — нет. А такие
даты приезжают постоянно: выгрузки читаются как текст (dtype=str), и ячейка
Excel с настоящей датой становится строкой «2026-04-05 09:00:00». С
dayfirst=True pandas читал её как 4 мая. Хуже того, формат pandas выводит по
первой строке колонки: если в ней день не больше 12, весь столбец читался как
«год-день-месяц», и дни с 13-го по 31-е не разбирались вовсе. Апрельская
выгрузка становилась периодом «04.01.2026–04.12.2026», у половины сообщений
даты не было, остальные разъезжались по месяцам. Колонка, где у одних дат
пояс Z, а у других +03:00, обрывала загрузку ошибкой.

Теперь колонка разбирается по частям, у каждой — свой явный формат:

1. Начинается с года (2026-04-05, 2026-04-05T09:00:00Z) — ISO. Время без пояса
   остаётся как есть: системы мониторинга выгружают московское время. Время с
   поясом (Z, +03:00) переводится в московское.
2. «дд.мм.гггг», «дд.мм.гггг чч:мм», «дд.мм.гггг чч:мм:сс» и то же с
   двузначным годом — явными форматами. Результат тот же, что у прежнего
   разбора: на этих датах он не ошибался.
3. Номер дня Excel («45383», «45383.5») — ячейка-дата без формата даты
   приезжает числом. Считается от эпохи Excel.
4. Всё остальное — прежним разбором (dayfirst=True), но только этот остаток:
   что разбиралось раньше, разбирается и теперь. Строки, которые начинаются с
   года, и здесь читаются без dayfirst. Исключение — время без даты («09:15»)
   и слова «today»/«now»: прежний разбор делал из них сегодняшнее число, и
   период растягивался до дня загрузки. Теперь это не дата.

Каждая часть разбирается одним векторным вызовом — без цикла по строкам:
выгрузка бывает на десятки тысяч сообщений.
"""

from __future__ import annotations

import logging

import pandas as pd

LOGGER = logging.getLogger("platform.import_dates")

# Время выгрузок — московское: к нему приводится время с явным поясом.
MOSCOW_TZ = "Europe/Moscow"

# Тип результата — тот, что pandas даёт обычной строке «гггг-мм-дд чч:мм:сс»
# (datetime64[us] в pandas 3, datetime64[ns] в pandas 2). Такой же отдавал и
# прежний разбор, и к нему приводится каждая часть: колонка сообщений не
# должна менять тип от того, в каком виде пришли даты.
RESULT_DTYPE = pd.to_datetime(pd.Series(["2000-01-01 00:00:00"])).dtype

# Регулярки без заглядываний вперёд и назад: строки pandas 3 хранятся в
# pyarrow, а его движок регулярок (RE2) их не знает.
_TIME = r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
_YEAR_FIRST = r"\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:\D|$)"
_ISO_NAIVE = rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}(?:[T ]{_TIME})?"
_ISO_WITH_ZONE = rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}[T ]{_TIME} ?(?:[Zz]|[+-]\d{{2}}(?::?\d{{2}})?)"
# Признак пояса в конце строки, которая не ISO: такие строки разбираются
# отдельно от строк без пояса, иначе смесь поясов уронила бы весь остаток.
# Пояс считается поясом, только если стоит сразу после времени: иначе хвост
# «-2026» у даты «05-04-2026» выглядел бы как пояс «-20:26», и полночь
# такой даты уезжала бы на 03:00 — а время без пояса сдвигать нельзя.
_ZONE_SUFFIX = rf"{_TIME} ?(?:[Zz]|[+-]\d{{2}}(?::?\d{{2}})?|UTC|GMT|utc|gmt)$"
# Время без даты («09:15», «9:15 PM») и слова «today» / «now» прежний разбор
# превращал в сегодняшнее число. Такая «дата» хуже пустой: сообщение уезжает
# в день загрузки, а название периода и period_id растягиваются до него.
_TIME_ONLY = rf"T?{_TIME}(?: ?[AaPp][Mm])?(?: ?(?:[Zz]|[+-]\d{{2}}(?::?\d{{2}})?|UTC|GMT|utc|gmt))?"
_RELATIVE_WORDS = ("today", "now")

# Русская запись: регулярка выбирает строки ровно этого вида, формат их
# разбирает. Каждый формат применяется только к своим строкам, а не ко всему
# столбцу по очереди, — так ни одна строка не разбирается впустую.
_RU_PREFIX = r"\d{1,2}\.\d{1,2}\.\d{2}"
_RU_FORMATS = (
    (r"\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}", "%d.%m.%Y %H:%M:%S"),
    (r"\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}", "%d.%m.%Y %H:%M"),
    (r"\d{1,2}\.\d{1,2}\.\d{4}", "%d.%m.%Y"),
    # Двузначный год прежний разбор понимал (поштучно, через dateutil): 26 —
    # это 2026. Явный формат даёт то же для годов 00–68; 69–99 он относит к
    # XX веку, но таких дат у сообщений из соцсетей не бывает.
    (r"\d{1,2}\.\d{1,2}\.\d{2} \d{1,2}:\d{2}:\d{2}", "%d.%m.%y %H:%M:%S"),
    (r"\d{1,2}\.\d{1,2}\.\d{2} \d{1,2}:\d{2}", "%d.%m.%y %H:%M"),
    (r"\d{1,2}\.\d{1,2}\.\d{2}", "%d.%m.%y"),
)

# Номер дня Excel: 1 — это 01.01.1900, отсчёт от 30.12.1899 учитывает
# несуществующее 29.02.1900 из Excel. Числом считаются только пятизначные
# значения от 1970 до 2100 года: «2026» или номер строки датой не станут.
_EXCEL_SERIAL = r"\d{5}(?:\.\d+)?"
_EXCEL_EPOCH = pd.Timestamp("1899-12-30")
_EXCEL_SERIAL_MIN = 25569  # 01.01.1970
_EXCEL_SERIAL_MAX = 73051  # 01.01.2100, не включая

_SECONDS_PER_DAY = 86400


def _empty_result(index: pd.Index) -> pd.Series:
    return pd.Series(pd.NaT, index=index, dtype=RESULT_DTYPE)


def _naive_moscow(parsed: pd.Series) -> pd.Series:
    """Привести разобранную часть к общему типу: без пояса, московское время."""
    if parsed.dtype == object:
        # pandas 2 на смеси поясов без utc=True отдаёт объекты, а не даты.
        parsed = pd.to_datetime(parsed, errors="coerce", utc=True)
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        parsed = parsed.dt.tz_convert(MOSCOW_TZ).dt.tz_localize(None)
    return parsed.astype(RESULT_DTYPE)


def _parse_rest(values: pd.Series, *, dayfirst: bool) -> pd.Series:
    """Прежний разбор для строк, которые не подошли ни под один явный формат.

    Строки с поясом в конце разбираются отдельно и с utc=True: смесь поясов
    в одном вызове pandas не разбирает, а бросает ValueError — раньше это
    обрывало загрузку целиком. Если и так не вышло, эти даты остаются
    пустыми: сообщения без даты попадут в отчёт о загрузке, а файл загрузится.
    """
    result = _empty_result(values.index)
    zoned = values.str.contains(_ZONE_SUFFIX, regex=True)
    for part, utc in ((values[~zoned], False), (values[zoned], True)):
        if part.empty:
            continue
        try:
            parsed = pd.to_datetime(part, errors="coerce", dayfirst=dayfirst, utc=utc)
        except (ValueError, TypeError, OverflowError) as exc:
            LOGGER.warning(
                "Даты выгрузки не разобраны (%s строк): %s", len(part), type(exc).__name__
            )
            continue
        result.loc[part.index] = _naive_moscow(parsed)
    return result


def _parse_year_first(values: pd.Series) -> pd.Series:
    """Даты, записанные с года, — по ISO, без dayfirst."""
    result = _empty_result(values.index)
    # «2026/04/05» и «2026.04.05» — та же дата с другим разделителем.
    values = values.str.replace(
        r"^(\d{4})[./](\d{1,2})[./](\d{1,2})", r"\1-\2-\3", regex=True
    )
    naive = values.str.fullmatch(_ISO_NAIVE)
    zoned = ~naive & values.str.fullmatch(_ISO_WITH_ZONE)
    if naive.any():
        part = values[naive]
        result.loc[part.index] = _naive_moscow(
            pd.to_datetime(part, format="ISO8601", errors="coerce")
        )
    if zoned.any():
        # utc=True — единственный способ разобрать вместе Z и +03:00; время
        # без пояса сюда не попадает и потому не сдвигается.
        part = values[zoned]
        result.loc[part.index] = _naive_moscow(
            pd.to_datetime(part, format="ISO8601", errors="coerce", utc=True)
        )
    rest = result.isna()
    if rest.any():
        result.loc[rest] = _parse_rest(values[rest], dayfirst=False)
    return result


def _parse_ru(values: pd.Series) -> pd.Series:
    result = _empty_result(values.index)
    todo = values
    for pattern, fmt in _RU_FORMATS:
        # Каждая следующая регулярка проверяет только то, что не подошло под
        # предыдущие: у обычной выгрузки весь столбец в одном виде, и после
        # первого подходящего формата проверять нечего.
        if todo.empty:
            break
        mask = todo.str.fullmatch(pattern)
        if mask.any():
            part = todo[mask]
            result.loc[part.index] = _naive_moscow(
                pd.to_datetime(part, format=fmt, errors="coerce")
            )
            todo = todo[~mask]
    return result


def _excel_serial_values(values: pd.Series) -> pd.Series:
    """Номера дней Excel → даты; вне правдоподобных лет — NaT."""
    numbers = pd.to_numeric(values, errors="coerce")
    plausible = (numbers >= _EXCEL_SERIAL_MIN) & (numbers < _EXCEL_SERIAL_MAX)
    numbers = numbers.where(plausible)
    # Доля суток в Excel хранится дробью с погрешностью: 45383.375 бывает
    # 45383.37499999999. Округление до секунды возвращает ровные 09:00:00.
    stamps = (_EXCEL_EPOCH + pd.to_timedelta(numbers, unit="D")).dt.round("s")
    return _naive_moscow(stamps)


def parse_source_datetimes(series: pd.Series) -> pd.Series:
    """Колонка дат выгрузки → даты без пояса (московское время), NaT — не дата.

    Правила разбора — в описании модуля. Индекс результата — индекс series.
    """
    prepared = (
        series.fillna("").astype(str).str.replace("\xa0", " ", regex=False).str.strip()
    )
    # Работаем на позиционном индексе: у входа он может повторяться, а части
    # колонки собираются обратно по индексу.
    values = prepared.reset_index(drop=True)
    result = _empty_result(values.index)
    # pending — ещё не разобранное. Каждый шаг смотрит только на него: у
    # обычной выгрузки весь столбец в одном виде, и после своего шага
    # остальные не проходят по столбцу зря.
    pending = values.ne("")

    year_first = pending & values.str.match(_YEAR_FIRST)
    if year_first.any():
        result.loc[year_first] = _parse_year_first(values[year_first])
        pending &= ~year_first

    if pending.any():
        ru = pending & values.str.match(_RU_PREFIX)
        if ru.any():
            result.loc[ru] = _parse_ru(values[ru])
            pending &= result.isna()

    if pending.any():
        part = values[pending]
        serial = part[part.str.fullmatch(_EXCEL_SERIAL)]
        if not serial.empty:
            result.loc[serial.index] = _excel_serial_values(serial)
        # Остаток: ни под один явный формат не подошло (или подошло, но дата
        # невозможная, вроде 31.02.2026). Прежний разбор — только для него,
        # кроме времени без даты и «today»/«now»: это не дата, а NaT, и такое
        # сообщение попадёт в счётчик сообщений без даты.
        rest = pending & result.isna()
        if rest.any():
            leftover = values[rest]
            not_a_date = leftover.str.fullmatch(_TIME_ONLY) | leftover.isin(_RELATIVE_WORDS)
            rest.loc[not_a_date[not_a_date].index] = False
        if rest.any():
            result.loc[rest] = _parse_rest(values[rest], dayfirst=True)

    result.index = series.index
    return result


# --- Раздельные «Дата» и «Время» -------------------------------------------

# Ячейка-дата Excel без времени читается как «2026-04-01 00:00:00».
_ISO_MIDNIGHT = r"^(\d{4}-\d{1,2}-\d{1,2})[T ]00:00(?::00(?:\.0+)?)?$"
# Ячейка-дата, в которой уже есть своё время.
_ISO_WITH_TIME = r"^\d{4}-\d{1,2}-\d{1,2}[T ]\d{1,2}:\d{2}"
# Ячейка-время Excel, которую openpyxl отдал датой: «1900-01-01 09:00:00»
# (время больше суток или ячейка с форматом «дата и время»). Остаётся время.
_DATE_BEFORE_TIME = r"^\d{4}-\d{1,2}-\d{1,2}[T ](\d{1,2}:\d{2}.*)$"
# Ячейка-время с форматом «[ч]:мм» и значением больше суток openpyxl отдаёт
# промежутком времени: «1 day, 9:15:00». Остаётся время суток.
_DAYS_BEFORE_TIME = r"^\d+ days?,? (\d{1,2}:\d{2}.*)$"
# Время числом — доля суток: 0.375 — это 09:00. Ячейка без формата бывает и
# больше суток («1.385416666666667» — это 09:15 следующего дня), и записью с
# порядком («1.1574074074074073e-05» — одна секунда). Число больше единицы
# считается долей суток, только если в нём хотя бы три знака после точки:
# так записывает дробь Excel, а «9.30» — это время, написанное человеком.
_DAY_FRACTION = r"(?:0?\.\d+|0|\d+\.\d{3,}|\d*\.?\d+[eE]-\d+)"


def _time_of_day_text(times: pd.Series) -> pd.Series:
    times = times.str.replace(_DATE_BEFORE_TIME, r"\1", regex=True)
    times = times.str.replace(_DAYS_BEFORE_TIME, r"\1", regex=True)
    fraction = times.str.fullmatch(_DAY_FRACTION)
    if fraction.any():
        # Целые сутки отбрасываются: из 1.385416… остаётся 09:15 суток.
        seconds = (
            ((pd.to_numeric(times[fraction], errors="coerce") % 1) * _SECONDS_PER_DAY)
            .round()
            .clip(upper=_SECONDS_PER_DAY - 1)
        )
        times = times.copy()
        times.loc[fraction] = (
            pd.Timestamp("2000-01-01") + pd.to_timedelta(seconds, unit="s")
        ).dt.strftime("%H:%M:%S")
    return times


def _serial_date_text(dates: pd.Series) -> pd.Series:
    """Номер дня Excel в колонке даты → «гггг-мм-дд чч:мм:сс» для склейки.

    Дробный номер («46113.385416…» — ячейка «дата и время» без формата) несёт
    своё время и потому не дополняется колонкой «Время». У целого номера
    остаётся полночь, её следом убирает склейка — и время берётся из колонки.
    """
    serial = dates.str.fullmatch(_EXCEL_SERIAL)
    if not serial.any():
        return dates
    stamps = _excel_serial_values(dates[serial])
    dates = dates.copy()
    known = stamps.notna()
    dates.loc[known[known].index] = stamps[known].dt.strftime("%Y-%m-%d %H:%M:%S")
    return dates


def join_date_and_time(dates: pd.Series, times: pd.Series) -> pd.Series:
    """Склеить раздельные колонки «Дата» и «Время» в одну строку даты.

    Так выгружает, например, Excel Brand Analytics. Пока это были текстовые
    «24.04.2026» и «09:30», хватало склеить через пробел. Но ячейки Excel
    приезжают иначе: дата — «2026-04-01 00:00:00», время — «09:30:00», а
    иногда «1900-01-01 09:30:00» или число 0.395833. Склейка давала
    «2026-04-01 00:00:00 09:30:00», и прежний разбор читал её как 4 января.

    Теперь у даты-ячейки отбрасывается полночь, у времени-ячейки — дата 1900
    года или целые сутки («1 day, 9:15:00»), доля суток становится
    «чч:мм:сс», номер дня Excel — датой. Если в ячейке даты уже есть своё
    время, колонка «Время» к ней не дописывается. Текстовые значения
    склеиваются как раньше, через пробел.
    """
    date_text = dates.fillna("").astype(str).str.strip()
    time_text = _time_of_day_text(times.fillna("").astype(str).str.strip())
    day = _serial_date_text(date_text).str.replace(_ISO_MIDNIGHT, r"\1", regex=True)
    # Пустая дата склеивается, как раньше, в одно время — «09:15». Датой оно
    # не станет: parse_source_datetimes не превращает время без даты в
    # сегодняшнее число, и сообщение попадёт в счётчик сообщений без даты.
    has_time = time_text.ne("")
    own_time = day.str.match(_ISO_WITH_TIME)
    joined = (day + " " + time_text).str.strip()
    # Своё время в ячейке даты: остаётся она сама, но номер дня Excel — уже
    # датой со временем. Строки без «Времени» не трогаются вовсе.
    out = date_text.where(~(has_time & own_time), day)
    return out.where(~(has_time & ~own_time), joined)
