"""Восстановление сюжетов там, где Brand Analytics их не проставил.

Brand Analytics размечает сюжетом примерно четверть сообщений выгрузки.
Остальные три четверти платформа сваливала в один псевдоповод «Без сюжета»,
который перевешивал любой настоящий инфоповод и вставал первым в списке:
заказчик видел на первом месте мешок из семисот несвязанных сообщений.

Восстановление идёт двумя приёмами, от точного к приблизительному.

Наследование по тексту. Бессюжетное сообщение, чей текст дословно совпадает с
сообщением, у которого сюжет есть, получает тот же сюжет. Это не эвристика, а
факт: один и тот же пост, перепечатанный десятком каналов, — одно событие.
Brand Analytics размечает сюжетом часть таких перепечаток и пропускает
остальные. На выгрузках RUFLEX приём возвращает 72 и 62 сообщения, причём ни
один текст не оказался привязан к двум разным сюжетам — конфликтов нет вовсе.

Кластеризация остатка. То, что не собралось точным совпадением, группируется по
близости текста. Здесь важен порог не по числу сообщений, а по числу авторов:
инфоповод — это когда о чём-то пишут разные люди, а не когда один магазин
двадцать раз разместил одно объявление. Без такого порога в августовской
выгрузке первым «инфоповодом» становились 66 сообщений одного бота недвижимости.

Собирать событие имеет смысл не из всего подряд: отзыв покупателя и комментарий
под чужим постом — реакция, а не событие. Отбор делает services.message_kinds.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from services.message_kinds import event_candidate_mask
from services.ru_text import tokenize_ru, top_keywords, top_phrases

# Откуда у сообщения взялся сюжет — нужно и для отладки, и чтобы показать
# аналитику, что платформа досчитала сама, а что пришло из выгрузки.
ORIGIN_SOURCE = "source"
ORIGIN_INHERITED = "inherited"
ORIGIN_CLUSTERED = "clustered"
ORIGIN_NONE = ""

ORIGIN_LABELS = {
    ORIGIN_SOURCE: "Сюжет выгрузки",
    ORIGIN_INHERITED: "Та же публикация",
    ORIGIN_CLUSTERED: "Собран платформой",
    ORIGIN_NONE: "Вне сюжетов",
}

# Разные люди, а не разные публикации одного автора. Три — порог, при котором на
# выгрузках RUFLEX из списка ушли дилерские рассылки и кросс-постинг, а
# осмысленное осталось.
DEFAULT_MIN_AUTHORS = 3
DEFAULT_MIN_MESSAGES = 2
# Близость текстов для склейки в один сюжет. Выше, чем порог обсуждений внутри
# инфоповода: здесь связный компонент строится по одному ребру, и низкий порог
# сцепляет через цепочку посредников всё подряд.
DEFAULT_SIMILARITY = 0.45
MAX_FEATURES = 6000
# Ниже этого числа сообщений статистика по корпусу не работает: отсев частых и
# редких слов начинает выбрасывать содержание вместо шума.
SMALL_CORPUS = 20
TITLE_MAX_CHARS = 90

_TEXT_COLUMNS = ("text_clean", "Сообщение", "message_raw", "Текст")
_STORY_COLUMNS = ("source_main_topic", "Сюжет", "Основная тема")
_TITLE_COLUMNS = ("title", "Заголовок")
_AUTHOR_COLUMNS = ("author", "Автор", "author_id")


def _column(messages: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    for name in names:
        if name in messages.columns:
            return messages[name].fillna("").astype(str)
    return pd.Series([""] * len(messages), index=messages.index, dtype="object")


def _normalize_text(series: pd.Series) -> pd.Series:
    return (
        series.astype(str)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
        .str.lower()
        .str.replace("ё", "е", regex=False)
    )


# Фраза короче этого не годится в название: «Добрый день!» ровно так и попало
# в сюжеты при первом прогоне на выгрузке RUFLEX.
MIN_TITLE_CHARS = 25
# Дальше третьей фразы не ищем: если в начале поста нет сути, дальше её ищет
# уже не заголовок, а саммари.
TITLE_SENTENCE_LOOKAHEAD = 3


def _first_sentence(text: str) -> str:
    """Первая содержательная фраза сообщения — заготовка названия."""
    cleaned = re.sub(r"https?://\S+", " ", str(text))
    cleaned = re.sub(r"\[[^\]]*\]", " ", cleaned)  # вк-разметка [club123|Имя]
    cleaned = re.sub(r"[#@]\S+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""

    parts = [
        part.strip(" -–—•·:;,")
        for part in re.split(r"(?<=[.!?…])\s+|\n", cleaned)
    ]
    parts = [part for part in parts if part]
    if not parts:
        return cleaned[:TITLE_MAX_CHARS].strip()

    head = parts[:TITLE_SENTENCE_LOOKAHEAD]
    for part in head:
        if len(part) >= MIN_TITLE_CHARS:
            return part[:TITLE_MAX_CHARS].strip()
    # Приветствие, смайлик, «Итак» — сути в начале нет. Берём самую длинную из
    # просмотренных фраз: она ближе к содержанию, чем первая попавшаяся.
    return max(head, key=len)[:TITLE_MAX_CHARS].strip()


def _cluster_title(titles: pd.Series, texts: pd.Series) -> str:
    """Название для сюжета, собранного платформой.

    Заголовок выгрузки предпочитается тексту: у новости, видео или статьи это
    готовая формулировка, а первая строка поста — нет. Но заголовок есть не у
    всех, поэтому запасной путь обязателен.
    """
    named = [t.strip() for t in titles if str(t).strip()]
    if named:
        return str(pd.Series(named).value_counts().index[0])[:TITLE_MAX_CHARS].strip()

    # Самое длинное сообщение кластера обычно и есть исходная публикация,
    # остальные — короткие перепечатки и отклики.
    body = [str(t) for t in texts if str(t).strip()]
    if body:
        sentence = _first_sentence(max(body, key=len))
        if sentence:
            return sentence

    phrases = top_phrases(body, top_n=1)
    if phrases:
        phrase = phrases[0]
        return phrase[:1].upper() + phrase[1:]
    keywords = top_keywords(body, top_n=3)
    if keywords:
        return f"Обсуждение: {', '.join(keywords)}"
    return "Обсуждение без названия"


def _connected_components(texts: list[str], similarity: float) -> np.ndarray:
    """Связные компоненты по косинусной близости TF-IDF.

    Разреженное произведение, а не плотная матрица: на выгрузке в десять тысяч
    сообщений плотная развёртка съела бы сотни мегабайт ради одного шага.
    """
    from scipy.sparse.csgraph import connected_components
    from sklearn.feature_extraction.text import TfidfVectorizer

    if len(texts) < 2:
        return np.zeros(len(texts), dtype=int)

    # Отсев слишком частых слов осмыслен на большом корпусе и разрушителен на
    # маленьком: при трёх текстах слово, встречающееся во всех трёх, имеет
    # df = 1.0 и выбрасывается — от почти одинаковых сообщений не остаётся
    # ничего, и сюжет не собирается вовсе.
    small = len(texts) < SMALL_CORPUS
    vectorizer = TfidfVectorizer(
        tokenizer=tokenize_ru,
        token_pattern=None,
        ngram_range=(1, 2),
        min_df=1 if small else 2,
        max_df=1.0 if small else 0.72,
        max_features=MAX_FEATURES,
        sublinear_tf=True,
    )
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError:
        # Все слова отсеялись стоп-листом — сцеплять нечего.
        return np.arange(len(texts), dtype=int)

    similarities = (matrix @ matrix.T).tocsr()
    similarities.setdiag(0)
    similarities.eliminate_zeros()
    adjacency = similarities >= similarity
    _, labels = connected_components(adjacency, directed=False)

    # Сообщение без единого признака ни с чем не связано по определению, но
    # connected_components сводит такие строки вместе — им нужен свой ярлык,
    # иначе тексты «ок» и «спасибо» образуют общий сюжет.
    empty_rows = np.asarray((matrix.getnnz(axis=1) == 0)).ravel()
    if empty_rows.any():
        next_label = int(labels.max()) + 1
        for position in np.where(empty_rows)[0]:
            labels[position] = next_label
            next_label += 1
    return labels


def recover_stories(
    messages: pd.DataFrame,
    *,
    min_authors: int = DEFAULT_MIN_AUTHORS,
    min_messages: int = DEFAULT_MIN_MESSAGES,
    similarity: float = DEFAULT_SIMILARITY,
) -> pd.DataFrame:
    """Достроить сюжеты и сказать, откуда каждый взялся.

    Возвращает кадр с колонками `story` и `story_origin`, выровненный по
    индексу входных сообщений. Сюжеты из выгрузки не переписываются никогда:
    разметка Brand Analytics авторитетнее любого досчёта.
    """
    empty = pd.DataFrame(
        {"story": pd.Series(dtype="object"), "story_origin": pd.Series(dtype="object")}
    )
    if messages is None or len(messages) == 0:
        return empty

    story = _column(messages, _STORY_COLUMNS).str.strip()
    origin = pd.Series(ORIGIN_NONE, index=messages.index, dtype="object")
    origin = origin.mask(story != "", ORIGIN_SOURCE)

    text = _column(messages, _TEXT_COLUMNS)
    normalized = _normalize_text(text)

    # --- 1. Наследование по дословному совпадению текста ---
    donors = pd.DataFrame({"text": normalized, "story": story})
    donors = donors[(donors["story"] != "") & (donors["text"] != "")]
    if not donors.empty:
        # Один и тот же текст изредка может встретиться с разными сюжетами;
        # берём тот, за которым стоит больше сообщений.
        ranked = (
            donors.groupby(["text", "story"]).size().reset_index(name="n")
            .sort_values("n", ascending=False)
            .drop_duplicates("text")
            .set_index("text")["story"]
        )
        orphan = (story == "") & (normalized != "")
        inherited = normalized.where(orphan).map(ranked)
        found = inherited.notna()
        story = story.mask(found, inherited)
        origin = origin.mask(found, ORIGIN_INHERITED)

    # --- 2. Кластеризация того, что осталось ---
    candidates = event_candidate_mask(messages)
    remaining = (story == "") & (normalized != "") & candidates
    if int(remaining.sum()) >= min_messages:
        subset = messages.loc[remaining]
        labels = _connected_components(
            text.loc[remaining].tolist(), similarity=similarity
        )
        authors = _column(subset, _AUTHOR_COLUMNS).str.strip()
        titles = _column(subset, _TITLE_COLUMNS)
        grouped = pd.DataFrame(
            {"label": labels, "author": authors.values, "index": subset.index}
        )
        for label, group in grouped.groupby("label", sort=False):
            if len(group) < min_messages:
                continue
            distinct_authors = group.loc[group["author"] != "", "author"].nunique()
            if distinct_authors < min_authors:
                continue
            rows = group["index"].tolist()
            title = _cluster_title(titles.loc[rows], text.loc[rows])
            story.loc[rows] = title
            origin.loc[rows] = ORIGIN_CLUSTERED

    return pd.DataFrame({"story": story, "story_origin": origin})


def recovery_counts(recovered: pd.DataFrame) -> dict[str, int]:
    """Сколько сообщений получило сюжет каким способом."""
    if recovered is None or len(recovered) == 0:
        return {key: 0 for key in ORIGIN_LABELS}
    counts = recovered["story_origin"].value_counts()
    return {key: int(counts.get(key, 0)) for key in ORIGIN_LABELS}
