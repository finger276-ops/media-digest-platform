"""Репутация товара: отзывы покупателей на маркетплейсах и площадках отзывов.

Раздел появился из наблюдения на выгрузках RUFLEX: почти весь негатив периода
лежит в отзывах — 18 из 20 за июль и 11 из 13 за август. В ленте инфоповодов
его не видно и не может быть видно, потому что жалоба на клейкость гонта не
инфоповод: о ней не пишут разные площадки, у неё нет развития во времени, она
вообще не про рынок, а про конкретную посылку.

Зато у отзывов есть своя метрика, которой нет больше ни у чего в выгрузке, —
оценка товара. В выгрузках RUFLEX она заполнена у 117 отзывов из 118 и ровно
ни у одного сообщения другого рода.

Отзывы маркетплейсов приходят по шаблону: «Плюсы товара», «Достоинства»,
«Недостатки», «Комментарий». Разбор шаблона даёт отдельно то, что хвалят, и то,
на что жалуются, — без него всё это одна строка, в которой похвала и претензия
слиплись.
"""

from __future__ import annotations

import re

import pandas as pd

from services.message_kinds import KIND_REVIEW, classify_kinds

# Разделы шаблона отзыва. Порядок важен только для читаемости: разбор идёт по
# всем маркерам сразу, а текст до первого маркера считается свободным.
PROS_MARKERS = ("Плюсы товара", "Плюсы", "Достоинства")
CONS_MARKERS = ("Недостатки", "Минусы", "Минусы товара")
COMMENT_MARKERS = ("Комментарий", "Отзыв")

_ALL_MARKERS = PROS_MARKERS + CONS_MARKERS + COMMENT_MARKERS
_MARKER_PATTERN = re.compile(
    r"(?:^|\s)(" + "|".join(re.escape(m) for m in _ALL_MARKERS) + r")\s*:\s*",
    flags=re.IGNORECASE,
)

_TEXT_COLUMNS = ("text_clean", "Сообщение", "message_raw", "Текст")
_RATING_COLUMNS = ("rating", "Оценка")
# Отдельная колонка товара главнее заголовка: у Brand Analytics товар приезжает
# заголовком карточки, но если выгрузка назвала колонку прямо, верить надо ей.
_PRODUCT_COLUMNS = ("product", "Товар", "title", "Заголовок")

# У Медиалогии нет отдельной колонки товара для отзывов с площадок вроде
# Wildberries/RuStore/Otzovik — название приезжает склеенным в «Заголовок»
# вместе с меткой отзыва и самой оценкой: «Отзыв о Фонбет – ставки на спорт
# Оценка: 1 из 5 Не загружается видео с матча КХЛ!». Взять такую строку
# целиком как товар значило бы показать в карточке кашу из названия, оценки
# и куска текста отзыва. Проверено на реальной выгрузке (8318 строк,
# 122 отзыва, 7 площадок) — три шаблона покрывают 85% строк:
#   «Отзыв о X [Оценка: N из 5 ...]»       — RuStore, Wildberries, Озон, Legalbet
#   «Ответ на отзыв о X [...]»              — App Store, Wildberries, RuStore
#   «Отзыв: X - ...»                        — Otzovik
# Там, где ни один шаблон не подошёл — например, у Irecommend, где в этот же
# «Тип площадки» проваливаются рецензии на фильмы и книги, потому что монитор
# ищет по ключевым словам, а не по товарным карточкам, — заголовок остаётся
# как есть: это уже не наша каша, а естественный шум источника.
_MEDIALOGIA_RATING_SUFFIX = re.compile(r"\s*Оценка:\s*\d\s*из\s*5.*$", re.IGNORECASE | re.DOTALL)
_MEDIALOGIA_REVIEW_PREFIX = re.compile(r"^(?:Ответ на отзыв о|Отзыв о)\s+", re.IGNORECASE)
_MEDIALOGIA_OTZOVIK_PREFIX = re.compile(r"^Отзыв:\s*", re.IGNORECASE)


