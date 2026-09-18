"""Проверка разбора выгрузок: определение системы-источника и приведение к канону.

Через этот модуль проходит каждый загруженный клиентом файл, и ломается он
как раз тогда, когда приходит выгрузка нового формата. Тесты фиксируют
поведение на синтетических таблицах — без сети и (кроме проверки починки
битого xlsx) без файлов на диске.
"""

import sys
import tempfile
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
    get_excel_sheet_names,
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
    # Починка делает временную копию файла размером с исходную выгрузку.
    # Если её не удалять, каждая загрузка битого xlsx оставляет мусор в temp —
    # на сервере это растёт молча, пока не кончится место.
    def leftovers():
        return set(Path(tempfile.gettempdir()).glob("xlsx_styles_repaired_*"))

    before = leftovers()
    try:
        repaired = read_source_table(broken)
        check("битый файл всё равно прочитан", len(repaired) == 1, str(repaired.to_dict("records")))
        check("данные не потеряны при починке", repaired.loc[0, "Сообщение"] == "раз")
    except Exception as exc:  # noqa: BLE001
        check("битый файл всё равно прочитан", False, f"{type(exc).__name__}: {exc}")
    check(
        "чтение не оставило временную копию",
        not (leftovers() - before),
        str(sorted(p.name for p in leftovers() - before)),
    )

    before = leftovers()
    names = get_excel_sheet_names(broken)
    check("список листов битого файла получен", names == ["Сообщения"], str(names))
    check(
        "список листов не оставил временную копию",
        not (leftovers() - before),
        str(sorted(p.name for p in leftovers() - before)),
    )

    # Худший случай: расширение .xlsx, а внутри вовсе не zip. Починка успевает
    # создать временный файл и падает — убрать его должна она сама.
    not_a_zip = Path(tmp) / "fake.xlsx"
    not_a_zip.write_bytes(b"Date;Text\n24.04.2026;raz\n")
    before = leftovers()
    try:
        read_source_table(not_a_zip)
        check("не-zip с расширением .xlsx отвергнут", False, "чтение неожиданно удалось")
    except Exception:  # noqa: BLE001 — понятная ошибка пользователю ожидаема
        check("не-zip с расширением .xlsx отвергнут", True)
    check(
        "неудавшаяся починка не оставила временную копию",
        not (leftovers() - before),
        str(sorted(p.name for p in leftovers() - before)),
    )

print("10. Многолистовая выгрузка: лист с сообщениями находится сам")
# Выгрузки мониторинга редко состоят из одного листа: сверху обложка, рядом
# сводки и служебные вкладки. Раньше этот путь не был покрыт ничем, хотя через
# него проходит каждый загруженный xlsx.
MESSAGES = [
    {
        "Дата": "24.04.2026",
        "Сообщение": f"Сообщение номер {i} про кровельные материалы",
        "Ссылка": f"https://example.com/{i}",
        "Автор": f"user{i}",
        "Площадка": "vk.com",
        "Тональность": ["позитив", "негатив", "нейтрал"][i % 3],
    }
    for i in range(3)
]

