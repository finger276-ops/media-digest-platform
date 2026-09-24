from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

# Тональность платформа не определяет сама — берёт разметку из выгрузки. Если
# разметки нет совсем, «0 % негатива» и «100 % нейтрала» были бы ложным нулём:
# данных нет, а экран говорит «всё спокойно». Признак разметки живёт здесь, в
# нижнем слое, чтобы им одинаково пользовались экраны, отчёты, карточка для
# ИИ и индексы бренда (brand_metrics импортирует его отсюда).
POSITIVE_PATTERN = "позит|positive|полож"
NEGATIVE_PATTERN = "нег|negative|отриц"
NEGATIVE_FLAG_VALUES = ["true", "1", "yes", "да", "негатив", "negative"]
EMPTY_SENTIMENT_VALUES = {"", "nan", "none", "null"}
NO_SENTIMENT_REASON = (
    "В выгрузке нет разметки тональности: колонка «Тональность» пуста "
    "у всех сообщений периода."
)
NO_SENTIMENT_LABEL = "тональность не размечена"
PERIOD_MARKUP_COLUMN = "_period_sentiment_marked"


def sentiment_text(messages: pd.DataFrame) -> pd.Series:
    """Текст тональности в нижнем регистре, «ё» → «е».

    У строк, приклеенных к подготовленному кадру без подготовки, служебная
    колонка пуста (NaN) — для них текст берётся из исходной разметки.
    """
    def _raw(frame: pd.DataFrame) -> pd.Series:
        return (
            frame.get("sentiment", pd.Series([""] * len(frame), index=frame.index))
            .fillna("")
            .astype(str)
            .str.lower()
            .str.replace("ё", "е", regex=False)
        )

    if "_sentiment_lower" not in messages.columns:
        return _raw(messages)
    prepared = messages["_sentiment_lower"]
    missing = prepared.isna()
    if not bool(missing.any()):
        # Обычный подготовленный кадр: исходную разметку не разбираем заново.
        return prepared.astype(str)
    text = prepared.copy()
    text[missing] = _raw(messages[missing])
    return text.fillna("").astype(str)


