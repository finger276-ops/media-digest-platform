# -*- coding: utf-8 -*-
"""Разбор выгрузки: в 4–5 раз быстрее, результат тот же до байта.

normalize_messages превращает сырую выгрузку в таблицу сообщений и был
самым медленным шагом конвейера после ускорения сборки обсуждений:
11 секунд на 20 000 сообщений. Три четверти уходило на микротему — по
регулярке на каждый из 90 шаблонов с re.IGNORECASE, который проверяет
шаблон с каждой позиции текста, — и на normalize_spaces, которая гоняла
две регулярные замены по каждому полю, даже когда менять нечего.

Что стало и что стережёт тест:
- normalize_spaces заменяет регуляркой, только если есть что заменить;
  раздел 1 сверяет её с прежней на тексте из пробелов, табуляций,
  неразрывных пробелов и переводов строки;
- микротема: «ядро с начала слова» и «ядро где угодно» ищутся поиском
  подстроки, регулярка — только для двух сложных шаблонов, а текст с
  «двойниками» букв (ᲂ вместо о, ſ вместо s) проверяется полным выражением;
  раздел 2 сверяет с прежней классификацией (tests/microtopics_reference.py)
  на текстах с границами слов, двойниками, заглавными и ё;
- весь разбор сверяется с прежним (tests/message_normalize_reference.py) на
  случайных выгрузках обоих видов — Brand Analytics и без сюжетов — с
  пропусками и неполными колонками (раздел 3).

Мутационные проверки (что ломает какой тест):
- в normalize_spaces проверять только «  », без «\\t» -> «normalize_spaces
  совпадает с прежней» краснеет;
- в _starts_word считать границей любой символ перед ядром -> «микротема
  совпадает с прежней» краснеет (цена после буквы или цифры);
- не проверять двойники (plain всегда True) -> «микротема совпадает с
  прежней» краснеет на ᲂ/ſ;
- снять фоллбэк для сложных шаблонов (считать «не\\s+работа» ядром где
  угодно) -> «микротема совпадает с прежней» краснеет;
- row_tags получает пустой словарь вместо строки -> «разбор совпадает с
  прежним» краснеет.
"""

import os
import random
import re
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

from message_normalize_reference import normalize_messages_reference  # noqa: E402
from microtopics_reference import classify_microtopic as classify_reference  # noqa: E402
from services.message_normalize import normalize_messages  # noqa: E402
from services.microtopics import classify_microtopic  # noqa: E402
from services.text_cleaning import normalize_spaces  # noqa: E402

# Даты нарочно в разнобой: pandas предупреждает, что разбирает их по одной.
warnings.filterwarnings("ignore", message="Could not infer format")
warnings.filterwarnings("ignore", message="Parsing dates in")

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def normalize_spaces_reference(value):
    """Прежняя normalize_spaces (до ускорения) — дословно."""
    value = "" if value is None else str(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


print("1. normalize_spaces совпадает с прежней на любом тексте")
rng = random.Random(1)
alphabet = [" ", " ", "\t", "\xa0", "\n", "\n", "\r", " ", "　", "а", "б", "x", "_", "."]
samples = [None, 0, 1.5, "", "   ", "\n\n\n\n", "\t\t", "\xa0\xa0"]
samples += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30))) for _ in range(20000)]
diff = [repr(s) for s in samples if normalize_spaces(s) != normalize_spaces_reference(s)]
check("normalize_spaces совпадает с прежней (20 000 строк)", not diff, ", ".join(diff[:5]))

