"""Проверка инструмента нагрузочного прогона (scripts/loadtest_pipeline.py).

Сам прогон на 50 000 сообщений в CI не идёт — timeout-minutes: 15 в
.github/workflows/tests.yml он не переживёт, да и цель не в этом. Здесь
проверяется, что ГЕНЕРАТОР и ИЗМЕРИТЕЛЬ не врут: детерминированы, дают
представительную выгрузку (обе ветки конвейера, все нужные типы сообщений,
и позитив, и негатив), и что инструментированный прогон не разошёлся с
боевым порядком шагов build_processed_tables.

Мутационные проверки (что ломает какой тест):
- заменить rng на глобальный np.random в build_raw_export → «детерминированность»
  краснеет (два вызова с одним сидом дадут разные кадры);
- убрать колонку «Обработано» из ветки ba → «is_brand_analytics_dataframe»
  краснеет (детектор увидит алгоритмическую ветку вместо BA);
- сделать «Id сообщения» константой для всех строк → «строки не размножились
  через merge» краснеет (совпадающий message_id склеит discussion_messages);
- переставить местами шаги в run_instrumented (например refine_labels_by_tag
  и split_labels_by_time_gap) → «инструментированный прогон совпадает со
  штатным» краснеет;
- убрать meter.step().__enter__ вызов tracemalloc.reset_peak() → «пики шагов
  не все совпадают» краснеет (без сброса каждый шаг показывал бы глобальный
  максимум — то есть одно и то же число);
- выставить всем строкам «Тип площадки» = «Отзывы» → «есть все нужные виды
  сообщений» краснеет (post/comment/repost исчезнут).
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from import_adapters import CANONICAL_COLUMNS, canonicalize_table  # noqa: E402
from preprocess import detect_tag_columns, is_brand_analytics_dataframe  # noqa: E402
from services.message_kinds import classify_kinds  # noqa: E402

from loadtest_pipeline import (  # noqa: E402
    Meter,
    build_raw_export,
    canonicalize_synthetic,
    run_instrumented,
    verify_against_reference,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


N = 300

print("1. Детерминированность генератора")
raw_a = build_raw_export(messages=N, stories=15, chats=40, authors=90, days=5, seed=42, branch="ba")
raw_b = build_raw_export(messages=N, stories=15, chats=40, authors=90, days=5, seed=42, branch="ba")
raw_c = build_raw_export(messages=N, stories=15, chats=40, authors=90, days=5, seed=43, branch="ba")
check("один сид — побайтово одинаковые кадры", raw_a.equals(raw_b))
check("другой сид — другой кадр", not raw_a.equals(raw_c))

print("2. Каноническая форма (ветка Brand Analytics)")
canonical = canonicalize_table(raw_a, source_file="loadtest_test.xlsx")
check("все канонические колонки на месте", set(CANONICAL_COLUMNS).issubset(set(canonical.columns)))
non_string = [c for c in CANONICAL_COLUMNS if c in canonical.columns and not canonical[c].map(lambda v: isinstance(v, str)).all()]
check("все значения строковые (как в проде)", not non_string, str(non_string[:5]))
check("непустых строк столько же, сколько сгенерировано", len(canonical) == N, str(len(canonical)))

print("3. Обнаружение колонок-тегов совпадает со сгенерированными")
detected = set(detect_tag_columns(canonical))
generated_tags = {f"Тег{i + 1}" for i in range(6)}
check("ровно сгенерированные теги, без лишних", detected == generated_tags, str(detected))

print("4. Определение ветки конвейера")
# bool(...), не "is True"/"is False": .any() на pandas-серии отдаёт
# numpy.bool_, а numpy.bool_(False) is False — False по идентичности объекта,
# хотя по значению они равны.
check("ba-выгрузка распознана как Brand Analytics", bool(is_brand_analytics_dataframe(canonical)))
raw_algo = build_raw_export(messages=N, stories=15, chats=40, authors=90, days=5, seed=42, branch="algo")
canonical_algo = canonicalize_synthetic(raw_algo, branch="algo", source_file="loadtest_algo.xlsx")
check(
    "algo-выгрузка НЕ распознана как Brand Analytics",
    not bool(is_brand_analytics_dataframe(canonical_algo)),
)

print("5. Инструментированный прогон совпадает со штатным (обе ветки)")
with TemporaryDirectory() as tmp:
    meter = Meter(trace=True)
    manifest = run_instrumented(canonical, Path(tmp), meter)
    problems = verify_against_reference(canonical, manifest)
    check("ветка BA: манифест совпадает со штатным прогоном", not problems, "; ".join(problems))
    check("посчитаны кандидаты в восстановление сюжетов", int(manifest.get("story_candidates", 0)) > 0)

with TemporaryDirectory() as tmp:
    meter_algo = Meter(trace=False)
    manifest_algo = run_instrumented(canonical_algo, Path(tmp), meter_algo)
    problems_algo = verify_against_reference(canonical_algo, manifest_algo)
    check("ветка algo: манифест совпадает со штатным прогоном", not problems_algo, "; ".join(problems_algo))
    check(
        "ветка algo действительно алгоритмическая",
        manifest_algo.get("event_source") == "algorithmic_cluster",
        str(manifest_algo.get("event_source")),
    )

print("6. Пики шагов не все совпадают (доказательство, что reset_peak реально вызывается)")
peaks = [s.peak_mb for s in meter.steps if s.peak_mb > 0]
check(
    "хотя бы два разных значения пика среди шагов",
    len(set(round(p, 3) for p in peaks)) > 1,
    str(peaks),
)

print("7. Представительность синтетики: виды сообщений и тональность")
from preprocess import normalize_messages  # noqa: E402

tag_cols = detect_tag_columns(canonical)
messages, _ = normalize_messages(canonical, tag_cols)
kinds = set(classify_kinds(messages))
check(
    "присутствуют публикации, репосты и комментарии",
    {"post", "repost", "comment"}.issubset(kinds),
    str(kinds),
)
check(
    "присутствуют отзывы (тип площадки «Отзывы»)",
    "review" in kinds,
    str(kinds),
)
is_negative = messages.get("is_negative")
check(
    "есть и негативные, и не-негативные сообщения",
    is_negative is not None and bool(is_negative.any()) and not bool(is_negative.all()),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Инструмент нагрузочного прогона работает корректно.")