with TemporaryDirectory() as tmp:
    multi = Path(tmp) / "multi.xlsx"
    with pd.ExcelWriter(multi, engine="openpyxl") as writer:
        pd.DataFrame([{"Отчёт": "Еженедельный мониторинг", "Период": "24.04–30.04"}]).to_excel(
            writer, sheet_name="Обложка", index=False
        )
        pd.DataFrame().to_excel(writer, sheet_name="Пустой", index=False)
        pd.DataFrame([{"Площадка": "vk.com", "Сообщений": 12}]).to_excel(
            writer, sheet_name="Источники", index=False
        )
        pd.DataFrame(MESSAGES).to_excel(writer, sheet_name="Сообщения", index=False)

    names = get_excel_sheet_names(multi)
    check(
        "все листы перечислены в порядке книги",
        names == ["Обложка", "Пустой", "Источники", "Сообщения"],
        str(names),
    )
    table = read_source_table(multi)
    check("прочитан лист с сообщениями, а не обложка", len(table) == 3, str(len(table)))
    check(
        "текст сообщения на месте",
        "кровельные материалы" in str(table.loc[0, "Сообщение"]),
        str(table.loc[0, "Сообщение"]),
    )
    check(
        "пустой служебный лист не сломал выбор",
        "Отчёт" not in set(table.columns),
        str(list(table.columns))[:200],
    )

    # Имя листа — только подсказка: у части систем он называется как угодно,
    # и выбирать приходится по составу колонок.
    odd = Path(tmp) / "odd_names.xlsx"
    with pd.ExcelWriter(odd, engine="openpyxl") as writer:
        pd.DataFrame([{"Показатель": "Всего", "Значение": 3}]).to_excel(
            writer, sheet_name="Свод", index=False
        )
        pd.DataFrame(MESSAGES).to_excel(writer, sheet_name="Лист1", index=False)
    table = read_source_table(odd)
    check(
        "лист найден по колонкам, а не по названию",
        len(table) == 3 and "кровельные материалы" in str(table.loc[0, "Сообщение"]),
        str(table.to_dict("records"))[:200],
    )

    # Явное указание листа должно перебивать автоматический выбор.
    only_sources = read_source_table(multi, sheet_name="Источники")
    check(
        "явно указанный лист читается вместо угаданного",
        len(only_sources) == 1,
        str(only_sources.to_dict("records"))[:200],
    )

print("11. Метрики Brand Analytics в родительном падеже")
# Brand Analytics называет колонки метрик «Лайков», «Репостов», «Комментариев»,
# «Просмотров», «Дублей» — не так, как они называются в каноне. Родительная форма
# была учтена не везде, и лайки с репостами молча обнулялись на каждой выгрузке:
# заметить это по отчёту нельзя, потому что общая вовлечённость приезжает
# отдельной колонкой и выглядит правдоподобно.
ba_metrics = pd.DataFrame(
    [
        {
            "Дата": "24.04.2026",
            "Текст": "Обзор кровли",
            "Url": "https://vk.com/wall-1_1",
            "Источник": "vk.com",
            "Аудитория": "1434",
            "Просмотров": "152",
            "Вовлеченность": "48",
            "Лайков": "40",
            "Репостов": "3",
            "Комментариев": "5",
            "Дублей": "7",
        }
    ]
)
ba = canonicalize_table(ba_metrics)
for canon_col, raw_col, expected in (
    ("Лайки", "Лайков", "40"),
    ("Репосты", "Репостов", "3"),
    ("Комментарии", "Комментариев", "5"),
    ("Просмотры", "Просмотров", "152"),
    ("Количество дублей", "Дублей", "7"),
    ("Вовлечённость", "Вовлеченность", "48"),
    ("Аудитория", "Аудитория", "1434"),
):
    got = str(ba.loc[0, canon_col]).strip()
    check(f"«{raw_col}» → «{canon_col}»", got == expected, f"получено {got!r}")

# Вовлечённость приходит отдельной колонкой и равна сумме составляющих: если
# разбивка потеряется, итог всё равно сойдётся — потому проверяем именно части.
parts = sum(int(str(ba.loc[0, c]).strip() or 0) for c in ("Лайки", "Репосты", "Комментарии"))
check(
    "сумма лайков, репостов и комментариев равна вовлечённости",
    parts == int(str(ba.loc[0, "Вовлечённость"]).strip()),
    f"{parts} против {ba.loc[0, 'Вовлечённость']}",
)

# Именительный падеж других выгрузок ломаться не должен.
plain = canonicalize_table(
    pd.DataFrame([{"Дата": "24.04.2026", "Сообщение": "раз", "Лайки": "9", "Репосты": "2"}])
)
check("именительный падеж по-прежнему читается",
      str(plain.loc[0, "Лайки"]).strip() == "9" and str(plain.loc[0, "Репосты"]).strip() == "2",
      f"{plain.loc[0, 'Лайки']!r}, {plain.loc[0, 'Репосты']!r}")