def _clean_medialogia_title(title: str) -> str:
    """Достать товар из склеенного заголовка Медиалогии, если это возможно.

    Заголовок без узнаваемой склейки возвращается без изменений — это может
    быть уже чистое название (Brand Analytics, будущая колонка «Товар») или
    шум источника, который мы всё равно не умеем разобрать надёжнее, чем есть.
    """
    text = str(title or "").strip()
    if not text:
        return ""
    if _MEDIALOGIA_OTZOVIK_PREFIX.match(text):
        rest = _MEDIALOGIA_OTZOVIK_PREFIX.sub("", text, count=1)
        product, separator, _ = rest.partition(" - ")
        return product.strip() if separator else rest.strip()
    if _MEDIALOGIA_REVIEW_PREFIX.match(text):
        rest = _MEDIALOGIA_REVIEW_PREFIX.sub("", text, count=1)
        rest = _MEDIALOGIA_RATING_SUFFIX.sub("", rest)
        return rest.strip()
    return text


def _product_series(messages: pd.DataFrame) -> pd.Series:
    """Название товара для показа — с очисткой от склейки Медиалогии."""
    return _column(messages, _PRODUCT_COLUMNS).map(_clean_medialogia_title)

# Оценки приходят и дробные — это сводный рейтинг карточки товара, а не ошибка.
RATING_MIN = 1.0
RATING_MAX = 5.0
# Не выше этой оценки отзыв считается претензией, что бы ни говорила разметка
# тональности: три звезды покупатель ставит не от удовольствия.
LOW_RATING = 3.0


