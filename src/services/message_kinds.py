"""Природа сообщения: отзыв, комментарий, репост, публикация.

Brand Analytics отдаёт две независимые оси: тип сообщения (пост, репост,
комментарий) и тип площадки (соцсети, отзывы, видео, СМИ). Их сочетание
отвечает на вопрос, который до сих пор никто не задавал: может ли это
сообщение вообще стать инфоповодом.

Вопрос не праздный. На выгрузках RUFLEX три четверти сообщений оставались без
сюжета Brand Analytics и сваливались в один псевдоповод, который перевешивал
любой настоящий. Но эта корзина неоднородна: примерно каждое седьмое сообщение
в ней — отзыв покупателя на маркетплейсе. Жалоба «плохо прилипают лепестки» не
инфоповод ни в каком смысле, и попытка собрать из таких сообщений событие даёт
мусор. Зато именно там лежит почти весь негатив: 18 из 20 за июль и 11 из 13 за
август. Ему нужен раздел репутации товара, а не лента инфоповодов.

Опознавать отзывы по домену площадки бесполезно: в выгрузках RUFLEX они пришли
с десяти разных площадок, включая vk.com, 2gis.ru и dreamjob.ru. Работает
только собственный признак Brand Analytics — тип площадки.

Модуль намеренно не знает про Streamlit и Supabase: он нужен и воркеру
автозагрузки, и обработке выгрузки, и интерфейсу.
"""

from __future__ import annotations

import pandas as pd

KIND_REVIEW = "review"
KIND_COMMENT = "comment"
KIND_REPOST = "repost"
KIND_EMPTY = "empty"
KIND_POST = "post"

KIND_LABELS = {
    KIND_REVIEW: "Отзыв",
    KIND_COMMENT: "Комментарий",
    KIND_REPOST: "Репост",
    KIND_EMPTY: "Без текста",
    KIND_POST: "Публикация",
}

# Порядок разбора, а не просто перечисление: сообщение может подходить под
# несколько признаков сразу, и побеждает первый подошедший. Отзыв идёт раньше
# комментария, потому что на маркетплейсах отзыв и оформлен комментарием — в
# выгрузках RUFLEX так пришло 113 отзывов из 118. Раньше «без текста» он идёт
# потому, что оценка без комментария — это по-прежнему оценка товара.
KIND_ORDER = (KIND_REVIEW, KIND_COMMENT, KIND_REPOST, KIND_EMPTY, KIND_POST)

# Из чего можно собирать инфоповод.
#
# Репост включён намеренно, хотя и выглядит лишним: в выгрузках RUFLEX 71% и 58%
# репостов дословно повторяют публикацию, которая есть в той же выгрузке, и
# кластеризация сведёт их с оригиналом сама. Но оставшиеся 43 и 70 несут текст,
# которого больше нигде нет — их оригинал за период не попал. Выбросить репосты
# целиком значило бы потерять это содержание.
#
# Отзыв и комментарий исключены по природе: это реакция на чужую публикацию, а
# не событие. Комментарий вдобавок нечем привязать к родителю — в выгрузках
# RUFLEX ссылка на родительский пост не заполнена ни у одного из них.
EVENT_CANDIDATE_KINDS = frozenset({KIND_POST, KIND_REPOST})

_MESSAGE_TYPE_COLUMNS = ("message_type", "Тип")
_PLATFORM_TYPE_COLUMNS = ("platform_type", "Тип площадки")
_TEXT_COLUMNS = ("text_clean", "Сообщение", "message_raw", "Текст")


def _normalized(messages: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """Первая из колонок, приведённая к сравнимому виду."""
    for column in columns:
        if column in messages.columns:
            return (
                messages[column]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.lower()
                .str.replace("ё", "е", regex=False)
            )
    return pd.Series([""] * len(messages), index=messages.index, dtype="object")


def classify_kinds(messages: pd.DataFrame) -> pd.Series:
    """Определить природу каждого сообщения.

    Выгрузка, которая не отдаёт ни тип сообщения, ни тип площадки, получит
    сплошной «post» — это ровно прежнее поведение платформы, когда все
    сообщения считались равноправными кандидатами в инфоповоды.
    """
    if messages is None or len(messages) == 0:
        return pd.Series(dtype="object")

    message_type = _normalized(messages, _MESSAGE_TYPE_COLUMNS)
    platform_type = _normalized(messages, _PLATFORM_TYPE_COLUMNS)
    text = _normalized(messages, _TEXT_COLUMNS)

    # «Оценка без текста» — отзыв, у которого покупатель поставил звёзды и не
    # стал писать. В выгрузках RUFLEX такие всегда приходили с типом площадки
    # «Отзывы», но признак дешёвый и страхует выгрузки победнее.
    is_review = platform_type.str.contains("отзыв|review", regex=True, na=False) | (
        message_type.str.contains("оценка", na=False)
    )
    is_comment = message_type.str.contains("комментар|comment", regex=True, na=False)
    is_repost = message_type.str.contains("репост|repost|share", regex=True, na=False)
    is_empty = text == ""

    kinds = pd.Series(KIND_POST, index=messages.index, dtype="object")
    for kind, mask in (
        (KIND_EMPTY, is_empty),
        (KIND_REPOST, is_repost),
        (KIND_COMMENT, is_comment),
        (KIND_REVIEW, is_review),
    ):
        # Присваиваем от последнего приоритета к первому, чтобы более важный
        # признак перекрывал менее важный без лишних масок.
        kinds = kinds.mask(mask, kind)
    return kinds


def event_candidate_mask(messages: pd.DataFrame) -> pd.Series:
    """Какие сообщения имеет смысл собирать в инфоповоды."""
    if messages is None or len(messages) == 0:
        return pd.Series(dtype=bool)
    kinds = (
        messages["kind"]
        if "kind" in messages.columns
        else classify_kinds(messages)
    )
    return kinds.isin(EVENT_CANDIDATE_KINDS)


def kind_counts(messages: pd.DataFrame) -> dict[str, int]:
    """Сколько сообщений каждой природы — для сводок и отладки."""
    if messages is None or len(messages) == 0:
        return {kind: 0 for kind in KIND_ORDER}
    kinds = (
        messages["kind"]
        if "kind" in messages.columns
        else classify_kinds(messages)
    )
    counts = kinds.value_counts()
    return {kind: int(counts.get(kind, 0)) for kind in KIND_ORDER}
