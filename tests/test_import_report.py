"""Диагностика импорта: что платформа прочитала, а что не поняла.

Синонимов имён колонок не хватит никогда. У одной системы они различаются между
форматами («Просмотры» против «Просмотров»), у разных систем совпадают редко
(«Аудитория» против «Аудитории блога»). Пока платформа молчит о непонятых
колонках, потеря обнаруживается случайно и спустя месяцы: аудитория у проектов
на Медиалогии была нулевой ровно поэтому.

Отчёт строится из того, что произошло при разборе, а не из отдельного списка
синонимов — список пришлось бы держать рядом с шестью десятками вызовов, и
разошедшийся с кодом список врал бы.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from import_adapters import canonicalize_table  # noqa: E402
from services.import_report import (  # noqa: E402
    normalization_lines,
    summarize_import,
)

failures = []


def check(label, condition, detail=""):
    print(
        ("  ✓ " if condition else "  ✗ ")
        + label
        + (f" — {detail}" if detail and not condition else "")
    )
    if not condition:
        failures.append(label)


def columns_of(report, key):
    return [item["column"] for item in report.get(key, [])]


print("1. Знакомый формат разбирается без остатка")
report = {}
canonicalize_table(
    pd.DataFrame(
        [
            {
                "Дата": "24.04.2026",
                "Текст": "Сообщение про кровлю",
                "Url": "https://vk.com/1",
                "Источник": "vk.com",
                "Тип источника": "Соцсети",
                "Тональность": "позитив",
                "Аудитория": "1000",
            }
        ]
    ),
    report=report,
)
check("строки посчитаны", report["rows"] == 1, str(report["rows"]))
check("колонки посчитаны", report["source_columns"] == 7, str(report["source_columns"]))
check("непонятых колонок нет", report["unrecognized"] == [], str(columns_of(report, "unrecognized")))
check("система определена", report["detected_system"] == "generic", report["detected_system"])

print("2. Незнакомая колонка с данными видна")
# Ради этого всё и затевалось: чужой шаблон должен быть виден в момент
# загрузки, а не через месяцы расхождений в отчётах.
report = {}
canonicalize_table(
    pd.DataFrame(
        [
            {"Дата": "24.04.2026", "Текст": "раз",
             "Индекс заметности": "72", "Рубрика издания": "Спорт"},
            {"Дата": "25.04.2026", "Текст": "два",
             "Индекс заметности": "9", "Рубрика издания": ""},
        ]
    ),
    report=report,
)
unknown = columns_of(report, "unrecognized")
check("обе незнакомые колонки в отчёте", set(unknown) == {"Индекс заметности", "Рубрика издания"},
      str(unknown))
first = report["unrecognized"][0]
check("сначала самая заполненная", first["column"] == "Индекс заметности", first["column"])
check("заполненность посчитана", first["filled"] == 2, str(first["filled"]))
check("доля посчитана", abs(first["share"] - 1.0) < 1e-9, str(first["share"]))
check("есть примеры значений", "72" in first["sample"], first["sample"])
half = [i for i in report["unrecognized"] if i["column"] == "Рубрика издания"][0]
check("частично заполненная считается по факту", half["filled"] == 1, str(half["filled"]))

print("3. Пустая незнакомая колонка тревоги не поднимает")
# Системы отдают десятки колонок, которые у конкретного клиента не заполнены.
# Сообщать о них как о потере — значит приучить не читать предупреждения.
report = {}
canonicalize_table(
    pd.DataFrame([{"Дата": "24.04.2026", "Текст": "раз", "Избранное": "", "Прочитано": ""}]),
    report=report,
)
check("в непонятые не попала", columns_of(report, "unrecognized") == [], str(columns_of(report, "unrecognized")))
check("но в списке пустых есть", set(report["empty"]) == {"Избранное", "Прочитано"}, str(report["empty"]))

print("4. Служебные колонки не считаются потерей")
# «Обработано» — маркер начала тегов: по нему платформа находит колонки-теги,
# а само значение ей не нужно.
report = {}
canonicalize_table(
    pd.DataFrame(
        [
            {
                "Дата": "24.04.2026",
                "Текст": "раз",
                "Hash сообщения": "abc",
                "ID сообщения": "1",
                "Источник": "vk.com",
                "Url": "https://vk.com/1",
                "Тип источника": "Соцсети",
                "Обработано": "Да",
                "Docke": "Docke",
            }
        ]
    ),
    report=report,
)
check("формат опознан как Brand Analytics", report["detected_system"] == "brand_analytics",
      report["detected_system"])
check("«Обработано» не в потерях", "Обработано" not in columns_of(report, "unrecognized"),
      str(columns_of(report, "unrecognized")))
check("колонка-тег учтена как прочитанная", report["tag_columns"] == ["Docke"],
      str(report["tag_columns"]))
check("«Hash сообщения» больше не теряется", "Hash сообщения" not in columns_of(report, "unrecognized"),
      str(columns_of(report, "unrecognized")))

print("5. Выправленные ячейки посчитаны")
report = {}
canonicalize_table(
    pd.DataFrame(
        [
            {"Дата": "24.04.2026", "Сообщение": "Поступление&#33; Цена &gt; 100",
             "Аудитория": "2 786"},
            {"Дата": "25.04.2026", "Сообщение": "Текст_x000d_\nвторая строка",
             "Аудитория": "12 045"},
        ]
    ),
    report=report,
)
normalized = report["normalized"]
check("мнемоники посчитаны", normalized.get("html_entities") == 1, str(normalized))
check("экранированный перевод строки посчитан", normalized.get("excel_cr") == 1, str(normalized))
check("числа с пробелом посчитаны", normalized.get("grouped_numbers") == 2, str(normalized))
lines = normalization_lines(report)
check("описание человекочитаемое", any("мнемоник" in line for line in lines), str(lines))
check("порядок по убыванию", lines[0].endswith("2"), str(lines))

print("6. Амперсанд без мнемоники в счёт не идёт")
# Иначе отчёт сообщал бы о правке там, где ничего не менялось.
report = {}
canonicalize_table(
    pd.DataFrame([{"Дата": "24.04.2026", "Сообщение": "Иванов & Партнёры"}]),
    report=report,
)
check("счётчик пустой", not report["normalized"].get("html_entities"), str(report["normalized"]))

print("7. Одна строка для журнала")
report = {}
canonicalize_table(
    pd.DataFrame([{"Дата": "24.04.2026", "Текст": "раз", "Незнакомая": "значение"}]),
    report=report,
)
line = summarize_import(report)
check("строка содержит числа", "1 строк" in line and "не распознано" in line, line)
check("пустой отчёт не падает", summarize_import({}) == "")
check("пустые строки нормализации не падают", normalization_lines({}) == [])

print("8. Отчёт не обязателен")
# Без запроса отчёта разбор должен идти как раньше: диагностика не вправе
# ничего менять в результате.
plain = canonicalize_table(pd.DataFrame([{"Дата": "24.04.2026", "Текст": "раз"}]))
with_report = canonicalize_table(
    pd.DataFrame([{"Дата": "24.04.2026", "Текст": "раз"}]), report={}
)
check(
    "результат одинаковый",
    list(plain.columns) == list(with_report.columns) and len(plain) == len(with_report),
    f"{len(plain.columns)} против {len(with_report.columns)}",
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Диагностика импорта работает.")