def _column(messages: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    """Первая из колонок, в которой есть хоть что-то.

    Пустую колонку пропускаем сознательно. Импорт заводит «Товар» всегда, даже
    когда в файле такой колонки не было, — и если брать просто первую
    существующую, пустой «Товар» заслонит «Заголовок», в котором у Brand
    Analytics и лежит название товара.
    """
    fallback: pd.Series | None = None
    for name in names:
        if name not in messages.columns:
            continue
        values = messages[name].fillna("").astype(str)
        if fallback is None:
            fallback = values
        if values.str.strip().ne("").any():
            return values
    if fallback is not None:
        return fallback
    return pd.Series([""] * len(messages), index=messages.index, dtype="object")


def select_reviews(messages: pd.DataFrame) -> pd.DataFrame:
    """Только отзывы: они и составляют раздел репутации товара."""
    if messages is None or len(messages) == 0:
        return pd.DataFrame()
    kinds = (
        messages["kind"] if "kind" in messages.columns else classify_kinds(messages)
    )
    return messages[kinds == KIND_REVIEW]


def rating_values(messages: pd.DataFrame) -> pd.Series:
    """Оценки товара числом; вне диапазона 1–5 значения отбрасываются."""
    if messages is None or len(messages) == 0:
        return pd.Series(dtype=float)
    raw = _column(messages, _RATING_COLUMNS).str.replace(",", ".", regex=False)
    values = pd.to_numeric(raw.str.strip(), errors="coerce")
    return values.where((values >= RATING_MIN) & (values <= RATING_MAX))


def parse_review(text: str) -> dict[str, str]:
    """Разобрать отзыв маркетплейса на похвалу, претензию и свободный текст.

    Отзыв без единого маркера целиком считается свободным комментарием: так
    приходит примерно каждый четвёртый, и терять его нельзя.
    """
    source = re.sub(r"\s+", " ", str(text or "")).strip()
    result = {"pros": "", "cons": "", "comment": ""}
    if not source:
        return result

    matches = list(_MARKER_PATTERN.finditer(source))
    if not matches:
        result["comment"] = source
        return result

    head = source[: matches[0].start()].strip(" -–—:;,")
    if head:
        result["comment"] = head

    for index, match in enumerate(matches):
        marker = match.group(1).lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        value = source[start:end].strip(" -–—:;,")
        if not value:
            continue
        if any(marker == m.lower() for m in PROS_MARKERS):
            key = "pros"
        elif any(marker == m.lower() for m in CONS_MARKERS):
            key = "cons"
        else:
            key = "comment"
        result[key] = f"{result[key]} {value}".strip() if result[key] else value
    return result


def _strip_leading_title(text: str, title: str) -> str:
    """Убрать из начала отзыва карточку товара.

    Для Brand Analytics заголовок приклеивается к тексту сообщения — это нужно
    поиску и саммари. Но в отзыве заголовок это название товара, и без чистки
    претензией покупателя оказывается строка «DOCKE / Гибкая черепица мягкая
    кровля для крыши дома Серый 3м2».
    """
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    head = re.sub(r"\s+", " ", str(title or "")).strip()
    if head and body.lower().startswith(head.lower()):
        body = body[len(head):].strip(" -–—:;,")
    return body


def parse_reviews(messages: pd.DataFrame) -> pd.DataFrame:
    """Разобранные части отзывов, выровненные по индексу сообщений."""
    if messages is None or len(messages) == 0:
        return pd.DataFrame(columns=["pros", "cons", "comment"])
    texts = _column(messages, _TEXT_COLUMNS)
    titles = _column(messages, _PRODUCT_COLUMNS)
    parsed = [
        parse_review(_strip_leading_title(text, title))
        for text, title in zip(texts, titles)
    ]
    return pd.DataFrame(parsed, index=messages.index)


def review_overview(messages: pd.DataFrame) -> dict[str, object]:
    """Сводка по отзывам периода."""
    reviews = select_reviews(messages)
    total = len(reviews)
    if not total:
        return {
            "reviews": 0,
            "rated": 0,
            "rating_avg": None,
            "rating_counts": {},
            "negative": 0,
            "low_rated": 0,
            "products": 0,
        }

    ratings = rating_values(reviews)
    rounded = ratings.dropna().round().astype(int)
    negative = _negative_mask(reviews)
    products = _product_series(reviews).str.strip()
    return {
        "reviews": total,
        "rated": int(ratings.notna().sum()),
        "rating_avg": float(ratings.mean()) if ratings.notna().any() else None,
        "rating_counts": {
            int(star): int(count) for star, count in rounded.value_counts().items()
        },
        "negative": int(negative.sum()),
        "low_rated": int((ratings <= LOW_RATING).sum()),
        "products": int(products[products != ""].nunique()),
    }


def _negative_mask(reviews: pd.DataFrame) -> pd.Series:
    """Претензия — это низкая оценка ИЛИ негативная тональность.

    Одного признака мало: на выгрузках RUFLEX разметка тональности и оценка
    расходились в обе стороны — 18 негативных против 19 с оценкой не выше трёх
    за июль и 11 против 9 за август.
    """
    ratings = rating_values(reviews)
    low = ratings <= LOW_RATING
    if "is_negative" in reviews.columns:
        by_tone = reviews["is_negative"].fillna(False).astype(bool)
    else:
        by_tone = (
            reviews.get("sentiment", pd.Series([""] * len(reviews), index=reviews.index))
            .fillna("")
            .astype(str)
            .str.lower()
            .str.contains("нег|negative|отриц", regex=True, na=False)
        )
    return (low.fillna(False) | by_tone).astype(bool)


def reviews_by_product(messages: pd.DataFrame) -> pd.DataFrame:
    """Разрез по товару: заголовок отзыва на маркетплейсе — это карточка."""
    reviews = select_reviews(messages)
    if reviews.empty:
        return pd.DataFrame(
            columns=["Товар", "Отзывов", "Средняя оценка", "Претензий"]
        )
    work = reviews.assign(
        _product=_product_series(reviews).str.strip(),
        _rating=rating_values(reviews),
        _negative=_negative_mask(reviews).astype(int),
    )
    work["_product"] = work["_product"].replace("", "Товар не указан")
    grouped = (
        work.groupby("_product")
        .agg(
            Отзывов=("_product", "size"),
            **{"Средняя оценка": ("_rating", "mean")},
            Претензий=("_negative", "sum"),
        )
        .reset_index()
        .rename(columns={"_product": "Товар"})
    )
    grouped["Средняя оценка"] = grouped["Средняя оценка"].round(2)
    return grouped.sort_values(
        ["Претензий", "Отзывов"], ascending=False
    ).reset_index(drop=True)


def complaints(messages: pd.DataFrame) -> pd.DataFrame:
    """Претензии покупателей — то, ради чего раздел и нужен.

    Возвращает разобранные отзывы с низкой оценкой или негативной тональностью:
    товар, оценка, суть претензии, ссылка. Двух десятков штук в месяц слишком
    мало, чтобы строить по ним темы и доли: их читают поштучно.
    """
    reviews = select_reviews(messages)
    if reviews.empty:
        return pd.DataFrame(columns=["Товар", "Оценка", "Претензия", "Ссылка", "Дата"])

    mask = _negative_mask(reviews)
    selected = reviews[mask]
    if selected.empty:
        return pd.DataFrame(columns=["Товар", "Оценка", "Претензия", "Ссылка", "Дата"])

    parsed = parse_reviews(selected)
    # Претензия — это раздел «Недостатки», а если его нет, свободный текст.
    complaint = parsed["cons"].where(parsed["cons"] != "", parsed["comment"])
    complaint = complaint.where(complaint != "", parsed["pros"])
    out = pd.DataFrame(
        {
            "Товар": _product_series(selected).str.strip().replace("", "—"),
            "Оценка": rating_values(selected),
            "Претензия": complaint,
            "Ссылка": _column(selected, ("message_link", "Ссылка")),
            "Дата": _column(selected, ("date", "Дата")),
        },
        index=selected.index,
    )
    return out.sort_values("Оценка", na_position="last").reset_index(drop=True)


def _review_text(pros: str, cons: str, comment: str) -> str:
    """Собрать отзыв обратно в одну читаемую строку, ничего не потеряв.

    complaints() выбирает что-то одно, потому что там нужна суть претензии. В
    общем списке так нельзя: отзыв с плюсами, минусами и комментарием сразу —
    обычное дело на маркетплейсе, и показать из него только треть значит
    соврать про содержание.
    """
    parts = []
    if comment:
        parts.append(comment)
    if pros:
        parts.append(f"Плюсы: {pros}")
    if cons:
        parts.append(f"Минусы: {cons}")
    return " · ".join(parts)


def review_rows(messages: pd.DataFrame) -> pd.DataFrame:
    """Все отзывы периода списком, а не только претензии.

    Поштучно раздел показывал лишь претензии. Когда их ноль, он выглядит пустым
    при непустом счётчике: «Отзывов 14», а прочитать эти четырнадцать негде —
    ровно то, на что смотрел владелец. Разбор шаблона здесь тот же, что в
    complaints(), но без фильтра по негативу.
    """
    columns = ["Оценка", "Товар", "Отзыв", "Тональность", "Дата", "Ссылка"]
    reviews = select_reviews(messages)
    if reviews.empty:
        return pd.DataFrame(columns=columns)

    parsed = parse_reviews(reviews)
    text = [
        _review_text(row["pros"], row["cons"], row["comment"])
        for _, row in parsed.iterrows()
    ]
    out = pd.DataFrame(
        {
            "Оценка": rating_values(reviews),
            "Товар": _product_series(reviews).str.strip().replace("", "—"),
            "Отзыв": pd.Series(text, index=reviews.index),
            "Тональность": _column(reviews, ("sentiment", "Тональность")).str.strip(),
            "Дата": _column(reviews, ("date", "Дата")),
            "Ссылка": _column(reviews, ("message_link", "Ссылка")),
        },
        index=reviews.index,
    )
    # Сначала худшие оценки — раздел про репутацию, а не про ленту. Отзывы без
    # оценки идут следом: их нельзя ранжировать, но и прятать в конец нельзя,
    # потому что в выгрузках без колонки оценки это вообще все отзывы.
    return out.sort_values("Оценка", na_position="last").reset_index(drop=True)


def praise_phrases(messages: pd.DataFrame, top_n: int = 10) -> list[tuple[str, int]]:
    """Что покупатели отмечают как плюс товара.

    Считается только по разделу «Плюсы товара», и только он: на маркетплейсах
    плюсы выбираются галочками из готового списка, поэтому складываются в
    осмысленный счёт — «хорошее качество» 45, «внешний вид» 35.

    Недостатки так считать нельзя, и это не упущение. Они пишутся свободным
    текстом, каждый по-своему, а за месяц их два десятка: любой частотный
    список по ним состоит из единичных обрывков вроде «а 3 где-то потеряли».
    Претензии показываются поштучно — см. complaints().
    """
    reviews = select_reviews(messages)
    if reviews.empty:
        return []
    counter: dict[str, int] = {}
    for value in parse_reviews(reviews)["pros"]:
        for piece in re.split(r"[,;]|\s+и\s+", str(value)):
            piece = piece.strip(" .!—–-").lower()
            # Обрывок в два символа и целое предложение одинаково бесполезны
            # как ярлык.
            if 3 <= len(piece) <= 40:
                counter[piece] = counter.get(piece, 0) + 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