print("12. Заголовок и тип площадки доезжают отдельными колонками")
# Brand Analytics различает «Тип сообщения» (пост, репост, комментарий) и
# «Тип источника» (соцсети, отзывы, видео, СМИ). Второе терялось, а именно оно
# отделяет жалобу покупателя от новости рынка. Заголовок терялся иначе — он
# приклеивался к тексту и становился неотличим от первой строки поста.
ba_shape = pd.DataFrame(
    [
        {
            "Дата": "24.04.2026",
            "Заголовок": "Анализ рынка элементов вентиляции кровли",
            "Текст": "Полный текст обзора",
            "Url": "https://example.com/1",
            "Источник": "dzen.ru",
            "Тип сообщения": "Пост",
            "Тип источника": "Блоги",
            "Hash сообщения": "abc",
            "ID сообщения": "1",
        },
        {
            "Дата": "24.04.2026",
            "Заголовок": "DOCKE / Гибкая черепица 3м2",
            "Текст": "Недостатки: слабая клейкость",
            "Url": "https://wildberries.ru/2",
            "Источник": "wildberries.ru",
            "Тип сообщения": "Комментарий",
            "Тип источника": "Отзывы",
            "Hash сообщения": "def",
            "ID сообщения": "2",
        },
    ]
)
shaped = canonicalize_table(ba_shape)
check("«Заголовок» есть в каноне", "Заголовок" in shaped.columns)
check("«Тип площадки» есть в каноне", "Тип площадки" in shaped.columns)
check(
    "заголовок сохранён отдельно",
    str(shaped.loc[0, "Заголовок"]).strip() == "Анализ рынка элементов вентиляции кровли",
    str(shaped.loc[0, "Заголовок"]),
)
check(
    "тип источника попал в «Тип площадки», а не потерялся",
    list(shaped["Тип площадки"]) == ["Блоги", "Отзывы"],
    str(list(shaped["Тип площадки"])),
)
check(
    "«Тип» по-прежнему держит тип сообщения",
    list(shaped["Тип"]) == ["Пост", "Комментарий"],
    str(list(shaped["Тип"])),
)
# Склейка заголовка с текстом кормит поиск и саммари — её поведение не меняем.
check(
    "заголовок по-прежнему приклеен к тексту сообщения",
    "Анализ рынка" in str(shaped.loc[0, "Сообщение"])
    and "Полный текст обзора" in str(shaped.loc[0, "Сообщение"]),
    str(shaped.loc[0, "Сообщение"])[:80],
)
check(
    "отзыв маркетплейса отличим от новости",
    (shaped["Тип площадки"] == "Отзывы").sum() == 1,
    str(list(shaped["Тип площадки"])),
)

# Выгрузка без этих колонок не должна падать — просто пустые значения.
plain_shape = canonicalize_table(
    pd.DataFrame([{"Дата": "24.04.2026", "Сообщение": "раз"}])
)
check(
    "без заголовка и типа источника колонки пустые, но есть",
    str(plain_shape.loc[0, "Заголовок"]).strip() == ""
    and str(plain_shape.loc[0, "Тип площадки"]).strip() == "",
    f"{plain_shape.loc[0, 'Заголовок']!r}, {plain_shape.loc[0, 'Тип площадки']!r}",
)

print("13. Собственные поля Brand Analytics не становятся тегами")
# Позиция маркера «Обработано» не фиксирована. В xlsx-выгрузке RUFLEX за август
# после него шли только теги, а в csv-выгрузке того же периода за ним оказались
# «Место» и «Адрес» — география публикации. Без отсева заказчик видел бы в
# статистике тегов «Россия, Московская область» рядом с «Docke».
tag_layout = pd.DataFrame(
    [
        {
            "Дата": "24.04.2026",
            "Текст": "Сообщение",
            "Hash сообщения": "abc",
            "ID сообщения": "1",
            "Источник": "vk.com",
            "Url": "https://vk.com/1",
            "Тип источника": "Соцсети",
            "Обработано": "Да",
            "Место": "Руфлекс",
            "Адрес": "Россия, Московская область",
            "Язык": "Русский",
            "Docke": "Docke",
            "Кровля": "Кровля",
        }
    ]
)
found = _brand_analytics_tag_columns(tag_layout)
check("теги проекта найдены", found == ["Docke", "Кровля"], str(found))
check("«Место» не попало в теги", "Место" not in found, str(found))
check("«Адрес» не попал в теги", "Адрес" not in found, str(found))
check("«Язык» не попал в теги", "Язык" not in found, str(found))

