"""Склейка инфоповодов с почти одинаковыми заголовками.

Зачем это нужно
---------------
`aggregate_events` собирает инфоповоды по заголовку. Для Brand Analytics
заголовок — это «Сюжет», то есть готовая редакционная группировка источника,
и внутри одного периода она работает хорошо. Но есть два места, где точное
совпадение строк разваливает одну тему на несколько:

1. **Между периодами.** `event_id` при загрузке получает префикс периода,
   поэтому единственная связь между апрельским и майским «Сюжетом» — текст
   заголовка. Brand Analytics переформулирует сюжеты («Компания открыла завод
   в Рязани» → «Открытие завода в Рязани»), и одна тема разъезжается на два
   инфоповода.
2. **В алгоритмических проектах**, где заголовок собирается из ключевых слов
   (`build_title`): разный порядок слов и разные хвосты дают разные строки при
   одном смысле.

Что делает модуль
-----------------
Две ступени, обе консервативные:

* `normalize_event_title` — безошибочная нормализация (регистр, ё/е, кавычки,
  тире, многоточия, разделители в числах, пунктуация). Тут склеивается только
  то, что и так одно и то же.
* `group_similar_titles` — сведение близких заголовков к одному «лидеру» по
  взвешенной мере Жаккара с IDF. Без цепочек: каждый участник похож
  непосредственно на лидера группы, а не на соседа по цепочке. Любое слияние
  видно в отчёте `merge_report`, и аналитик может запретить конкретный
  заголовок через `blocked`.

Модуль не ходит в сеть и не зависит от sklearn: на нескольких сотнях
заголовков чистый Python быстрее, чем построение матрицы TF-IDF.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Iterable, Sequence

import pandas as pd

try:  # pragma: no cover - зависит от того, как поднят sys.path
    from settings import RUSSIAN_STOPWORDS
except Exception:  # pragma: no cover
    RUSSIAN_STOPWORDS = set()

# Слова, которые в заголовках инфоповодов почти ничего не различают.
TITLE_STOPWORDS = set(RUSSIAN_STOPWORDS) | {
    "без",
    "названия",
    "новости",
    "новость",
    "сообщение",
    "сообщения",
    "тема",
    "темы",
    "сюжет",
    "публикация",
    "публикации",
    "год",
    "года",
    "году",
    "лет",
    "млн",
    "млрд",
    "тыс",
    "руб",
    "рублей",
    "проц",
}

EMPTY_TITLE = "Без названия"

# Порог по умолчанию. Подобран так, чтобы склеивались переформулировки одного
# сюжета, но не сливались разные события с общим брендом в заголовке.
DEFAULT_SIMILARITY = 0.62
# Заголовки короче этого числа значимых слов сливаются только при точном
# совпадении нормализованной формы: у «Пожар на складе» слишком мало сигнала.
MIN_TOKENS_FOR_FUZZY = 3
# Сколько значимых слов должно быть общим, чтобы вообще рассматривать пару.
MIN_SHARED_TOKENS = 2
# Правило «усечённого заголовка»: короткая сторона почти целиком содержится в
# длинной. Работает только для заголовков от четырёх значимых слов.
CONTAINMENT_RATIO = 0.85
CONTAINMENT_MIN_TOKENS = 4

_QUOTES = re.compile(r"[«»“”„‟\"'`’‘]")
_DASHES = re.compile(r"[–—−‒―]")
_ELLIPSIS = re.compile(r"(\.{2,}|…)")
_URL = re.compile(r"https?://\S+|t\.me/\S+")
_DIGIT_SEP = re.compile(r"(?<=\d)[\s  ](?=\d)")
_NON_WORD = re.compile(r"[^\wёЁ\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_TOKEN = re.compile(r"[а-яa-z][а-яa-z0-9]{2,}")

# Лёгкий стеммер. Полноценная лемматизация (pymorphy) сюда не нужна: заголовки
# короткие, и нам важно только свести «открыла»/«открытие» и «завод»/«завода» к
# одной форме. Срез до пяти букв после снятия окончания даёт это без словаря и
# без риска уронить приложение на лишней зависимости.
_SUFFIXES = tuple(
    sorted(
        (
            "иями", "ями", "ами", "ов", "ев", "ий", "ая", "ое", "ые", "ых",
            "ыми", "ого", "ому", "ем", "ям", "ах", "ях", "ии", "ие", "ия",
            "ью", "ей", "ой", "ый", "ом", "ут", "ют", "ат", "ят", "ла", "ло",
            "ли", "на", "ны", "но", "ть", "ся", "а", "я", "ы", "и", "е", "о",
            "у", "ю", "ь",
        ),
        key=len,
        reverse=True,
    )
)
_STEM_LENGTH = 5
_STEM_MIN_ROOT = 4


def stem_token(token: str) -> str:
    """Грубая основа слова: снять окончание и обрезать до пяти букв."""
    if len(token) <= _STEM_MIN_ROOT:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= _STEM_MIN_ROOT:
            token = token[: -len(suffix)]
            break
    return token[:_STEM_LENGTH]


def normalize_event_title(value: Any) -> str:
    """Привести заголовок к форме, в которой видно только смысловое различие.

    Убираем регистр, ё/е, ссылки, кавычки и тире всех начертаний, многоточия,
    пробелы внутри чисел («1 000» → «1000») и остальную пунктуацию.
    """
    text = str(value if value is not None else "").lower().replace("ё", "е")
    text = _URL.sub(" ", text)
    text = _ELLIPSIS.sub(" ", text)
    text = _QUOTES.sub(" ", text)
    text = _DASHES.sub(" ", text)
    text = _DIGIT_SEP.sub("", text)
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def title_tokens(value: Any) -> set[str]:
    """Значимые основы слов заголовка: без стоп-слов и голых чисел."""
    normalized = value if isinstance(value, str) else normalize_event_title(value)
    return {
        stem_token(token)
        for token in _TOKEN.findall(normalized)
        if token not in TITLE_STOPWORDS and not token.isdigit()
    }


def _idf(token_sets: Sequence[set[str]]) -> dict[str, float]:
    total = max(1, len(token_sets))
    document_freq: dict[str, int] = defaultdict(int)
    for tokens in token_sets:
        for token in tokens:
            document_freq[token] += 1
    return {
        token: math.log(1.0 + total / freq) for token, freq in document_freq.items()
    }


def _mass(tokens: Iterable[str], idf: dict[str, float]) -> float:
    return sum(idf.get(token, 1.0) for token in tokens)


def title_similarity(
    left: set[str], right: set[str], idf: dict[str, float] | None = None
) -> float:
    """Взвешенная мера Жаккара: общий вес слов делим на вес объединения."""
    if not left or not right:
        return 0.0
    idf = idf or {}
    shared = left & right
    if not shared:
        return 0.0
    union_mass = _mass(left | right, idf)
    if union_mass <= 0:
        return 0.0
    return _mass(shared, idf) / union_mass


def _is_truncation(
    left: set[str], right: set[str], idf: dict[str, float]
) -> bool:
    """Один заголовок — усечённая версия другого («…» или обрезанный хвост)."""
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if len(short) < CONTAINMENT_MIN_TOKENS:
        return False
    shared = short & long
    if len(shared) / len(short) < CONTAINMENT_RATIO:
        return False
    short_mass = _mass(short, idf)
    if short_mass <= 0:
        return False
    return _mass(shared, idf) / short_mass >= CONTAINMENT_RATIO


def group_similar_titles(
    titles: Sequence[str],
    weights: Sequence[float] | None = None,
    *,
    threshold: float = DEFAULT_SIMILARITY,
    blocked: set[str] | None = None,
) -> dict[str, str]:
    """Сопоставить каждому нормализованному заголовку заголовок-лидер группы.

    Возвращает словарь `нормализованный заголовок → нормализованный лидер`.
    Заголовки из `blocked` всегда остаются сами по себе и никого не принимают.
    """
    normalized = [normalize_event_title(t) for t in titles]
    # 0 и меньше — склейка выключена, остаётся только точное совпадение
    # нормализованной формы. 1 и больше — недостижимый порог, то же самое.
    if threshold <= 0.0 or threshold >= 1.0 or len(normalized) < 2:
        return {key: key for key in normalized}

    blocked = {normalize_event_title(x) for x in (blocked or set())}
    weights = list(weights or [1.0] * len(normalized))
    if len(weights) < len(normalized):
        weights = weights + [1.0] * (len(normalized) - len(weights))

    # Одна строка на уникальный нормализованный заголовок.
    best_weight: dict[str, float] = {}
    for key, weight in zip(normalized, weights):
        best_weight[key] = best_weight.get(key, 0.0) + float(weight or 0.0)

    unique = list(best_weight)
    tokens = {key: title_tokens(key) for key in unique}
    idf = _idf([tokens[key] for key in unique])

    # Сильные лидеры идут первыми: у крупной темы больше шансов стать центром.
    order = sorted(
        unique, key=lambda key: (-best_weight[key], -len(tokens[key]), key)
    )

    mapping: dict[str, str] = {}
    leaders: list[str] = []
    # Инвертированный индекс по словам, чтобы не сравнивать всё со всем.
    index: dict[str, list[str]] = defaultdict(list)

    for key in order:
        own = tokens[key]
        if key in blocked or len(own) < MIN_TOKENS_FOR_FUZZY:
            mapping[key] = key
            if key not in blocked:
                leaders.append(key)
                for token in own:
                    index[token].append(key)
            continue

        candidates: dict[str, int] = defaultdict(int)
        for token in own:
            for leader in index.get(token, ()):
                candidates[leader] += 1

        best_leader = ""
        best_score = 0.0
        for leader, shared_count in candidates.items():
            if shared_count < MIN_SHARED_TOKENS:
                continue
            other = tokens[leader]
            if len(other) < MIN_TOKENS_FOR_FUZZY:
                continue
            score = title_similarity(own, other, idf)
            if score < threshold and not _is_truncation(own, other, idf):
                continue
            if score > best_score:
                best_score = score
                best_leader = leader

        if best_leader:
            mapping[key] = best_leader
            continue

        mapping[key] = key
        leaders.append(key)
        for token in own:
            index[token].append(key)

    return mapping


def _union_pipe_values(series: pd.Series) -> str:
    values: set[str] = set()
    for raw in series.fillna("").astype(str):
        for part in raw.split("|"):
            part = part.strip()
            if part:
                values.add(part)
    return " | ".join(sorted(values))


def _combine_group(group: pd.DataFrame) -> dict[str, Any]:
    """Собрать один инфоповод из нескольких строк агрегата."""
    counts = pd.to_numeric(
        group.get("message_count", pd.Series(dtype=float)), errors="coerce"
    ).fillna(0)
    lead_pos = int(counts.values.argmax()) if len(counts) else 0
    lead = group.iloc[lead_pos]

    # Варианты первой ступени (точное совпадение нормализованной формы) уже
    # лежат в title_variants — их нельзя терять при второй ступени.
    variants: list[str] = []
    for position, raw_title in enumerate(group["title"].astype(str)):
        candidates: list[str] = [raw_title]
        if "title_variants" in group.columns:
            stored = group["title_variants"].iloc[position]
            if isinstance(stored, (list, tuple)):
                candidates = [str(x) for x in stored] or candidates
        for candidate in candidates:
            if candidate and candidate not in variants:
                variants.append(candidate)
    # Заголовок лидера всегда первый в списке вариантов.
    lead_title = str(lead.get("title") or EMPTY_TITLE)
    variants = [lead_title] + [v for v in variants if v != lead_title]

    event_ids: list[str] = []
    for raw in group.get("event_ids", pd.Series(dtype=object)):
        for value in raw if isinstance(raw, (list, tuple, set)) else []:
            text = str(value)
            if text and text not in event_ids:
                event_ids.append(text)

    row: dict[str, Any] = dict(lead)
    row["title"] = lead_title
    row["title_variants"] = variants
    row["merged_titles"] = max(0, len(variants) - 1)
    row["event_ids"] = event_ids
    row["tags"] = _union_pipe_values(group.get("tags", pd.Series(dtype=str)))
    row["start_date"] = pd.to_datetime(group.get("start_date"), errors="coerce").min()
    row["end_date"] = pd.to_datetime(group.get("end_date"), errors="coerce").max()
    for col in ("message_count", "chat_count", "negative_count"):
        if col in group.columns:
            row[col] = int(
                pd.to_numeric(group[col], errors="coerce").fillna(0).sum()
            )
    if "importance_score" in group.columns:
        row["importance_score"] = float(
            pd.to_numeric(group["importance_score"], errors="coerce").fillna(0).max()
        )
    message_count = int(row.get("message_count") or 0)
    row["negative_share"] = (
        int(row.get("negative_count") or 0) / message_count if message_count else 0
    )
    # Признак остаточной корзины заразителен: если в склейку попал остаток,
    # результат остаётся остатком, каким бы ни был лидер группы. Иначе мешок
    # из сотен сообщений вернётся в рейтинг через заднюю дверь.
    if "is_residual" in group.columns:
        row["is_residual"] = bool(group["is_residual"].fillna(False).any())
    return row


def _sorted_events(out: pd.DataFrame) -> pd.DataFrame:
    """Упорядочить инфоповоды: остаточная корзина всегда последняя.

    Её вес считается по тем же формулам и закономерно выходит наибольшим — в
    ней сотни сообщений, — но это объём мешка, а не значимость события.
    Обнулять вес нельзя: он честно показывает, сколько осталось за кадром.

    Сортировка нужна на обоих выходах merge_similar_events. Когда склеивать
    нечего, функция возвращалась раньше и не сортировала вовсе; порядок держался
    только потому, что вход уже был упорядочен — то есть случайно.
    """
    if out.empty or not {"importance_score", "message_count"} <= set(out.columns):
        return out
    if "is_residual" in out.columns:
        # Пропуски приравниваются к «не остаток»: у периодов, обработанных до
        # появления колонки, её просто нет, и наверх им не место.
        out = out.copy()
        out["is_residual"] = out["is_residual"].fillna(False).astype(bool)
        columns = ["is_residual", "importance_score", "message_count"]
        ascending = [True, False, False]
    else:
        columns = ["importance_score", "message_count"]
        ascending = [False, False]
    return out.sort_values(columns, ascending=ascending).reset_index(drop=True)


def merge_similar_events(
    events_agg: pd.DataFrame,
    *,
    threshold: float = DEFAULT_SIMILARITY,
    blocked: set[str] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Схлопнуть строки агрегата с близкими заголовками.

    Возвращает новый агрегат и отчёт о слияниях — список словарей
    `{"title", "variants", "message_count"}` только по тем группам, где
    объединилось больше одного заголовка. Отчёт нужен, чтобы аналитик видел
    каждое автоматическое решение и мог его отменить.
    """
    if (
        events_agg is None
        or not isinstance(events_agg, pd.DataFrame)
        or events_agg.empty
        or "title" not in events_agg.columns
    ):
        return events_agg, []
    if threshold <= 0.0 or threshold >= 1.0:
        return events_agg, []

    work = events_agg.copy()
    weights = (
        pd.to_numeric(work.get("message_count", 1), errors="coerce").fillna(0).tolist()
    )
    mapping = group_similar_titles(
        work["title"].astype(str).tolist(),
        weights,
        threshold=threshold,
        blocked=blocked,
    )
    work["_merge_key"] = [
        mapping.get(normalize_event_title(t), normalize_event_title(t))
        for t in work["title"].astype(str)
    ]
    if work["_merge_key"].nunique() == len(work):
        # Ничего не склеилось: возвращаем исходный кадр, но с колонками-маркерами,
        # чтобы интерфейс не проверял их наличие каждый раз.
        out = work.drop(columns=["_merge_key"])
        if "title_variants" not in out.columns:
            out["title_variants"] = [[t] for t in out["title"].astype(str)]
        if "merged_titles" not in out.columns:
            out["merged_titles"] = 0
        return _sorted_events(out), []

    rows = [
        _combine_group(group)
        for _, group in work.groupby("_merge_key", sort=False, dropna=False)
    ]
    out = pd.DataFrame(rows)
    if "_merge_key" in out.columns:
        out = out.drop(columns=["_merge_key"])
    out = _sorted_events(out)

    report = [
        {
            "title": str(row.get("title") or ""),
            "variants": list(row.get("title_variants") or []),
            "message_count": int(row.get("message_count") or 0),
        }
        for _, row in out.iterrows()
        if int(row.get("merged_titles") or 0) > 0
    ]
    report.sort(key=lambda item: -item["message_count"])
    return out, report


def preview_merge_levels(
    events_agg: pd.DataFrame,
    levels: Sequence[float] = (0.5, 0.62, 0.75, 0.9),
    *,
    blocked: set[str] | None = None,
) -> pd.DataFrame:
    """Диагностика: сколько инфоповодов останется при разных порогах.

    Нужна, чтобы вопрос «это реальная проблема или тень» решался измерением на
    своих данных, а не на глаз.
    """
    if events_agg is None or events_agg.empty or "title" not in events_agg.columns:
        return pd.DataFrame(columns=["Порог", "Инфоповодов", "Склеено заголовков"])
    rows = []
    base = len(events_agg)
    for level in levels:
        merged, report = merge_similar_events(
            events_agg, threshold=float(level), blocked=blocked
        )
        rows.append(
            {
                "Порог": round(float(level), 2),
                "Инфоповодов": int(len(merged)),
                "Склеено заголовков": int(base - len(merged)),
                "Групп со склейкой": len(report),
            }
        )
    return pd.DataFrame(rows)