print("2. Микротема совпадает с прежней")
RULE_WORDS = [
    "проблема", "жалобы", "недовольны", "не  работает", "не\tработал", "ошибка", "сбой", "брак",
    "цена", "стоимость", "тарифы", "скидка", "акция", "акционный", "акции", "акциз", "счет", "счёт",
    "качество", "плесень", "плесёнь", "наличие", "доставка", "монтаж", "утепление", "сертификат",
    "гост", "пожар", "огнестойкий", "экология", "устойчивый", "конкуренты", "rockwool", "ursa",
    "поддержка", "магазин", "клиенты", "обычное", "слово", "текст",
]
PREFIXES = ["", "", "", " ", "_", "5", "x", "-", "«", "(", "\n", "по", "у"]
LOOKALIKES = {"о": "ᲂ", "с": "ᲃ", "в": "ᲀ", "т": "ᲄ", "д": "ᲁ", "s": "ſ", "k": "K"}


def random_text(rng):
    words = []
    for _ in range(rng.randint(0, 8)):
        word = rng.choice(RULE_WORDS)
        roll = rng.random()
        if roll < 0.15:
            word = "".join(LOOKALIKES.get(ch, ch) if rng.random() < 0.5 else ch for ch in word)
        elif roll < 0.25:
            word = word.upper()
        elif roll < 0.3:
            word = word.capitalize()
        words.append(rng.choice(PREFIXES) + word)
    return rng.choice([" ", "  ", "\n", ", "]).join(words)


TAG_CHOICES = ["", "Качество|Бренд", "Цены", ["Отзывы", " Доставка "], [], "без тега", "nan"]
mismatches = []
for seed in range(4000):
    rng = random.Random(seed)
    text = random_text(rng)
    tags = rng.choice(TAG_CHOICES)
    got, expected = classify_microtopic(text, tags), classify_reference(text, tags)
    if got != expected:
        mismatches.append(f"{text!r} → {got} вместо {expected}")
check("микротема совпадает с прежней (4 000 текстов)", not mismatches, "; ".join(mismatches[:3]))
check("двойник буквы по-прежнему узнаётся", classify_microtopic("пᲂставка", "") == classify_reference("пᲂставка", ""))
check("цена после цифры — не начало слова", classify_microtopic("5цена", "") == classify_reference("5цена", "") == "general")

print("3. Разбор выгрузки совпадает с прежним")
BASE = pd.Timestamp("2026-09-01 10:00")
POOL = {
    "Сообщение": ["Цена выросла", "Проблема с доставкой", "", "Обычный текст про утеплитель", np.nan, "Акция!  Скидка\t10%"],
    "Автораспознанный текст": ["", "Тексты с изображений\nтекст картинки", np.nan, "Расшифровки\r\nречь"],
    "Дата": [lambda i: str(BASE + pd.Timedelta(minutes=37 * i)), "", "25.09.2026 10:00", "не дата", np.nan],
    "Id сообщения": [lambda i: f"id{i}", "", np.nan, "id-dup"],
    "Ссылка": [lambda i: f"https://t.me/chan{i % 3}/{i}", "", "https://site.ru/a"],
    "Профиль блога": ["https://vk.com/blog1", "", np.nan],
    "Блог": ["Блог 1", "Канал", "", np.nan],
    "Профиль автора": ["https://vk.com/u1", "", np.nan],
    "Автор": ["Иван", "Мария", "", np.nan],
    "Сюжет": ["Сюжет 1", " Сюжет  2 ", "", np.nan, "['A', 'B']"],
    "Основная тема": ["Цены", "", np.nan],
    "Все темы": ["A; B", "", "nan"],
    "Теги": ["Качество|Бренд", "", np.nan],
    "Категории": ["Отзывы", ""],
    "Релевантное": ["да", "нет", "", "1", np.nan],
    "Текст родительского поста": ["Пост про цены", "", np.nan],
    "Количество дублей": ["3", "", "1 000", np.nan],
    "Аудитория": ["12 300", "", "abc", np.nan, "1 200"],
    "Просмотры": ["100", "", np.nan],
    "Лайки": ["5", "", np.nan],
    "Тональность": ["Негатив", "Позитив", "нейтральная", "", np.nan],
    "Токсичность": ["", "токсично", np.nan],
    "Тип": ["Пост", "Комментарий", "Репост", ""],
    "Заголовок": ["Заголовок", "", np.nan],
    "ROCKWOOL": ["ROCKWOOL", "", "да", "нет", np.nan],
    "Качество": ["Да", "", "0", "Качество"],
}