canon_tags = canonicalize_table(tag_layout)
check(
    "в каноне тоже только теги проекта",
    str(canon_tags.loc[0, "source_tag_columns"]) == "Docke|Кровля",
    str(canon_tags.loc[0, "source_tag_columns"]),
)
# «Место» и «Адрес» — не мусор, а поля системы: они должны доехать до канона
# как данные, но не как теги проекта. Разница принципиальная: тег попадает в
# статистику тегов и в названия инфоповодов, поле — нет.
check(
    "география доехала до канона полем",
    {"Место", "Адрес"} <= set(canon_tags.columns)
    and str(canon_tags.loc[0, "Адрес"]) == "Россия, Московская область",
    str(canon_tags.loc[0].get("Адрес")),
)

# Порядок колонок между форматами различается — набор тегов меняться не должен.
reordered = tag_layout[
    ["Дата", "ID сообщения", "Hash сообщения", "Текст", "Источник", "Url",
     "Тип источника", "Обработано", "Docke", "Кровля", "Место", "Адрес", "Язык"]
]
check(
    "перестановка колонок не меняет набор тегов",
    _brand_analytics_tag_columns(reordered) == ["Docke", "Кровля"],
    str(_brand_analytics_tag_columns(reordered)),
)

print("14. HTML-мнемоники расшифровываются на входе")
# Brand Analytics отдаёт часть текстов так, как они лежали в разметке страницы.
# В выгрузках RUFLEX мнемоники нашлись в 3–5% строк, и в xlsx, и в csv. Чинить
# при показе нельзя: одно и то же сообщение выглядело бы по-разному в ленте, в
# заголовке инфоповода и в поиске.
entities = canonicalize_table(
    pd.DataFrame(
        [
            {
                "Дата": "24.04.2026",
                "Заголовок": "Нетворкинг &#8212; Строительная газета",
                "Текст": "Свежее поступление&#33; Цена &gt; 100 руб&nbsp;за штуку, "
                "марка &quot;Дёке&quot; &amp; Tegola",
                "Источник": "vk.com",
                "Url": "https://vk.com/wall?a=1&amp;b=2",
                "Hash сообщения": "abc",
                "ID сообщения": "1",
                "Тип источника": "Соцсети",
            }
        ]
    )
)
text = str(entities.loc[0, "Сообщение"])
check("восклицательный знак расшифрован", "поступление!" in text, text[:80])
check("угловая скобка расшифрована", "> 100" in text, text[:80])
check("кавычки расшифрованы", '"Дёке"' in text, text[:90])
check("амперсанд расшифрован", "& Tegola" in text, text[:90])
check(
    "неразрывный пробел стал обычным",
    "руб за штуку" in text and " " not in text,
    repr(text[:90]),
)
check(
    "тире в заголовке расшифровано",
    str(entities.loc[0, "Заголовок"]) == "Нетворкинг — Строительная газета",
    str(entities.loc[0, "Заголовок"]),
)
check(
    "ссылка тоже расшифрована",
    str(entities.loc[0, "Ссылка"]) == "https://vk.com/wall?a=1&b=2",
    str(entities.loc[0, "Ссылка"]),
)
# Одиночный амперсанд — не мнемоника, трогать его нельзя.
plain_amp = canonicalize_table(
    pd.DataFrame([{"Дата": "24.04.2026", "Сообщение": "Иванов & Партнёры, 5 & 7"}])
)
check(
    "одиночный амперсанд не пострадал",
    str(plain_amp.loc[0, "Сообщение"]) == "Иванов & Партнёры, 5 & 7",
    str(plain_amp.loc[0, "Сообщение"]),
)