def sentiment_masks(messages: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Маски позитивных и негативных сообщений.

    Негатив — по тексту разметки («нег», «negative», «отриц») или по флагу
    is_negative: некоторые выгрузки отмечают только негатив.
    """
    if messages is None or messages.empty:
        empty = pd.Series(dtype=bool)
        return empty, empty
    sentiment = sentiment_text(messages)
    positive = sentiment.str.contains(POSITIVE_PATTERN, regex=True, na=False)
    negative = sentiment.str.contains(NEGATIVE_PATTERN, regex=True, na=False)
    def _raw_flag(frame: pd.DataFrame) -> pd.Series:
        if "is_negative" not in frame.columns:
            return pd.Series(False, index=frame.index)
        return frame["is_negative"].astype(str).str.lower().isin(NEGATIVE_FLAG_VALUES)

    if "_is_negative_bool" in messages.columns:
        flag = messages["_is_negative_bool"]
        missing = flag.isna()
        if bool(missing.any()):
            # NaN — строка без подготовки; astype(bool) дал бы ей «негатив».
            flag = flag.where(~missing, _raw_flag(messages))
        negative = negative | flag.astype(bool)
    else:
        negative = negative | _raw_flag(messages)
    # Сообщение не может быть одновременно позитивным и негативным:
    # при грязной разметке приоритет у негатива, он важнее для рисков.
    positive = positive & ~negative
    return positive, negative


def _row_markup(messages: pd.DataFrame) -> pd.Series:
    """Размечено ли каждое сообщение: непустая тональность или флаг негатива."""
    text = sentiment_text(messages).str.strip()
    _, negative = sentiment_masks(messages)
    return (~text.isin(EMPTY_SENTIMENT_VALUES)) | negative


def has_sentiment_markup(messages: pd.DataFrame) -> bool:
    """Есть ли в выгрузке хоть какая-то разметка тональности.

    «нейтральная» у всех сообщений — это разметка и законный ноль. Пустая
    колонка — отсутствие данных, и тогда тональность показывается прочерком.

    Признак определяется по выгрузке-периоду, а не по срезу: день, тег или
    инфоповод из одних пустых строк внутри размеченной выгрузки — это те же
    неразмеченные-значит-нейтральные сообщения, что и раньше, и у них законный
    ноль. Подготовленный кадр несёт признак периода в PERIOD_MARKUP_COLUMN,
    и любой его срез наследует его. Если к подготовленному кадру приклеены
    сырые строки (NaN в колонке), их досканировать построчно.
    """
    if not isinstance(messages, pd.DataFrame) or messages.empty:
        return False
    if PERIOD_MARKUP_COLUMN in messages.columns:
        flags = messages[PERIOD_MARKUP_COLUMN]
        if flags.notna().all():
            return bool(flags.astype(bool).any())
        if bool(flags.fillna(False).astype(bool).any()):
            return True
    return bool(_row_markup(messages).any())


def sentiment_unmarked(
    sentiment: Mapping[str, Any] | None, messages: pd.DataFrame | None = None
) -> bool:
    """Показать прочерк вместо тональности: сообщения есть, а разметки нет.

    Решает ключ has_markup в словаре sentiment_counts. У словарей без него
    (старые записи кеша, словари, собранные вручную) — признак по messages, а
    без них считается, что разметка есть: лучше прежнее поведение, чем прочерк
    там, где данные на самом деле есть. Пустой период — не «нет разметки», а
    «нет данных», и его показывают как раньше.
    """
    sent = sentiment or {}
    if sent:
        total = int(sent.get("total") or 0)
    else:
        total = len(messages) if isinstance(messages, pd.DataFrame) else 0
    if total <= 0:
        return False
    if "has_markup" in sent:
        return not bool(sent["has_markup"])
    if isinstance(messages, pd.DataFrame) and not messages.empty:
        return not has_sentiment_markup(messages)
    return False


def no_sentiment_line(subject: str, verdict: str = "нет данных") -> str:
    """Строка для текстов саммари и карточки ИИ: «Тональность: нет данных. …»."""
    return f"{subject}: {verdict}. {NO_SENTIMENT_REASON}"


def numeric_series(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    """Return a parsed numeric series from the first useful existing column.

    If a processed metric column exists but contains only zeros while a raw
    alias is also present, try the raw alias before giving up. This helps with
    older uploaded periods and mixed Brand Analytics exports.
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)

    # Prefer pre-parsed dashboard columns when available. This avoids reparsing
    # audience/reach/engagement on every rerun and every chart/table render.
    precomputed_map = {
        "_audience": {"audience", "Аудитория"},
        "_reach": {"views", "Просмотры", "Просмотров", "reach", "Охват"},
        "_engagement": {
            "engagement",
            "Вовлечённость",
            "Вовлеченность",
            "engagement_count",
        },
    }
    requested = set(columns or [])
    for pre_col, aliases in precomputed_map.items():
        if pre_col in df.columns and requested & aliases:
            return pd.to_numeric(df[pre_col], errors="coerce").fillna(0)

    fallback = pd.Series([0] * len(df), index=df.index, dtype=float)

    for col in columns:
        if col not in df.columns:
            continue
        series = (
            df[col]
            .fillna("")
            .astype(str)
            .str.replace("\ufeff", "", regex=False)
            .str.replace("\u00a0", "", regex=False)
            .str.replace("\u202f", "", regex=False)
            .str.replace(" ", "", regex=False)
            .str.replace("\t", "", regex=False)
            .str.replace(r"[^0-9\-]", "", regex=True)
            .pipe(pd.to_numeric, errors="coerce")
            .fillna(0)
        )
        # Use the first column with a non-zero value. Keep a zero fallback in
        # case all aliases are empty or genuinely zero.
        if float(series.sum()) != 0:
            return series
        fallback = series

    return fallback


