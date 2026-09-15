"""Проверка разбора выгрузок: определение системы-источника и приведение к канону.

Через этот модуль проходит каждый загруженный клиентом файл, и ломается он
как раз тогда, когда приходит выгрузка нового формата. Тесты фиксируют
поведение на синтетических таблицах — без сети и (кроме проверки починки
битого xlsx) без файлов на диске.
"""

import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from import_adapters import (  # noqa: E402
    CANONICAL_COLUMNS,
    _brand_analytics_tag_columns,
    _clean_col_name,
    _clean_dataframe,
    _normalize_bool_text,
    _normalize_sentiment,
    _normalize_topics_list,
    canonicalize_table,
    detect_source_system,
    first_existing,
    read_source_table,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Определение системы-источника по колонкам")
check(
    "Brand Analytics: Hash/Источник/Url + ID сообщения",
    detect_source_system(
        pd.DataFrame(columns=["Hash сообщения", "ID сообщения", "Источник", "Url"])
    )
    == "brand_analytics",
)
check(
    "без «ID сообщения» это уже не Brand Analytics",
    detect_source_system(pd.DataFrame(columns=["Hash сообщения", "Источник"]))
    != "brand_analytics",
)
check(
    "Медиалогия Excel по «Кто пишет»",
    detect_source_system(pd.DataFrame(columns=["Кто пишет", "Сообщение"]))
    == "mediologia_excel",
)
check(
    "Медиалогия CSV по «Профиль блога»",
    detect_source_system(pd.DataFrame(columns=["Профиль блога", "Сообщение"]))
    == "mediologia",
)
check(
    "незнакомый формат — универсальный",
    detect_source_system(pd.DataFrame(columns=["Дата", "Текст"])) == "generic",
)

print("2. Канонический контракт таблицы")
raw = pd.DataFrame(
    [{"Дата": "24.04.2026", "Текст": "Привет", "Ссылка": "https://t.me/x/1"}]
)
canon = canonicalize_table(raw, source_file="/tmp/выгрузка недели.xlsx")
check(
    "все канонические колонки на месте",
    set(CANONICAL_COLUMNS).issubset(canon.columns),
    str(sorted(set(CANONICAL_COLUMNS) - set(canon.columns))),
)
check(
    "порядок колонок канонический",
    list(canon.columns)[: len(CANONICAL_COLUMNS)] == list(CANONICAL_COLUMNS),
    str(list(canon.columns)[:5]),
)
check("текст подхвачен из «Текст»", canon.loc[0, "Сообщение"] == "Привет")
check(
    "source_file сведён к имени файла",
    canon.loc[0, "source_file"] == "выгрузка недели.xlsx",
    str(canon.loc[0, "source_file"]),
)

print("3. Склейка раздельных «Дата» и «Время»")
split_dt = pd.DataFrame(
    [
        {"Дата": "24.04.2026", "Время": "09:30", "Сообщение": "раз"},
        {"Дата": "25.04.2026", "Время": "", "Сообщение": "два"},
    ]
)
merged = canonicalize_table(split_dt)
check("дата и время склеены", merged.loc[0, "Дата"] == "24.04.2026 09:30", str(merged.loc[0, "Дата"]))
check(
    "строка без времени осталась просто датой",
    merged.loc[1, "Дата"] == "25.04.2026",
    str(merged.loc[1, "Дата"]),
)
check("колонка «Время» не попадает в канон", "Время" not in CANONICAL_COLUMNS)

print("4. Brand Analytics: заголовок приклеивается к тексту")
ba_raw = pd.DataFrame(
    [
        {
            "ID сообщения": "m1",
            "Hash сообщения": "h1",
            "Источник": "vk.com",
            "Url": "https://vk.com/1",
            "Заголовок": "Завод открыт",
            "Сообщение": "Подробности в тексте.",
        }
    ]
)
ba_canon = canonicalize_table(ba_raw, source_system="auto")
check(
    "заголовок и текст склеены для BA",
    "Завод открыт" in ba_canon.loc[0, "Сообщение"]
    and "Подробности" in ba_canon.loc[0, "Сообщение"],
    str(ba_canon.loc[0, "Сообщение"]),
)
generic_canon = canonicalize_table(
    pd.DataFrame([{"Заголовок": "Завод открыт", "Сообщение": "Подробности в тексте."}])
)
check(
    "для не-BA заголовок в текст не подмешивается",
    generic_canon.loc[0, "Сообщение"] == "Подробности в тексте.",
    str(generic_canon.loc[0, "Сообщение"]),
)

print("5. Теговые колонки Brand Analytics после «Обработано»")
tagged = pd.DataFrame(
    [
        {"Сообщение": "раз", "Обработано": "да", "ТЕХНОНИКОЛЬ": "ТЕХНОНИКОЛЬ", "Пусто": "", "отзыв": ""},
        {"Сообщение": "два", "Обработано": "да", "ТЕХНОНИКОЛЬ": "", "Пусто": "", "отзыв": "отзыв"},
    ]
)
tag_cols = _brand_analytics_tag_columns(tagged)
check("колонки после маркера распознаны", "ТЕХНОНИКОЛЬ" in tag_cols and "отзыв" in tag_cols, str(tag_cols))
check("полностью пустая колонка не считается тегом", "Пусто" not in tag_cols, str(tag_cols))
check(
    "без маркера «Обработано» теговых колонок нет",
    _brand_analytics_tag_columns(pd.DataFrame([{"Сообщение": "раз", "ТЕХНОНИКОЛЬ": "да"}])) == [],
)

print("6. Нормализация значений")
sentiment = _normalize_sentiment(pd.Series(["негатив", "Позитивная", "neutral", "непонятно"]))
check("негатив приведён к канону", sentiment.iloc[0] == "негативная", str(sentiment.tolist()))
check("позитив приведён к канону", sentiment.iloc[1] == "позитивная", str(sentiment.tolist()))
check("нейтрал приведён к канону", sentiment.iloc[2] == "нейтральная", str(sentiment.tolist()))
check("незнакомое значение не выдумывается", sentiment.iloc[3] == "непонятно", str(sentiment.tolist()))
check("«да» → True", _normalize_bool_text("да") == "True")
check("«нерелевантно» → False", _normalize_bool_text("нерелевантно") == "False")
check(
    "список тем из питоновского литерала",
    _normalize_topics_list("['Цены', 'Качество']") == "Цены; Качество",
    _normalize_topics_list("['Цены', 'Качество']"),
)
check(
    "список тем через разделители, без дублей",
    _normalize_topics_list("Цены|Качество|Цены") == "Цены; Качество",
    _normalize_topics_list("Цены|Качество|Цены"),
)

print("7. Поиск колонки по псевдонимам")
df3 = pd.DataFrame([{"A": "1"}, {"A": "2"}, {"A": "3"}])
missing = first_existing(df3, ["Нет такой", "И такой нет"])
check("отсутствующая колонка даёт Series нужной длины", len(missing) == len(df3), str(len(missing)))
check("значения пустые, а не NaN-ловушка", all(str(x).strip() == "" for x in missing), str(missing.tolist()))
check(
    "поиск нечувствителен к регистру",
    first_existing(pd.DataFrame([{"СсЫлКа": "u"}]), ["ссылка"]).iloc[0] == "u",
)

print("8. Чистка заголовков и пустых строк")
check("BOM убирается", _clean_col_name("﻿Дата") == "Дата")
check("неразрывный пробел убирается", _clean_col_name("Тип\xa0источника") == "Тип источника")
check("Unnamed-колонка отбрасывается", _clean_col_name("Unnamed: 3") == "")
dirty = pd.DataFrame(
    [
        {"﻿Дата": "24.04.2026", "Unnamed: 1": "", "Сообщение": "раз"},
        {"﻿Дата": "", "Unnamed: 1": "", "Сообщение": ""},
    ]
)
cleaned = _clean_dataframe(dirty)
check("служебная колонка удалена", "Unnamed: 1" not in cleaned.columns, str(list(cleaned.columns)))
check("полностью пустая строка удалена", len(cleaned) == 1, str(cleaned.to_dict("records")))

print("9. Битый xl/styles.xml чинится, а не роняет загрузку")
with TemporaryDirectory() as tmp:
    good = Path(tmp) / "ok.xlsx"
    with pd.ExcelWriter(good, engine="openpyxl") as writer:
        pd.DataFrame([{"Дата": "24.04.2026", "Сообщение": "раз"}]).to_excel(
            writer, sheet_name="Сообщения", index=False
        )
    broken = Path(tmp) / "broken.xlsx"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(broken, "w") as dst:
        for item in src.infolist():
            payload = src.read(item.filename)
            if item.filename == "xl/styles.xml":
                payload = b"<styleSheet>"  # обрезанный, невалидный XML
            dst.writestr(item, payload)
    try:
        repaired = read_source_table(broken)
        check("битый файл всё равно прочитан", len(repaired) == 1, str(repaired.to_dict("records")))
        check("данные не потеряны при починке", repaired.loc[0, "Сообщение"] == "раз")
    except Exception as exc:  # noqa: BLE001
        check("битый файл всё равно прочитан", False, f"{type(exc).__name__}: {exc}")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки разбора выгрузок пройдены.")
