# -*- coding: utf-8 -*-
"""Сборка обсуждений: быстрее, но результат тот же до байта.

make_discussions был самым дорогим шагом конвейера: построчный iterrows по
всем сообщениям и десяток операций pandas на каждое из тысяч обсуждений —
50 секунд на 20 000 сообщений. Новая реализация сортирует сообщения один
раз и считает группы по спискам Python. Цель — только скорость, поэтому тест
сверяет её с прежней реализацией (tests/discussion_build_reference.py) на
случайных данных — с пропусками, дублями и неполными колонками, на каких
прежний код вёл себя по-своему:

- пустая дата: пара соседних сообщений никогда не разрывает ветку;
- пустой chat_id у сообщения без родителя: в обсуждения оно не попадает;
- пустой text_clean в представительных сообщениях превращался в «nan»;
- microtopic NaN — это «nan», а не «other»;
- несколько одинаковых message_id размножаются при слиянии;
- одинаковое время — порядок сообщений внутри обсуждения устойчивый.

Всё это сохранено: тест требует совпадения таблиц целиком, с типами
колонок. Одно отличие намеренное: когда не складывается ни одного
обсуждения (пустой вход или, например, одно сообщение без родителя и без
чата), прежний код падал KeyError'ом, новый возвращает пустые таблицы с
нужными колонками.

Мутационные проверки (что ломает какой тест):
- порог 12 минут для смены микротемы заменить на 8 -> «совпадает с
  прежней на случайных данных» краснеет;
- убрать проверку пустой даты (both_dated) -> то же;
- брать представительные сообщения из текстов с fillna (без «nan») -> то же;
- сортировать обсуждения не устойчиво (sort_values только по sort_date) ->
  то же;
- кеш нормализации без проверки type(value) is str — поймать не может
  (в выгрузке эти колонки строковые), поэтому проверка в кеше описана в
  его докстринге, а не здесь;
- работать с входным кадром на месте, без копии -> «входная таблица не
  меняется» краснеет.
"""

import os
import random
import sys
import time
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from pandas.testing import assert_frame_equal  # noqa: E402

from discussion_build_reference import make_discussions_reference  # noqa: E402
from services.discussion_build import make_discussions  # noqa: E402

# Даты нарочно в разнобой, включая «не дату»: pandas предупреждает, что
# разбирает их по одной. Для теста это ожидаемо и только шумит.
warnings.filterwarnings("ignore", message="Could not infer format")

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


OPTIONAL = [
    "parent_text",
    "source_main_topic",
    "source_topics",
    "microtopic",
    "title",
    "chat_title",
    "author_id",
    "is_negative",
    "is_toxic",
]
COLUMNS = ["message_id", "parent_link", "datetime", "chat_id", "tags", "text_clean"] + OPTIONAL
BASE = pd.Timestamp("2026-09-01 10:00")


def messy_frame(rng: random.Random, n: int) -> pd.DataFrame:
    def pick(options):
        return options[rng.randrange(len(options))]

    rows = []
    for i in range(n):
        minutes = rng.choice([0, 1, 5, 9, 13, 30, 61, 200, 2000]) * rng.randint(0, 3)
        duplicate = rng.random() < 0.05
        rows.append(
            {
                "message_id": f"m{rng.randrange(max(n, 1)):04d}" if duplicate else f"m{i:04d}",
                "parent_link": pick(["", "", "", "https://p/1", " https://p/1 ", "https://p/2", None, np.nan]),
                "datetime": pick(
                    [str(BASE + pd.Timedelta(minutes=minutes * (i % 7))), None, "не дата", str(BASE)]
                ),
                "chat_id": pick(["c1", "c1", "c2", "c3", None, np.nan, ""]),
                "tags": pick(["", "a", "a|b", " b | c ", "c", np.nan, "|", "яндекс|a"]),
                "text_clean": pick(
                    ["Текст  про\xa0бренд", "", "   ", "другой\n\n\n\nтекст", np.nan, "x" * 400, "короткий"]
                ),
                "parent_text": pick(["", "Пост", " Пост ", np.nan, "Другой пост"]),
                "source_main_topic": pick(["", "Сюжет 1", " Сюжет  1 ", "Сюжет 2", np.nan]),
                "source_topics": pick(["", "A; B", "['A', 'C']", "nan", np.nan, "B|A"]),
                "microtopic": pick(["other", "цены", "", None, np.nan, "доставка"]),
                "title": pick(["", "Заголовок", " Заголовок ", "Другой", np.nan]),
                "chat_title": pick(["Чат 1", "", np.nan]),
                "author_id": pick(["u1", "u2", "u3", np.nan]),
                "is_negative": pick([True, False, False]),
                "is_toxic": pick([True, False, False, False]),
            }
        )
    frame = pd.DataFrame(rows, columns=COLUMNS)
    for col in OPTIONAL:
        if rng.random() < 0.15:
            frame = frame.drop(columns=col)
    return frame