def prepare_dashboard_messages(messages: pd.DataFrame) -> pd.DataFrame:
    """Add reusable normalized columns for dashboard calculations."""
    if messages is None or messages.empty:
        return messages
    work = messages.copy()
    if "_audience" not in work.columns:
        work["_audience"] = numeric_series(work, ["audience", "Аудитория"]).astype(int)
    if "_audience_place" not in work.columns:
        work["_audience_place"] = audience_place_key(work)
    if "_reach" not in work.columns:
        work["_reach"] = numeric_series(
            work, ["views", "Просмотры", "Просмотров", "reach", "Охват"]
        ).astype(int)
    if "_engagement" not in work.columns:
        work["_engagement"] = numeric_series(
            work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]
        ).astype(int)
    if "_sentiment_lower" not in work.columns:
        work["_sentiment_lower"] = (
            work.get("sentiment", pd.Series([""] * len(work), index=work.index))
            .fillna("")
            .astype(str)
            .str.lower()
            .str.replace("ё", "е", regex=False)
        )
    if "_is_negative_bool" not in work.columns:
        work["_is_negative_bool"] = work["_sentiment_lower"].str.contains(
            "нег|negative|отриц", regex=True, na=False
        )
        if "is_negative" in work.columns:
            work["_is_negative_bool"] = work["_is_negative_bool"] | work[
                "is_negative"
            ].astype(str).str.lower().isin(
                ["true", "1", "yes", "да", "негатив", "negative"]
            )
    if "_period_id_str" not in work.columns and "period_id" in work.columns:
        work["_period_id_str"] = work["period_id"].astype(str)
    # Признак разметки — на всю выгрузку-период: его наследует любой срез
    # (день, неделя, тег, инфоповод), см. has_sentiment_markup.
    if PERIOD_MARKUP_COLUMN not in work.columns:
        marked_row = _row_markup(work)
        if "_period_id_str" in work.columns:
            work[PERIOD_MARKUP_COLUMN] = (
                marked_row.groupby(work["_period_id_str"]).transform("any").astype(bool)
            )
        else:
            work[PERIOD_MARKUP_COLUMN] = bool(marked_row.any())
    return work


# Аудитория — свойство площадки, а не сообщения: у поста и десяти комментариев
# под ним одно и то же число подписчиков. Суммирование по строкам считает
# площадку столько раз, сколько она опубликовала. На выгрузках Brand Analytics
# это давало завышение в 1,5–2,3 раза против сводки самой системы, и заказчик,
# держащий в руках отчёт BA, увидел бы у нас вдвое больший охват.
#
# Личность площадки несёт её адрес, а не название: названия совпадают у разных
# сообществ и меняются между периодами. Порядок колонок проверен на выгрузках
# RUFLEX за июль и август 2026 — воспроизводит число Brand Analytics с точностью
# 0,02% в обоих месяцах. Идентификаторы chat_id/author_id намеренно не
# используются: они заполнены всегда, но различают сообщения, а не площадки.
AUDIENCE_PLACE_COLUMNS = ("chat_profile", "author_profile", "chat_title", "author")


def audience_place_key(messages: pd.DataFrame) -> pd.Series:
    """Ключ площадки для дедупликации аудитории."""
    index = messages.index
    key = pd.Series([""] * len(messages), index=index, dtype="object")
    for column in AUDIENCE_PLACE_COLUMNS:
        if column not in messages.columns:
            continue
        values = messages[column].fillna("").astype(str).str.strip()
        key = key.where(key != "", values)
    # Строка, у которой не нашлось ни адреса, ни названия, считается отдельной
    # площадкой. Слить такие строки в одну — значит занизить аудиторию, а это
    # хуже, чем не сдедуплицировать: недосчёт объяснить нечем.
    return key.where(key != "", pd.Series(index.astype(str), index=index))


def audience_values(messages: pd.DataFrame) -> pd.Series:
    """Числовая аудитория сообщений, с опорой на подготовленную колонку."""
    if "_audience" in messages.columns:
        return pd.to_numeric(messages["_audience"], errors="coerce").fillna(0)
    return numeric_series(messages, ["audience", "Аудитория"])


def audience_total(messages: pd.DataFrame) -> int:
    """Суммарная аудитория площадок — каждая площадка учтена один раз."""
    if messages is None or len(messages) == 0:
        return 0
    values = audience_values(messages)
    if values.empty:
        return 0
    key = (
        messages["_audience_place"]
        if "_audience_place" in messages.columns
        else audience_place_key(messages)
    )
    frame = pd.DataFrame({"_a": values.values, "_k": list(key)})
    return int(frame.groupby("_k")["_a"].max().sum())


