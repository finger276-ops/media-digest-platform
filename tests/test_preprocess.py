"""Проверка обработки выгрузки: микротемы, заголовки, чистка текста, сборка таблиц.

preprocess.py — самый большой модуль платформы и до сих пор не имел тестов,
хотя через него проходит каждая загрузка. Здесь зафиксированы правила, от
которых зависит, что аналитик увидит в инфоповодах.

Важно: build_processed_tables по умолчанию пишет в data/processed и перед
этим удаляет оттуда прошлые файлы, поэтому в тестах всегда передаётся
временная папка.
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from import_adapters import canonicalize_table  # noqa: E402
from preprocess import (  # noqa: E402
    build_processed_tables,
    build_title,
    classify_microtopic,
    clean_text,
    make_events,
    normalize_label,
    split_tag_text,
)
from services.ingest import IngestError, process_canonical  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Классификация микротем")
check(
    "жалоба на брак → проблемы",
    classify_microtopic("Пришёл брак, монтажники жалуются на дефекты") == "issue_problem",
    classify_microtopic("Пришёл брак, монтажники жалуются на дефекты"),
)
check(
    "разговор о цене → цены",
    classify_microtopic("Сколько стоит, какая цена за упаковку") == "price_terms",
    classify_microtopic("Сколько стоит, какая цена за упаковку"),
)
check(
    "сертификаты → документы",
    classify_microtopic("Нужен сертификат и документы по ГОСТ") == "documents_certificates",
    classify_microtopic("Нужен сертификат и документы по ГОСТ"),
)
check(
    "правила проверяются по порядку: жалоба важнее цены",
    classify_microtopic("Жалоба: брак, да ещё и цена задрана") == "issue_problem",
    classify_microtopic("Жалоба: брак, да ещё и цена задрана"),
)
check(
    "без совпадений и без тегов → general",
    classify_microtopic("Погода сегодня хорошая") == "general",
    classify_microtopic("Погода сегодня хорошая"),
)
tagged_topic = classify_microtopic("Погода сегодня хорошая", tags="Логистика")
check(
    "без совпадений, но с тегом → метка по тегу",
    tagged_topic.startswith("label_"),
    tagged_topic,
)

print("2. Построение заголовка инфоповода")
check(
    "сюжет источника перебивает всё остальное",
    build_title(
        tag="Цены",
        keywords=["цена", "скидка"],
        microtopic="price_terms",
        source_main_topic="Запуск завода в Рязани",
    )
    == "Запуск завода в Рязани",
)
check(
    "без сюжета берётся понятный тег",
    build_title(tag="Маркетплейсы", keywords=["отзыв"], microtopic="other")
    == "Маркетплейсы",
    build_title(tag="Маркетплейсы", keywords=["отзыв"], microtopic="other"),
)
fallback_title = build_title(tag="Без тега", keywords=["доставка", "склад", "сроки"], microtopic="other")
check(
    "без тега и микротемы — заголовок из ключевых слов",
    fallback_title.startswith("Обсуждение:"),
    fallback_title,
)
check(
    "совсем пусто — «Прочие обсуждения»",
    build_title(tag="Без тега", keywords=[], microtopic="other") == "Прочие обсуждения",
    build_title(tag="Без тега", keywords=[], microtopic="other"),
)

print("3. Чистка текста сообщения")
text, source = clean_text("  Привет\r\nмир  ", "")
check("перевод строки нормализован", text == "Привет\nмир", repr(text))
check("источник текста — само сообщение", source == "message", source)
ocr_text, ocr_source = clean_text("", "Текст с картинки")
check("пустое сообщение подменяется распознанным текстом", ocr_text == "Текст с картинки", repr(ocr_text))
check("источник помечен как распознанный", ocr_source != "message", ocr_source)

print("4. Разбор тегов и меток")
check(
    "теги режутся по разделителям",
    split_tag_text("Цены|Качество; Логистика") == ["Цены", "Качество", "Логистика"],
    str(split_tag_text("Цены|Качество; Логистика")),
)
# Разделение и отсев служебных значений — разные слои: split_tag_text режет
# как есть, а мусор отсеивается уже нормализацией метки.
check("split_tag_text не фильтрует сам по себе", split_tag_text("Цены|нет") == ["Цены", "нет"])
check("служебное «нет» отсеивается нормализацией", normalize_label("нет") == "")
check("«Без тега» тоже считается пустой меткой", normalize_label("Без тега") == "")
long_label = normalize_label("я" * 200)
check("слишком длинная метка обрезается", len(long_label) <= 90, str(len(long_label)))

print("5. Пустая выгрузка не роняет сборку инфоповодов")
# Раньше здесь был KeyError: сортировка пустого DataFrame по несуществующим
# колонкам. Клиент, загрузивший файл без разбираемых строк, получал падение.
try:
    empty_events, empty_links = make_events(
        pd.DataFrame(columns=["discussion_id", "main_tags", "discussion_text"]),
        pd.Series(dtype=int),
    )
    check("вернулись пустые таблицы, а не исключение", empty_events.empty and empty_links.empty)
except Exception as exc:  # noqa: BLE001
    check("вернулись пустые таблицы, а не исключение", False, f"{type(exc).__name__}: {exc}")

print("6. Сквозная сборка таблиц из выгрузки Brand Analytics")
rows = []
topics = ["Рост цен на утеплитель", "Новый завод", "Отзывы о монтаже"]
for i in range(12):
    rows.append(
        {
            "ID сообщения": f"m{i}",
            "Hash сообщения": f"h{i}",
            "Дата": f"{24 + i % 5:02d}.04.2026",
            "Время": f"{9 + i % 8:02d}:00",
            "Сообщение": f"{topics[i % 3]}. Комментарий {i} про цену и качество материала.",
            "Автор": f"user{i % 4}",
            "Url": f"https://vk.com/p/{i}",
            "Источник": "vk.com",
            "Тип источника": "Соцсети",
            "Тональность": ["позитив", "нейтрал", "негатив"][i % 3],
            "Аудитория": "1 500",
            "Просмотры": "300",
            "Вовлеченность": "12",
            "Сюжет": topics[i % 3],
            "Обработано": "да",
            "ТЕХНОНИКОЛЬ": "ТЕХНОНИКОЛЬ" if i % 2 == 0 else "",
        }
    )
canonical = canonicalize_table(pd.DataFrame(rows), source_file="ba.xlsx")

with TemporaryDirectory() as tmp:
    manifest = build_processed_tables(canonical, output=tmp, source_file="ba.xlsx")
    check("манифест получен", isinstance(manifest, dict), str(type(manifest)))
    check(
        "инфоповоды собраны по сюжетам источника, без кластеризации",
        manifest.get("cluster_method") == "brand_analytics_story",
        str(manifest.get("cluster_method")),
    )
    check(
        "сообщения обработаны",
        int(manifest.get("rows_messages") or 0) == len(rows),
        str(manifest.get("rows_messages")),
    )
    check(
        "инфоповодов столько же, сколько уникальных сюжетов",
        int(manifest.get("rows_events") or 0) == len(set(topics)),
        str(manifest.get("rows_events")),
    )
    check(
        "источник инфоповодов помечен как сюжеты BA",
        manifest.get("event_source") == "brand_analytics_story",
        str(manifest.get("event_source")),
    )

print("7. Выгрузка без единой строки отклоняется понятным сообщением")
# Обработка пустой выгрузки намеренно не поддерживается: она отсекается на
# входе в process_canonical, до обращения к Supabase и до препроцессинга.
empty_canonical = canonicalize_table(pd.DataFrame([{"Дата": "", "Сообщение": ""}]))
check("пустые строки отброшены при канонизации", empty_canonical.empty, str(len(empty_canonical)))
try:
    process_canonical(empty_canonical, project_id="tn_project", source_filename="empty.xlsx")
    check("пустая выгрузка отклонена", False, "исключения не было")
except IngestError as exc:
    check("пустая выгрузка отклонена", True)
    check("сообщение объясняет причину", "не найдено" in str(exc).lower(), str(exc))
except Exception as exc:  # noqa: BLE001
    check("пустая выгрузка отклонена ожидаемой ошибкой", False, f"{type(exc).__name__}: {exc}")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки обработки выгрузки пройдены.")