def same_result(frame: pd.DataFrame, window: int) -> str:
    """Пустая строка — совпало; иначе — в чём расхождение."""
    try:
        expected = make_discussions_reference(frame.copy(), window_minutes=window)
    except Exception as exc:  # noqa: BLE001 — прежний код падал на пустом входе
        expected = exc
    got = make_discussions(frame, window_minutes=window)
    if isinstance(expected, Exception):
        # Прежний код падал ровно тогда, когда обсуждений не набиралось ни
        # одного, — новый в этом случае отдаёт пустые таблицы.
        if isinstance(expected, KeyError) and got[0].empty and got[1].empty:
            return ""
        return f"прежний код упал ({expected!r}), новый вернул {len(got[0])} обсуждений"
    try:
        assert_frame_equal(got[0], expected[0], check_exact=True)
        assert_frame_equal(got[1], expected[1], check_exact=True)
    except AssertionError as exc:
        return str(exc)[:500]
    return ""


print("1. Совпадает с прежней реализацией на случайных данных")
problems = []
for seed in range(150):
    rng = random.Random(seed)
    frame = messy_frame(rng, rng.choice([1, 2, 5, 20, 60, 150]))
    window = rng.choice([10, 30, 60, 120])
    problem = same_result(frame, window)
    if problem:
        problems.append(f"seed {seed}: {problem}")
check("совпадает с прежней на случайных данных (150 наборов)", not problems, "\n".join(problems[:3]))

print("2. Средний набор: совпадает и заметно быстрее")
rng = random.Random(20260925)
medium = messy_frame(rng, 2000)
for col in ("datetime",):
    medium[col] = pd.to_datetime(medium[col], errors="coerce")
t0 = time.perf_counter()
expected = make_discussions_reference(medium.copy(), window_minutes=60)
old_seconds = time.perf_counter() - t0
t0 = time.perf_counter()
got = make_discussions(medium, window_minutes=60)
new_seconds = time.perf_counter() - t0
try:
    assert_frame_equal(got[0], expected[0], check_exact=True)
    assert_frame_equal(got[1], expected[1], check_exact=True)
    check("2 000 сообщений — таблицы совпадают", True)
except AssertionError as exc:
    check("2 000 сообщений — таблицы совпадают", False, str(exc)[:500])
print(f"     прежняя: {old_seconds:.2f} с, новая: {new_seconds:.2f} с")
# Порог с большим запасом: на машине CI замеры шумные, а проверяется не
# точная цифра, а то, что построчный обход не вернулся.
check("новая быстрее прежней хотя бы вдвое", new_seconds * 2 < old_seconds, f"{new_seconds:.2f} против {old_seconds:.2f}")

print("3. Пустой вход и неизменность входной таблицы")
empty_discussions, empty_links = make_discussions(pd.DataFrame(columns=COLUMNS))
check("на пустом входе — пустые таблицы, а не исключение", empty_discussions.empty and empty_links.empty)
check(
    "у пустых таблиц те же колонки, что у непустых",
    list(empty_discussions.columns) == list(expected[0].columns)
    and list(empty_links.columns) == list(expected[1].columns),
    f"{list(empty_discussions.columns)} / {list(empty_links.columns)}",
)
frame = messy_frame(random.Random(7), 80)
before = frame.copy()
make_discussions(frame, window_minutes=60)
try:
    assert_frame_equal(frame, before)
    check("входная таблица не меняется", True)
except AssertionError as exc:
    check("входная таблица не меняется", False, str(exc)[:300])

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Сборка обсуждений совпадает с прежней до байта и работает быстрее.")