print("15. Поля системы-источника доезжают до канона")
# Сверка csv и xlsx одной выгрузки показала, что мимо канона проходило девять
# колонок с данными. «Роль объекта» и «Язык» заполнены у всех строк: роль
# отвечает, о бренде ли сообщение или он упомянут вскользь.
fields = canonicalize_table(
    pd.DataFrame(
        [
            {
                "Дата": "24.04.2026",
                "Текст": "Сообщение",
                "Роль объекта": "Главная",
                "Язык": "Русский",
                "Тип автора": "Пользователь",
                "Пол": "Мужской",
                "Возраст": "35",
                "Место": "Руфлекс",
                "Адрес": "Россия, Московская область",
                "Цитируемость СМИ": "12",
                "Аудитория СМИ": "29792",
            }
        ]
    )
)
for column, expected in (
    ("Роль объекта", "Главная"),
    ("Язык", "Русский"),
    ("Тип автора", "Пользователь"),
    ("Пол", "Мужской"),
    ("Возраст", "35"),
    ("Место", "Руфлекс"),
    ("Адрес", "Россия, Московская область"),
    ("Цитируемость СМИ", "12"),
    ("Аудитория СМИ", "29792"),
):
    check(f"«{column}» доехало", str(fields.loc[0, column]) == expected,
          str(fields.loc[0].get(column)))
check(
    "все девять в каноническом контракте",
    {"Роль объекта", "Язык", "Тип автора", "Пол", "Возраст", "Место", "Адрес",
     "Цитируемость СМИ", "Аудитория СМИ"} <= set(CANONICAL_COLUMNS),
    str(sorted(set(CANONICAL_COLUMNS))[:5]),
)

print("16. Медиалогия: две аудитории вместо одной")
# Медиалогия не отдаёт «Аудиторию» одной колонкой — у неё их две, блога и
# автора. Без синонимов аудитория у любого проекта на Медиалогии оказывалась
# нулевой, а вместе с ней и ER, который делится на неё.
mlg = canonicalize_table(
    pd.DataFrame(
        [
            {
                "Время публикации": "24.08.2026 23:59",
                "Текст сообщения": "Сообщение",
                "Кто пишет": "Автор",
                "Где пишет": "Канал",
                "Аудитория блога": "109931",
                "Аудитория автора": "2122",
                "Тип блога": "Сообщество",
                "СМ Индекс": "72",
                "Семейный статус": "В браке",
                "Образование": "Высшее",
                "Статус на площадке": "Администратор",
                "Оценка от 1 до 5": "4",
            }
        ]
    )
)
check(
    "аудитория взята из блога, а не потеряна",
    str(mlg.loc[0, "Аудитория"]) == "109931",
    str(mlg.loc[0, "Аудитория"]),
)
check(
    "обе исходные аудитории сохранены раздельно",
    str(mlg.loc[0, "Аудитория блога"]) == "109931"
    and str(mlg.loc[0, "Аудитория автора"]) == "2122",
    f"{mlg.loc[0, 'Аудитория блога']!r}, {mlg.loc[0, 'Аудитория автора']!r}",
)
check("«Оценка от 1 до 5» — та же оценка", str(mlg.loc[0, "Оценка"]) == "4",
      str(mlg.loc[0, "Оценка"]))
for column, expected in (
    ("Тип блога", "Сообщество"),
    ("СМ Индекс", "72"),
    ("Семейный статус", "В браке"),
    ("Образование", "Высшее"),
    ("Статус на площадке", "Администратор"),
):
    check(f"«{column}» доехало", str(mlg.loc[0, column]) == expected, str(mlg.loc[0].get(column)))

# Когда аудитория блога пуста, работает авторская — иначе строка теряет вес.
fallback = canonicalize_table(
    pd.DataFrame([{"Дата": "24.08.2026", "Сообщение": "раз",
                   "Аудитория блога": "", "Аудитория автора": "500"}])
)
check(
    "пустая аудитория блога уступает авторской",
    str(fallback.loc[0, "Аудитория"]) == "500",
    str(fallback.loc[0, "Аудитория"]),
)

