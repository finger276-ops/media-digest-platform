"""Как узнать период, загруженный с неверными датами.

До версии 4.12.8 даты, записанные с года (так приезжает ячейка-дата Excel:
«2026-04-05 09:00:00»), разбирались как «год-день-месяц». У такого периода
видны следы (services/date_parsing.py — почему так было):

- у многих сообщений нет даты — дни после 12-го не разбирались;
- даты границ — одно и то же число разных месяцев: апрельская выгрузка
  становилась «04.01.2026–04.12.2026», недельная за 1–7 апреля —
  «04.01.2026–04.07.2026»;
- период тянется на месяцы, хотя выгрузка недельная или месячная.

Есть и тихий случай: текстовые даты «дд.мм.гггг», у части которых есть
время, а у части нет (например, пустое «Время» в выгрузке Brand Analytics).
Прежний разбор брал формат первой строки и терял даты другого вида. Границы
такого периода выглядят правдоподобно, счётчика сообщений без даты в старом
манифесте нет — виден он только по сообщениям (messages_date_problems).

Здесь только проверки: модуль ничего не читает из базы и ничего не пишет.
Читает scripts/check_period_dates.py — и только читает.
"""

from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from .import_report import UNDATED_WARNING_SHARE
from .message_normalize import UNDATED_MESSAGES

# Дольше этого выгрузки не бывают: недельная, месячная, редко за два месяца.
# Длиннее — не обязательно ошибка, но повод посмотреть на даты сообщений.
LONG_PERIOD_DAYS = 62

# Доля сообщений с одним и тем же числом месяца, при которой это не
# совпадение: настоящая выгрузка раскладывается по разным числам.
SAME_DAY_SHARE = 0.9

# Сколько месяцев должно быть между границами с одним числом, чтобы это
# было похоже на перестановку. Обычный месячный период «05.03–05.04» тоже
# имеет одно число в соседних месяцах; перепутанная выгрузка даже за три дня
# («01–03.04» → «04.01–04.03») разносит границы хотя бы на два месяца.
SWAPPED_MIN_MONTHS = 2

# Сколько разных месяцев должно быть у сообщений с одним и тем же числом.
# Выгрузка за сутки на стыке месяцев («31.03» и «01.04») — два месяца, и
# почти все сообщения в ней — 1-е число; перепутанная разбегается шире.
SWAPPED_MIN_MESSAGE_MONTHS = 3


def _day(value: Any) -> pd.Timestamp | None:
    stamp = pd.to_datetime(value, errors="coerce")
    if stamp is None or pd.isna(stamp):
        return None
    stamp = pd.Timestamp(stamp)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.normalize()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def period_date_problems(period: Mapping[str, Any]) -> list[str]:
    """Признаки неверных дат по строке периода — без чтения сообщений.

    period — строка platform_periods (date_from, date_to, manifest). Пустой
    список — признаков нет. Счётчик сообщений без даты есть в манифесте только
    у периодов, загруженных с версии 4.12.8; у прежних работают признаки дат.
    """
    problems: list[str] = []
    manifest = period.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    undated = _int(manifest.get(UNDATED_MESSAGES))
    total = _int(manifest.get("rows_messages"))
    if undated and total and undated / total > UNDATED_WARNING_SHARE:
        problems.append(f"без даты {undated} из {total} сообщений")

    start, end = _day(period.get("date_from")), _day(period.get("date_to"))
    if start is None or end is None:
        return problems
    months_apart = (end.year - start.year) * 12 + (end.month - start.month)
    if start.day == end.day and start.day <= 12 and months_apart >= SWAPPED_MIN_MONTHS:
        problems.append(
            f"обе границы — {start.day}-е число разных месяцев: похоже, день и "
            "месяц перепутаны"
        )
    span = (end - start).days + 1
    if span > LONG_PERIOD_DAYS:
        problems.append(f"период на {span} дн. — длиннее обычной выгрузки")
    return problems


def messages_date_problems(messages: pd.DataFrame | None) -> list[str]:
    """Признаки неверных дат по сообщениям периода.

    Точнее признаков по строке периода, но требует прочитать все сообщения:
    доля сообщений без даты и «все даты — одно число разных месяцев».
    """
    if messages is None or messages.empty or "datetime" not in messages.columns:
        return []
    # utc=True — чтобы строка с поясом не уронила разбор; время без пояса
    # при этом не сдвигается, а пояс тут же снимается.
    stamps = pd.to_datetime(messages["datetime"], errors="coerce", utc=True).dt.tz_localize(None)
    total = len(stamps)
    problems: list[str] = []
    undated = int(stamps.isna().sum())
    if undated / total > UNDATED_WARNING_SHARE:
        problems.append(f"в сообщениях без даты {undated} из {total}")
    dated = stamps.dropna()
    if len(dated) >= 2 and dated.dt.to_period("M").nunique() >= SWAPPED_MIN_MESSAGE_MONTHS:
        days = dated.dt.day
        top_day = int(days.mode().iloc[0])
        if top_day <= 12 and (days == top_day).mean() >= SAME_DAY_SHARE:
            problems.append(
                f"даты сообщений — почти все {top_day}-е число разных месяцев: "
                "похоже, день и месяц перепутаны"
            )
    return problems