def audience_by_group(messages: pd.DataFrame, group: pd.Series) -> pd.Series:
    """Аудитория по группам — площадка учтена один раз внутри каждой группы.

    Сумма по группам может превышать общую аудиторию: площадка, попавшая в два
    тега, честно считается в обоих. Так же устроены и срезы Brand Analytics.
    """
    if messages is None or len(messages) == 0:
        return pd.Series(dtype=float)
    values = audience_values(messages)
    key = (
        messages["_audience_place"]
        if "_audience_place" in messages.columns
        else audience_place_key(messages)
    )
    frame = pd.DataFrame(
        {"_g": list(group), "_k": list(key), "_a": values.values}
    )
    return frame.groupby(["_g", "_k"])["_a"].max().groupby(level=0).sum()


def format_int(value: Any) -> str:
    try:
        return f"{int(float(value)):,}".replace(",", " ")
    except (TypeError, ValueError, OverflowError):
        # OverflowError — это int(inf): бесконечность из деления на ноль
        # показывается нулём, как и любое несчитаемое значение.
        return "0"


def sentiment_counts(messages: pd.DataFrame) -> dict[str, Any]:
    """Return positive/neutral/negative counts for any project profile.

    has_markup — есть ли в выгрузке разметка тональности. Без неё все
    сообщения попадают в «нейтрал», и показывать эти числа нельзя: читать
    признак через sentiment_unmarked, а не по нулю негатива.
    """
    total = int(len(messages)) if isinstance(messages, pd.DataFrame) else 0
    if total == 0:
        return {"positive": 0, "neutral": 0, "negative": 0, "total": 0, "has_markup": False}

    if "_sentiment_lower" in messages.columns:
        sentiment = messages["_sentiment_lower"].fillna("").astype(str)
    else:
        sentiment = (
            messages.get("sentiment", pd.Series([""] * total, index=messages.index))
            .fillna("")
            .astype(str)
            .str.lower()
            .str.replace("ё", "е", regex=False)
        )
    positive_mask = sentiment.str.contains("позит|positive|полож", regex=True, na=False)
    negative_mask = sentiment.str.contains("нег|negative|отриц", regex=True, na=False)
    neutral_mask = sentiment.str.contains("нейтр|neutral", regex=True, na=False)

    if "_is_negative_bool" in messages.columns:
        negative_mask = messages["_is_negative_bool"].astype(bool)
    elif "is_negative" in messages.columns:
        negative_mask = negative_mask | messages["is_negative"].astype(
            str
        ).str.lower().isin(["true", "1", "yes", "да", "негатив", "negative"])

    positive = int(positive_mask.sum())
    negative = int(negative_mask.sum())
    neutral_detected = int(neutral_mask.sum())
    neutral = max(0, total - positive - negative)
    if neutral_detected and neutral_detected > neutral:
        neutral = neutral_detected
        # Keep total stable if imported data has overlapping/dirty sentiment values.
        overflow = positive + negative + neutral - total
        if overflow > 0:
            neutral = max(0, neutral - overflow)
    return {
        "positive": positive,
        "neutral": neutral,
        "negative": negative,
        "total": total,
        "has_markup": has_sentiment_markup(messages),
    }


def percent_text(count: int, total: int) -> str:
    return f"{count / total * 100:.0f}%" if total else "0%"


def overview_metrics(messages: pd.DataFrame) -> dict[str, Any]:
    total_messages = int(len(messages)) if isinstance(messages, pd.DataFrame) else 0
    return {
        "messages": total_messages,
        "audience": audience_total(messages) if total_messages else 0,
        "reach": (
            int(
                numeric_series(
                    messages, ["views", "Просмотры", "Просмотров", "reach", "Охват"]
                ).sum()
            )
            if total_messages
            else 0
        ),
        "engagement": (
            int(
                numeric_series(
                    messages,
                    [
                        "engagement",
                        "Вовлечённость",
                        "Вовлеченность",
                        "engagement_count",
                    ],
                ).sum()
            )
            if total_messages
            else 0
        ),
        "sentiment": sentiment_counts(messages),
    }
