"""Разбор русского текста сообщений: токены, ключевые слова, устойчивые фразы.

Вынесено из preprocess, чтобы этим могли пользоваться и сервисы. Иначе
восстановление сюжетов пришлось бы импортировать из конвейера обработки —
зависимость не в ту сторону, да ещё и круговая.

Поведение не менялось при переносе: это тот же токенизатор, на котором
построена вся кластеризация платформы.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from settings import KEYWORD_STOPWORDS, RUSSIAN_STOPWORDS

# Дальше этого предела текст не читаем: длинные посты не улучшают ключевые
# слова, а время на них уходит линейно.
TEXT_SCAN_LIMIT = 3500


def tokenize_ru(text: str, *, for_keywords: bool = False) -> list[str]:
    text = str(text).lower().replace("ё", "е")
    text = re.sub(r"https?://\S+|t\.me/\S+", " ", text)
    tokens = re.findall(r"[а-яa-z0-9]{3,}", text)
    stop = KEYWORD_STOPWORDS if for_keywords else RUSSIAN_STOPWORDS
    return [t for t in tokens if t not in stop and not t.isdigit()]


def top_keywords(texts: Iterable[str], top_n: int = 7) -> list[str]:
    """
    Возвращает чистые ключевые слова для карточки инфоповода.
    В отличие от TF-IDF токенизации, здесь жестче режем мат, бренды и слишком общие слова.
    """
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(tokenize_ru(str(text)[:TEXT_SCAN_LIMIT], for_keywords=True))
    return [w for w, _ in counter.most_common(top_n)]


def top_phrases(texts: Iterable[str], top_n: int = 5) -> list[str]:
    """
    Простая вытяжка устойчивых 2-словных фраз.
    Нужна не для ML, а для более понятного названия/описания.
    """
    counter: Counter[str] = Counter()
    for text in texts:
        tokens = tokenize_ru(str(text)[:TEXT_SCAN_LIMIT], for_keywords=True)
        for a, b in zip(tokens, tokens[1:]):
            if a != b:
                counter[f"{a} {b}"] += 1
    return [p for p, c in counter.most_common(top_n) if c >= 2]
