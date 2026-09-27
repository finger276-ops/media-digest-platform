# -*- coding: utf-8 -*-
"""Теги для показа — только из выгрузки (задачи 106 и 108).

На выгрузках не из Brand Analytics платформа дописывает к тегам сообщения
свои рубрики: «Проблемы, жалобы и негативный опыт», «Монтаж, применение и
эксплуатация», «Конкуренты и сравнение на рынке», «Прочие обсуждения».
Рубрики писались под заказчика из стройматериалов, и проект такси видел у
себя «Экология и энергоэффективность» — в «Тегах», «Обзоре», карточках
сообщений и отчёте. Вдобавок теги резались по запятой: клиент видел обрывки
«Проблемы» и «жалобы и негативный опыт».

Рубрики остаются в данных — на них опирается сборка сюжетов, — а при чтении
(clean_display_tags в prepare_period_data) отсекаются. Поэтому уже
загруженные периоды чистятся без перезагрузки.

Мутационные проверки (что ломает какой тест):
- в clean_display_tags снова ничего не делать для не-Brand Analytics ->
  «Медиалогия: рубрики платформы ушли» краснеет;
- вернуть запятую в split_pipe_values -> «запятая — часть названия»
  краснеет;
- проверять рубрики раньше объявленных тегов -> «объявленный выгрузкой тег
  остаётся, даже если совпал с рубрикой» краснеет;
- скрывать автотеги такси на любых выгрузках -> «на Медиалогии свои теги
  такси остаются» краснеет;
- не скрывать автотеги такси на Brand Analytics без объявленных колонок ->
  «старые автотеги такси на Brand Analytics скрыты» краснеет;
- не пересчитывать tag_count -> «число тегов пересчитано» краснеет;
- в infer_display_tags поменять рубрики -> «при загрузке рубрики на месте»
  краснеет (сборка сюжетов на них опирается, загрузку не трогаем).
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.tag_compute import (  # noqa: E402
    build_tag_statistics_compute,
    clean_display_tags,
    split_pipe_values,
)
from services.tag_parsing import infer_display_tags  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Разделитель тегов — «|», запятая — часть названия")
check(
    "запятая — часть названия",
    split_pipe_values("Проблемы, жалобы|Тарифы") == ["Проблемы, жалобы", "Тарифы"],
    str(split_pipe_values("Проблемы, жалобы|Тарифы")),
)
check("точка с запятой по-прежнему разделяет старые записи", split_pipe_values("А;Б") == ["А", "Б"])
check("повторы и пробелы схлопываются", split_pipe_values(" Тарифы | тарифы |") == ["Тарифы"])

print("2. Медиалогия: теги выгрузки остаются, рубрики платформы уходят")
medialogia = pd.DataFrame(
    {
        "source_system": ["mediologia_excel"] * 4,
        "source_tag_columns": [""] * 4,
        "tags": [
            "Тарифы|Проблемы, жалобы и негативный опыт|Цены, стоимость и условия",
            "Водители|Экология и энергоэффективность",
            "Прочие обсуждения",
            "Коэффициент|Яндекс Про|Общие обсуждения",
        ],
        "tag_count": [3, 2, 1, 3],
    }
)
cleaned = clean_display_tags(medialogia)
check(
    "Медиалогия: рубрики платформы ушли",
    cleaned["tags"].tolist() == ["Тарифы", "Водители", "", "Коэффициент|Яндекс Про"],
    str(cleaned["tags"].tolist()),
)
check(
    "на Медиалогии свои теги такси остаются",
    "Коэффициент" in cleaned["tags"].iloc[3],
    str(cleaned["tags"].iloc[3]),
)
check("число тегов пересчитано", cleaned["tag_count"].tolist() == [1, 1, 0, 2], str(cleaned["tag_count"].tolist()))
check("исходный кадр не тронут", medialogia["tags"].iloc[0].count("|") == 2)
stats = build_tag_statistics_compute(cleaned.assign(text=["a", "b", "c", "d"]))
check(
    "в статистике тегов нет ни рубрик, ни обрывков по запятой",
    set(stats["Тег"]) == {"Тарифы", "Водители", "Коэффициент", "Яндекс Про"},
    str(sorted(stats["Тег"])),
)

print("3. Brand Analytics: показываются объявленные выгрузкой теги")
brand_analytics = pd.DataFrame(
    {
        "source_system": ["brand_analytics"] * 3,
        "source_tag_columns": ["ROCKWOOL|Проблемы, жалобы и негативный опыт|Аэропорты"] * 3,
        "tags": [
            "ROCKWOOL|Проблемы, жалобы и негативный опыт",
            "Аэропорты|Без тега",
            "детские кресла|Старая метка",
        ],
        "tag_count": [2, 2, 2],
    }
)
cleaned_ba = clean_display_tags(brand_analytics)
check(
    "объявленный выгрузкой тег остаётся, даже если совпал с рубрикой",
    cleaned_ba["tags"].iloc[0] == "ROCKWOOL|Проблемы, жалобы и негативный опыт",
    cleaned_ba["tags"].iloc[0],
)
check(
    "объявленный тег из списка такси у другого заказчика не прячется",
    cleaned_ba["tags"].iloc[1] == "Аэропорты",
    cleaned_ba["tags"].iloc[1],
)
check(
    "необъявленные метки на Brand Analytics скрыты",
    cleaned_ba["tags"].iloc[2] == "",
    cleaned_ba["tags"].iloc[2],
)
legacy = pd.DataFrame(
    {
        "source_system": ["brand_analytics"],
        "tags": ["яндекс|Настоящий тег|Без тега"],
    }
)
check(
    "старые автотеги такси на Brand Analytics скрыты",
    clean_display_tags(legacy)["tags"].iloc[0] == "Настоящий тег",
    clean_display_tags(legacy)["tags"].iloc[0],
)

print("4. Загрузка не меняется: рубрики пишутся, сюжеты на них опираются")
check(
    "при загрузке рубрики на месте",
    infer_display_tags("", "price_terms", ["Тарифы"]) == ["Тарифы", "Цены, стоимость и условия"]
    and infer_display_tags("", "other", []) == ["Прочие обсуждения"],
    str(infer_display_tags("", "price_terms", ["Тарифы"])),
)

print("5. Пустые и неполные кадры не падают")
check("пустой кадр", clean_display_tags(pd.DataFrame()).empty)
check("без колонки tags", list(clean_display_tags(pd.DataFrame({"x": [1]})).columns) == ["x"])
check("NaN в тегах", clean_display_tags(pd.DataFrame({"tags": [None, "Тарифы"]}))["tags"].tolist() == ["", "Тарифы"])

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Теги для показа берутся только из выгрузки.")
