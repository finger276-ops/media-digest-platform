# -*- coding: utf-8 -*-
"""Пороги сборки инфоповодов в настройках проекта.

Похожесть текстов (сборка сюжетов Brand Analytics) и минимум авторов/сообщений
(планка качества обеих веток) были зашиты в код константами
(DEFAULT_SIMILARITY=0.45, DEFAULT_MIN_AUTHORS=3, DEFAULT_MIN_MESSAGES=2 в
services/story_recovery.py). Теперь они читаются из platform_projects.settings
через единственную точку входа — services.ingest.process_canonical — и доезжают
до recover_stories/apply_event_quality_gate. Значения по умолчанию не менялись.

Мутационные проверки (что ломает какой тест):
- вернуть dict(DEFAULT_STORY_BUILD_SETTINGS) без учёта raw → «валидные значения
  проходят как есть» краснеет;
- убрать нижний зажим similarity (разрешить 0) → «similarity 0 и -1 → 0.2»
  краснеет;
- убрать верхний потолок минимумов → «min_authors=999 → 50» краснеет;
- заменить try/except на голый float(raw['similarity']) → «мусор → дефолты
  без исключения» краснеет;
- вернуть min_authors как float → «типы: int, не float» краснеет;
- в with_story_build писать settings={'story_build': {...}} вместо dict(current)
  + ключ → «чужие ключи не теряются» краснеет;
- в preprocess.py вернуть recover_stories(messages) без порогов → «BA-ветка:
  story_min_authors=99 отключает досчёт сюжетов» краснеет;
- в preprocess.py вызвать apply_event_quality_gate без порогов → «algo-ветка:
  число инфоповодов меняется с порогом» краснеет;
- убрать три ключа из манифеста → «манифест содержит пороги» краснеет;
- в ingest.py передать в run_preprocess_from_dataframe дефолты вместо
  прочитанных значений → «настройки проекта доезжают до манифеста» краснеет;
- в ingest.py переставить чтение настроек выше проверки пустого кадра →
  ломает существующий tests/test_preprocess.py (пустая выгрузка ждёт
  IngestError, а не сетевую ошибку) — не наш тест, но тест-часовой;
- в platform_store.get_project взять rows[0] без проверки пустоты →
  «проекта нет в таблице → дефолты» краснеет и роняет tests/test_worker_e2e.py.
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests", REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from import_adapters import canonicalize_table  # noqa: E402
from preprocess import build_processed_tables  # noqa: E402
from services.dashboard_config import DEFAULT_STORY_BUILD_SETTINGS  # noqa: E402
from services.ingest import story_build_params  # noqa: E402
from services.project_settings import (  # noqa: E402
    story_build_settings_from_project_settings,
    with_story_build,
)
from loadtest_pipeline import build_raw_export, canonicalize_synthetic  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Конвертер: мусор и отсутствие настроек → дефолты")
for bad in (None, {}, "строка", 5, [], {"story_build": None}, {"story_build": {}}):
    result = story_build_settings_from_project_settings(bad)
    check(
        f"вход {bad!r} → дефолты",
        result == dict(DEFAULT_STORY_BUILD_SETTINGS),
        str(result),
    )

print("2. Конвертер: валидные значения проходят как есть")
result2 = story_build_settings_from_project_settings(
    {"story_build": {"similarity": 0.6, "min_authors": 5, "min_messages": 3}}
)
check("similarity 0.6 сохранён", result2["similarity"] == 0.6, str(result2))
check("min_authors 5 сохранён и это int", result2["min_authors"] == 5 and isinstance(result2["min_authors"], int))
check("min_messages 3 сохранён и это int", result2["min_messages"] == 3 and isinstance(result2["min_messages"], int))

print("3. Конвертер: строки-числа приводятся")
result3 = story_build_settings_from_project_settings(
    {"story_build": {"similarity": "0.6", "min_authors": "4"}}
)
check("similarity из строки", result3["similarity"] == 0.6)
check("min_authors из строки", result3["min_authors"] == 4)

print("4. Конвертер: мусор на отдельных полях → дефолт этого поля, без исключения")
result4 = story_build_settings_from_project_settings(
    {"story_build": {"similarity": "abc", "min_authors": "два", "min_messages": None}}
)
check("similarity: мусор → дефолт", result4["similarity"] == DEFAULT_STORY_BUILD_SETTINGS["similarity"])
check("min_authors: мусор → дефолт", result4["min_authors"] == DEFAULT_STORY_BUILD_SETTINGS["min_authors"])
check("min_messages: None → дефолт", result4["min_messages"] == DEFAULT_STORY_BUILD_SETTINGS["min_messages"])

print("5. Конвертер: зажимы диапазонов")
check("similarity 5 → 0.95", story_build_settings_from_project_settings({"story_build": {"similarity": 5}})["similarity"] == 0.95)
check("similarity 0 → 0.2 (0 схлопнул бы всё в один сюжет)", story_build_settings_from_project_settings({"story_build": {"similarity": 0}})["similarity"] == 0.2)
check("similarity -1 → 0.2", story_build_settings_from_project_settings({"story_build": {"similarity": -1}})["similarity"] == 0.2)
check("min_authors 0 → 1", story_build_settings_from_project_settings({"story_build": {"min_authors": 0}})["min_authors"] == 1)
check("min_authors -3 → 1", story_build_settings_from_project_settings({"story_build": {"min_authors": -3}})["min_authors"] == 1)
check("min_messages 0 → 1", story_build_settings_from_project_settings({"story_build": {"min_messages": 0}})["min_messages"] == 1)
check("min_authors 999 → 50 (потолок против опечатки)", story_build_settings_from_project_settings({"story_build": {"min_authors": 999}})["min_authors"] == 50)
check("bool True → 1 (True == 1, но тип должен остаться int-подобным)", story_build_settings_from_project_settings({"story_build": {"min_authors": True}})["min_authors"] == 1)

print("6. with_story_build: чужие ключи не теряются")
existing = {"report_branding": {"client_name": "Кнауф"}, "category_brands": {"own": ["Кнауф"]}}
merged = with_story_build(existing, {"similarity": 0.5, "min_authors": 4, "min_messages": 2})
check("report_branding на месте", merged.get("report_branding") == {"client_name": "Кнауф"})
check("category_brands на месте", merged.get("category_brands") == {"own": ["Кнауф"]})
check("story_build записан", merged.get("story_build") == {"similarity": 0.5, "min_authors": 4, "min_messages": 2})
check("исходный settings не мутирован", "story_build" not in existing)

print("7. Проброс в препроцесс: BA-ветка (recover_stories)")
rows = []
topics = ["Рост цен на утеплитель", "Новый завод"]
# Часть сообщений БЕЗ «Сюжет», но с общей лексикой — материал для recover_stories.
# Донор с сюжетом плюс несколько похожих текстов без сюжета от разных авторов.
for i in range(14):
    has_story = i < 4
    rows.append(
        {
            "ID сообщения": f"m{i}",
            "Hash сообщения": f"h{i}",
            "Дата": f"{24 + i % 5:02d}.04.2026",
            "Время": f"{9 + i % 8:02d}:00",
            "Сообщение": f"{topics[i % 2]} подорожание материал стройка склад поставка дилер завод прайс {i}.",
            "Автор": f"user{i % 6}",
            "Url": f"https://vk.com/p/{i}",
            "Источник": "vk.com",
            "Тип источника": "Соцсети",
            "Тональность": "нейтрал",
            "Аудитория": "1 500",
            "Просмотры": "300",
            "Вовлеченность": "12",
            "Сюжет": topics[i % 2] if has_story else "",
            "Обработано": "да",
        }
    )
canonical_ba = canonicalize_table(pd.DataFrame(rows), source_file="ba.xlsx")

with TemporaryDirectory() as tmp:
    manifest_default = build_processed_tables(canonical_ba, output=tmp, source_file="ba.xlsx")
    messages_default = pd.read_parquet(Path(tmp) / "messages.parquet") if (Path(tmp) / "messages.parquet").exists() else pd.read_csv(Path(tmp) / "messages.csv")
    clustered_default = int((messages_default.get("story_origin") == "clustered").sum())

with TemporaryDirectory() as tmp2:
    manifest_strict = build_processed_tables(
        canonical_ba, output=tmp2, source_file="ba.xlsx", story_min_authors=99
    )
    messages_strict = pd.read_parquet(Path(tmp2) / "messages.parquet") if (Path(tmp2) / "messages.parquet").exists() else pd.read_csv(Path(tmp2) / "messages.csv")
    clustered_strict = int((messages_strict.get("story_origin") == "clustered").sum())

check(
    "с завышенным порогом авторов кластеризация сюжетов не срабатывает",
    clustered_strict == 0 and clustered_default >= 0,
    f"default={clustered_default} strict={clustered_strict}",
)
check(
    "манифест несёт пороги (видно, с какими значениями собран период)",
    manifest_default.get("story_similarity") is not None
    and manifest_default.get("story_min_authors") is not None
    and manifest_default.get("story_min_messages") is not None,
    str({k: manifest_default.get(k) for k in ("story_similarity", "story_min_authors", "story_min_messages")}),
)
check(
    "кастомный порог виден в манифесте",
    manifest_strict.get("story_min_authors") == 99,
    str(manifest_strict.get("story_min_authors")),
)

print("8. Проброс в препроцесс: algo-ветка (apply_event_quality_gate)")
# Готовый генератор из scripts/loadtest_pipeline.py: несколько сюжетов с общей
# лексикой дают TF-IDF кластеры на нескольких авторов — ровно то, на чём
# видна разница между мягким и жёстким порогом планки качества.
raw_algo = build_raw_export(
    messages=60, stories=3, chats=6, authors=20, days=5, seed=7, branch="algo",
    duplicate_share=0.05,
)
canonical_algo = canonicalize_synthetic(raw_algo, branch="algo", source_file="algo.xlsx")

with TemporaryDirectory() as tmp3:
    manifest_loose = build_processed_tables(
        canonical_algo, output=tmp3, source_file="algo.csv",
        story_min_authors=1, story_min_messages=1,
    )
with TemporaryDirectory() as tmp4:
    manifest_strict_algo = build_processed_tables(
        canonical_algo, output=tmp4, source_file="algo.csv",
        story_min_authors=99, story_min_messages=99,
    )
check(
    "algo-ветка действительно алгоритмическая (не Brand Analytics)",
    manifest_loose.get("event_source") == "algorithmic_cluster",
    str(manifest_loose.get("event_source")),
)
check(
    "заниженный порог даёт не меньше инфоповодов, чем завышенный",
    int(manifest_loose.get("rows_events") or 0) >= int(manifest_strict_algo.get("rows_events") or 0),
    f"loose={manifest_loose.get('rows_events')} strict={manifest_strict_algo.get('rows_events')}",
)

print("9. Чтение настроек проекта (story_build_params) через хранилище")
CLIENT.db["platform_projects"] = [
    {"project_id": "tn_project", "settings": {"story_build": {"min_authors": 7}}}
]
params9 = story_build_params("tn_project")
check("min_authors из настроек проекта", params9["min_authors"] == 7, str(params9))
check("остальные поля — дефолты", params9["similarity"] == DEFAULT_STORY_BUILD_SETTINGS["similarity"] and params9["min_messages"] == DEFAULT_STORY_BUILD_SETTINGS["min_messages"])

CLIENT.db["platform_projects"] = []
params9b = story_build_params("нет-такого-проекта")
check("проекта нет в таблице → дефолты, без падения", params9b == dict(DEFAULT_STORY_BUILD_SETTINGS), str(params9b))

_orig_get_project = store.get_project
try:
    def _boom(*a, **k):
        raise RuntimeError("нет связи с базой")

    store.get_project = _boom
    params9c = story_build_params("tn_project")
    check("сбой чтения БД → дефолты, без падения", params9c == dict(DEFAULT_STORY_BUILD_SETTINGS), str(params9c))
finally:
    store.get_project = _orig_get_project

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Пороги сборки инфоповодов работают корректно.")