print("17. Одни и те же данные в csv и xlsx читаются одинаково")
# Одна выгрузка Медиалогии в двух форматах давала разный текст в 1208 строках и
# разные числа в 4170 ячейках при полностью совпадающих данных. Артефакты
# зеркальные: xlsx оставляет «_x000d_» вместо возврата каретки, csv пишет
# пробел в разрядах и настоящий «\r».
quirks = canonicalize_table(
    pd.DataFrame(
        [
            {
                "Дата": "24.08.2026",
                "Сообщение": "Тексты с изображений_x000d_\nвторая строка",
                "Аудитория": "2 786",
                "Просмотры": "12 045",
            },
            {
                "Дата": "24.08.2026",
                "Сообщение": "Тексты с изображений\r\nвторая строка",
                "Аудитория": "2786",
                "Просмотры": "12045",
            },
        ]
    )
)
check(
    "экранированный возврат каретки убран",
    "_x000d_" not in str(quirks.loc[0, "Сообщение"]),
    str(quirks.loc[0, "Сообщение"]),
)
check(
    "перевод строки в обоих форматах одинаков",
    str(quirks.loc[0, "Сообщение"]) == str(quirks.loc[1, "Сообщение"]),
    f"{quirks.loc[0, 'Сообщение']!r} против {quirks.loc[1, 'Сообщение']!r}",
)
check(
    "неразрывный пробел в разрядах убран",
    str(quirks.loc[0, "Аудитория"]) == "2786",
    str(quirks.loc[0, "Аудитория"]),
)
check(
    "обычный пробел в разрядах убран",
    str(quirks.loc[0, "Просмотры"]) == "12045",
    str(quirks.loc[0, "Просмотры"]),
)
check(
    "числа в двух форматах совпали",
    str(quirks.loc[0, "Аудитория"]) == str(quirks.loc[1, "Аудитория"])
    and str(quirks.loc[0, "Просмотры"]) == str(quirks.loc[1, "Просмотры"]),
    "числа разошлись",
)

# Пробел склеивается только внутри числа: в тексте это была бы порча данных.
prose = canonicalize_table(
    pd.DataFrame([{"Дата": "24.08.2026", "Сообщение": "Встреча в 5 7 часов, дом 12 345"}])
)
check(
    "пробелы в тексте не тронуты",
    str(prose.loc[0, "Сообщение"]) == "Встреча в 5 7 часов, дом 12 345",
    str(prose.loc[0, "Сообщение"]),
)
# «2 786 рублей» — не число целиком, и трогать его нельзя.
mixed = canonicalize_table(
    pd.DataFrame([{"Дата": "24.08.2026", "Сообщение": "Цена 2 786 рублей"}])
)
check(
    "число внутри фразы не склеено",
    str(mixed.loc[0, "Сообщение"]) == "Цена 2 786 рублей",
    str(mixed.loc[0, "Сообщение"]),
)

print("Оценка и товар: пустая колонка не заслоняет заполненную")
# Реальный случай: в файле есть и «Оценка», и «Рейтинг», оценка пустая насквозь,
# рейтинг заполнен. Раньше побеждало имя, а не данные, и раздел отзывов
# показывал прочерк вместо средней.
both = canonicalize_table(
    pd.DataFrame(
        [
            {"Дата": "24.08.2026", "Сообщение": "Отзыв", "Оценка": "", "Рейтинг": "5"},
            {"Дата": "24.08.2026", "Сообщение": "Отзыв", "Оценка": "", "Рейтинг": "4"},
        ]
    )
)
check(
    "пустая «Оценка» не заслоняет заполненный «Рейтинг»",
    both["Оценка"].tolist() == ["5", "4"],
    str(both["Оценка"].tolist()),
)
# Обратное тоже должно работать: заполненная «Оценка» главнее, построчной
# склейки с «Рейтингом» быть не должно.
own = canonicalize_table(
    pd.DataFrame(
        [
            {"Дата": "24.08.2026", "Сообщение": "Отзыв", "Оценка": "5", "Рейтинг": "1"},
            {"Дата": "24.08.2026", "Сообщение": "Пост", "Оценка": "", "Рейтинг": "1"},
        ]
    )
)
check(
    "заполненная «Оценка» главнее, значения из «Рейтинга» не подмешиваются",
    own["Оценка"].tolist() == ["5", ""],
    str(own["Оценка"].tolist()),
)
product = canonicalize_table(
    pd.DataFrame(
        [{"Дата": "24.08.2026", "Сообщение": "Отзыв", "Название товара": "Гвозди"}]
    )
)
check(
    "«Название товара» доезжает до канона",
    str(product.loc[0, "Товар"]) == "Гвозди",
    str(product.get("Товар")),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки разбора выгрузок пройдены.")