def random_raw(rng, n, brand_analytics):
    columns = [c for c in POOL if rng.random() < 0.8] or ["Сообщение"]
    data = {}
    for column in columns:
        options = POOL[column]
        values = []
        for i in range(n):
            value = rng.choice(options)
            values.append(value(i) if callable(value) else value)
        data[column] = values
    if brand_analytics:
        data["source_system"] = ["brand_analytics"] * n
    tag_cols = [c for c in ("ROCKWOOL", "Качество") if c in data and rng.random() < 0.8]
    return pd.DataFrame(data), tag_cols


def compare(raw, tag_cols):
    try:
        expected = normalize_messages_reference(raw.copy(), tag_cols)
    except Exception as exc:  # noqa: BLE001 — сверяется ниже
        expected = exc
    before = raw.copy()
    try:
        got = normalize_messages(raw, tag_cols)
    except Exception as exc:  # noqa: BLE001 — сверяется ниже
        got = exc
    try:
        assert_frame_equal(raw, before)
    except AssertionError:
        return "входная таблица изменилась"
    if isinstance(expected, Exception) or isinstance(got, Exception):
        if type(expected) is type(got):
            return ""
        return f"прежний: {expected!r}, новый: {got!r}"
    try:
        assert_frame_equal(got[0], expected[0], check_exact=True)
        assert_frame_equal(got[1], expected[1], check_exact=True)
    except AssertionError as exc:
        return str(exc)[:400]
    return ""


problems = []
for seed in range(120):
    rng = random.Random(seed)
    raw, tag_cols = random_raw(rng, rng.choice([1, 2, 7, 25, 60]), brand_analytics=seed % 2 == 0)
    problem = compare(raw, tag_cols)
    if problem:
        problems.append(f"seed {seed}: {problem}")
check("разбор совпадает с прежним (120 выгрузок, оба вида)", not problems, "\n".join(problems[:3]))

print("4. Средняя выгрузка: совпадает и заметно быстрее")
rng = random.Random(20260925)
medium, medium_tags = random_raw(rng, 3000, brand_analytics=True)
# Как в настоящих выгрузках: большинство слов ни под одно правило не
# подходит, и прежний разбор прогонял по тексту все 90 шаблонов.
PLAIN_WORDS = [
    "дом", "крыша", "сегодня", "вопрос", "друзья", "город", "лето", "новый", "хороший",
    "день", "люди", "мнение", "вчера", "место", "время", "рядом", "отлично", "спасибо",
]
medium["Сообщение"] = [
    " ".join(
        rng.choice(RULE_WORDS) if rng.random() < 0.01 else rng.choice(PLAIN_WORDS)
        for _ in range(rng.randint(40, 160))
    )
    for _ in range(len(medium))
]
t0 = time.perf_counter()
expected = normalize_messages_reference(medium.copy(), medium_tags)
old_seconds = time.perf_counter() - t0
t0 = time.perf_counter()
got = normalize_messages(medium, medium_tags)
new_seconds = time.perf_counter() - t0
try:
    assert_frame_equal(got[0], expected[0], check_exact=True)
    assert_frame_equal(got[1], expected[1], check_exact=True)
    check("3 000 сообщений — таблицы совпадают", True)
except AssertionError as exc:
    check("3 000 сообщений — таблицы совпадают", False, str(exc)[:400])
print(f"     прежний: {old_seconds:.2f} с, новый: {new_seconds:.2f} с")
# Порог с запасом: на машине CI замеры шумные, а проверяется не цифра, а то,
# что регулярки по каждому шаблону не вернулись.
check("новый быстрее прежнего хотя бы вдвое", new_seconds * 2 < old_seconds, f"{new_seconds:.2f} против {old_seconds:.2f}")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Разбор выгрузки совпадает с прежним до байта и работает быстрее.")
