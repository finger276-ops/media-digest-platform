"""Приведение сырой выгрузки к канонической таблице сообщений.

Вынесено из preprocess.py при распиле монолита. normalize_messages — самая
большая функция бывшего preprocess.py (было 471-829, 359 строк): один проход
по сырому кадру, который определяет практически весь набор колонок messages.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable

import numpy as np
import pandas as pd

from services.message_kinds import classify_kinds

from .microtopics import classify_microtopic
from .tag_parsing import (
    ROW_TAG_TOPIC_FIELDS,
    infer_display_tags,
    normalize_relevant,
    row_tags,
    split_source_topics,
    unique_labels,
)
from .text_cleaning import (
    chat_key_from_link,
    clean_text,
    get_text_series,
    normalize_spaces,
    parse_datetime,
    pick_first_non_empty,
    stable_hash,
)


def is_brand_analytics_dataframe(df: pd.DataFrame) -> bool:
    """Return True for canonical Brand Analytics tables.

    In Brand Analytics exports, information events should be based on the
    source story column (``Сюжет``), while system tag columns after
    ``Обработано`` remain tags/analytics dimensions only.
    """
    if df is None or df.empty:
        return False
    if "source_system" in df.columns:
        source_system = df["source_system"].fillna("").astype(str).str.lower()
        if source_system.eq("brand_analytics").any():
            return True
    if "source_tag_columns" in df.columns:
        return df["source_tag_columns"].fillna("").astype(str).str.strip().ne("").any()
    return False


# Колонки выгрузки, из которых берутся аудитория, охват и вовлечённость. Один
# список и для разбора, и для признака «колонка в выгрузке была»
# (metrics_in_source): разошедшиеся списки дали бы прочерк при живых числах.
SOURCE_METRIC_ALIASES = {
    "audience": ("Аудитория", "Audience", "audience"),
    "reach": ("Просмотры", "Просмотров", "Охват", "Views", "views", "Reach", "reach"),
    "engagement": ("Вовлечённость", "Вовлеченность", "Engagement", "engagement"),
}


def metrics_in_source(raw: pd.DataFrame) -> str:
    """Какие из трёх метрик выгрузка действительно содержит: «audience|reach».

    При разборе пустая ячейка становится нулём, и после него «колонки не было»
    не отличить от «везде ноль». Поэтому признак снимается с исходной таблицы:
    метрика есть, если хотя бы в одной ячейке её колонок есть цифра. Колонка
    из одних нулей — законный ноль, а не отсутствие.
    """
    present = []
    for metric, aliases in SOURCE_METRIC_ALIASES.items():
        for column in aliases:
            if column in raw.columns and bool(
                raw[column].fillna("").astype(str).str.contains(r"\d", regex=True).any()
            ):
                present.append(metric)
                break
    return "|".join(present)


# Счётчики повторов ID для отчёта о загрузке. Под этими именами они лежат в
# манифесте периода и в итоге загрузки, а страница загрузки показывает их
# строками services.import_report.repeated_id_lines.
DUPLICATES_REMOVED = "duplicate_rows_removed"
SHARED_ID_MESSAGES = "shared_id_messages"


def resolve_repeated_ids(
    *,
    message_ids: list[str],
    source_ids: list[str],
    texts: list[str],
    dates: list[str],
    author_profiles: list[str],
    author_names: list[str],
    links: list[str],
    filled_counts: Callable[[list[int]], list[int]],
) -> tuple[list[str], list[int], dict[str, int]]:
    """Разобрать строки с одинаковым ID сообщения (или ссылкой без ID).

    ID сообщения — хеш «ID сообщения», а без него — ссылки. Повтор ID ломал
    загрузку: две строки с одним ID в одной пачке записи Postgres отвергает
    целиком (код 21000), а в разных пачках вторая молча затирает первую.
    Повторы бывают трёх видов: файл склеен из двух выгрузок, выгрузки
    перекрываются по датам, или у разных сообщений одна ссылка (комментарии
    без своего ID приезжают со ссылкой на пост).

    Правило владельца: настоящие повторы схлопнуть, разные сообщения не
    терять ни одного.

    - Одно и то же сообщение — тот же текст, та же дата и тот же автор и
      ссылка там, где они есть (_messages_of_group). Остаётся одна строка —
      самая заполненная (больше непустых ячеек в файле), при равенстве —
      первая.
    - Разные сообщения под одним ID остаются все. Исходный ID сохраняет
      самое раннее по дате (_anchor_order) — у комментариев под постом это
      обычно сам пост. Остальные получают ID из исходного ID, своего текста
      и даты (_derived_id_source). Номер строки не участвует ни там, ни там:
      та же выгрузка в другом порядке строк, как и выгрузка, дополненная
      более поздними комментариями, даёт те же ID, и ручные правки (скрыть,
      перенести) остаются у своих сообщений.
    - Строки без ID и ссылки получили ID по номеру строки, они и так
      разные — их правило не трогает.

    Почему не «первое по файлу»: тогда исходный ID переходил бы к другому
    сообщению при выгрузке «новые сверху», и правки, привязанные к ID,
    молча оказывались бы у чужого сообщения. Цена выбора — периоды,
    загруженные до версии 4.12.7: прежняя запись хранила под общим ID
    последнее по файлу сообщение (остальные терялись), а пересборка отдаст
    этот ID самому раннему. Это сказано в README (версия 4.12.7).

    Возвращает ID для каждой строки, номера строк, которые надо убрать, и
    счётчики для отчёта. filled_counts(позиции) — число непустых ячеек
    исходного файла у этих строк: считается только для строк с повтором,
    чтобы выгрузка без повторов не платила за проверку.
    """
    stats = {DUPLICATES_REMOVED: 0, SHARED_ID_MESSAGES: 0}
    new_ids = list(message_ids)
    has_source = [bool(str(value).strip()) for value in source_ids]
    seen = Counter(mid for mid, sourced in zip(message_ids, has_source) if sourced)
    groups: dict[str, list[int]] = {}
    for pos, (mid, sourced) in enumerate(zip(message_ids, has_source)):
        if sourced and seen[mid] > 1:
            groups.setdefault(mid, []).append(pos)
    if not groups:
        return new_ids, [], stats

    candidates = [pos for positions in groups.values() for pos in positions]
    filled = dict(zip(candidates, filled_counts(candidates)))
    drop: list[int] = []
    for positions in groups.values():
        authors = {
            pos: pick_first_non_empty(author_profiles[pos], author_names[pos])
            for pos in positions
        }
        link_keys = {pos: normalize_spaces(links[pos]) for pos in positions}
        messages = _messages_of_group(positions, texts, dates, authors, link_keys)
        source = source_ids[positions[0]]
        anchor = min(messages, key=_anchor_order)
        same_text_date = Counter(key[:2] for key in messages)
        same_text_date_author = Counter(key[:3] for key in messages)
        for key, rows in messages.items():
            keep = max(rows, key=lambda pos: (filled[pos], -pos))
            drop.extend(pos for pos in rows if pos != keep)
            if key != anchor:
                new_ids[keep] = stable_hash(
                    _derived_id_source(
                        source, key, same_text_date, same_text_date_author
                    ),
                    prefix="m_",
                )
        stats[DUPLICATES_REMOVED] += len(positions) - len(messages)
        if len(messages) > 1:
            stats[SHARED_ID_MESSAGES] += len(messages)
    return new_ids, sorted(drop), stats


def _messages_of_group(
    positions: list[int],
    texts: list[str],
    dates: list[str],
    authors: dict[int, str],
    links: dict[int, str],
) -> dict[tuple[str, str, str, str], list[int]]:
    """Строки группы одного ID → сообщения: ключ (текст, дата, автор, ссылка).

    Сначала сообщения различает текст с датой. Автор различает только строки
    с одинаковыми текстом и датой, и только если среди них хотя бы два
    разных непустых автора. Так копия из выгрузки без колонки автора
    остаётся копией, даже если под той же ссылкой пишут несколько человек:
    у её текста с датой автор один. Если же одинаковый текст с той же датой
    написали двое, строку без автора не к кому отнести — она остаётся
    отдельным сообщением: сомнение решается в пользу «сохранить». Ссылка
    точно так же различает строки с одинаковыми текстом, датой и автором.
    В ключе автор и ссылка пустые, когда ничего не различают.
    """
    by_text_date: dict[tuple[str, str], list[int]] = {}
    for pos in positions:
        by_text_date.setdefault((texts[pos], dates[pos]), []).append(pos)
    messages: dict[tuple[str, str, str, str], list[int]] = {}
    for (text, date), rows in by_text_date.items():
        split_author = len({authors[pos] for pos in rows if authors[pos]}) > 1
        by_author: dict[str, list[int]] = {}
        for pos in rows:
            by_author.setdefault(authors[pos] if split_author else "", []).append(pos)
        for author, same in by_author.items():
            split_link = len({links[pos] for pos in same if links[pos]}) > 1
            for pos in same:
                link = links[pos] if split_link else ""
                messages.setdefault((text, date, author, link), []).append(pos)
    return messages


def _anchor_order(key: tuple[str, str, str, str]) -> tuple[bool, str, str, str, str]:
    """Порядок, в котором сообщение группы получает исходный ID: первое — его.

    Самое раннее по дате; дата хранится как «ГГГГ-ММ-ДД чч:мм:сс», и
    сравнение таких строк совпадает с порядком времени. Сообщения без даты —
    в конце, при равной дате решают текст, автор и ссылка. Номер строки не
    участвует.
    """
    text, date, author, link = key
    return (date == "", date, text, author, link)


def _filled_counts(raw: pd.DataFrame, positions: list[int]) -> list[int]:
    """Сколько непустых ячеек исходного файла у строк с этими номерами.

    Колонки берутся по номеру, а не по имени: в выгрузке бывают одноимённые
    колонки, и обращение по имени вернуло бы таблицу вместо колонки.
    """
    subset = raw.iloc[positions]
    total = np.zeros(len(positions), dtype=int)
    for column in range(subset.shape[1]):
        values = subset.iloc[:, column].fillna("").astype(str).str.strip()
        total += values.ne("").to_numpy(dtype=bool, na_value=False)
    return total.tolist()


def _derived_id_source(
    source: str,
    key: tuple[str, str, str, str],
    same_text_date: Counter,
    same_text_date_author: Counter,
) -> str:
    """Строка, из которой хешируется ID сообщения группы, кроме самого раннего.

    Исходный ID, текст и дата. Автор входит, только если у другого
    сообщения группы те же текст и дата, ссылка — только если совпадает и
    автор: иначе ID менялся бы, когда в выгрузку добавится сообщение
    другого автора или придёт копия без колонки автора. Счётчики — сколько
    сообщений группы с такими текстом и датой (и автором): посчитаны один
    раз на группу, чтобы группа из тысяч строк с одним ID не разбиралась
    попарно. Разделитель — управляющий символ \\x1f: в текстах выгрузок и
    датах его не бывает, так что разные сообщения не склеиваются в одну
    строку. Если совпадение ID всё же случится, загрузка не оборвётся:
    страховка row_id в platform_store оставит последнее и сообщит владельцу.
    """
    text, date, author, link = key
    parts = [str(source), text, date]
    if same_text_date[key[:2]] > 1:
        parts.append(f"author={author}")
    if same_text_date_author[key[:3]] > 1:
        parts.append(f"link={link}")
    return "\x1f".join(parts)


def normalize_messages(
    raw: pd.DataFrame,
    tag_cols: list[str],
    *,
    stats: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Сырая выгрузка → таблица сообщений и их тегов.

    stats, если передан, получает счётчики повторов ID (resolve_repeated_ids):
    сколько копий одного сообщения убрано и сколько разных сообщений делили
    один ID или ссылку.
    """
    df = raw.copy()
    for col in df.columns:
        df[col] = df[col].fillna("").astype(str)

    # Different exports may have slightly different text/date/chat column sets.
    # Always use Series with df.index, never scalar defaults, so new CSV periods
    # cannot fail with length mismatch during assignment.
    message_series = get_text_series(
        df,
        "Сообщение",
        aliases=["Текст", "Текст сообщения", "Message", "message", "text"],
    )
    recognized_series = get_text_series(
        df,
        "Автораспознанный текст",
        aliases=["Распознанный текст", "OCR", "Текст с изображения", "recognized_text"],
    )

    # Колонки идут в циклы списками (.tolist()), а не самими Series: обход
    # Series на строках pyarrow выдаёт значения по одному и заметно медленнее
    # одной массовой выгрузки в список. Значения те же.
    text_pairs = [
        clean_text(message, recognized)
        for message, recognized in zip(
            message_series.tolist(), recognized_series.tolist()
        )
    ]
    df["text_clean"] = [x[0] for x in text_pairs]
    df["text_source"] = [x[1] for x in text_pairs]

    date_series = get_text_series(
        df,
        "Дата",
        aliases=["Дата публикации", "Дата сообщения", "date", "datetime", "created_at"],
    )
    df["datetime"] = parse_datetime(date_series)
    df["date"] = df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")

    # Robust message id: prefer Id сообщения, then link, then row number.
    id_series = get_text_series(
        df, "Id сообщения", aliases=["ID сообщения", "message_id", "id"]
    )
    link_series = get_text_series(
        df,
        "Ссылка",
        aliases=["URL", "Url", "url", "message_link", "Ссылка на сообщение"],
    )
    source_ids = id_series.where(id_series.str.strip() != "", link_series).tolist()
    df["message_id"] = [
        stable_hash(v if str(v).strip() else f"row_{i}", prefix="m_")
        for i, v in enumerate(source_ids)
    ]
    link_values = link_series.tolist()

    blog_profile = get_text_series(
        df,
        "Профиль блога",
        aliases=[
            "Профиль чата",
            "Ссылка на блог",
            "Ссылка на чат",
            "chat_profile",
            "blog_profile",
        ],
    )
    blog_title = get_text_series(
        df,
        "Блог",
        aliases=[
            "Чат",
            "Название чата",
            "Канал",
            "Группа",
            "chat_title",
            "blog",
            "source_name",
        ],
    )
    df["chat_id"] = [
        stable_hash(
            pick_first_non_empty(
                profile, title, chat_key_from_link(link), fallback=f"unknown_chat_{i}"
            ),
            prefix="c_",
        )
        for i, (profile, title, link) in enumerate(
            zip(blog_profile.tolist(), blog_title.tolist(), link_values)
        )
    ]

    author_profile = get_text_series(
        df,
        "Профиль автора",
        aliases=["Ссылка на автора", "author_profile", "user_profile"],
    )
    author_name = get_text_series(
        df,
        "Автор",
        aliases=["Имя автора", "Пользователь", "author", "user_name", "username"],
    )
    author_profiles = author_profile.tolist()
    author_names = author_name.tolist()
    df["author_id"] = [
        stable_hash(
            pick_first_non_empty(profile, author, fallback=f"unknown_author_{i}"),
            prefix="a_",
        )
        for i, (profile, author) in enumerate(zip(author_profiles, author_names))
    ]

    is_brand_analytics = is_brand_analytics_dataframe(df)
    # row_tags читает из строки только колонки тегов и тем через .get(), поэтому
    # ей хватает словаря с этими колонками. Раньше здесь был df.apply(axis=1):
    # он собирал pandas-строку из всех ~70 колонок на каждое сообщение.
    tag_fields = [
        c
        for c in dict.fromkeys(list(tag_cols) + list(ROW_TAG_TOPIC_FIELDS))
        if c in df.columns
    ]
    tag_rows = zip(*(df[c].tolist() for c in tag_fields))
    raw_tag_lists = pd.Series(
        [
            row_tags(
                dict(zip(tag_fields, values)),
                tag_cols,
                include_topic_fields=not is_brand_analytics,
            )
            for values in tag_rows
        ]
        if tag_fields
        else [
            row_tags({}, tag_cols, include_topic_fields=not is_brand_analytics)
            for _ in range(len(df))
        ],
        index=df.index,
        dtype="object",
    )

    if is_brand_analytics:
        # Brand Analytics logic: information events are based on the source
        # story only. Tags from columns after `Обработано` must not create
        # separate topics/events.
        source_main_topic_series = get_text_series(
            df,
            "Сюжет",
            aliases=["source_main_topic", "Тема", "Topic", "Theme", "Основная тема"],
        ).apply(normalize_spaces)
        source_topics_series = source_main_topic_series.apply(
            lambda x: "; ".join(split_source_topics(x))
        )
    else:
        source_main_topic_series = get_text_series(
            df,
            "Основная тема",
            aliases=[
                "Главная тема",
                "Main topic",
                "Primary topic",
                "source_main_topic",
                "Сюжет",
                "Тема",
                "Topic",
            ],
        ).apply(normalize_spaces)
        source_topics_series = get_text_series(
            df,
            "Все темы (список)",
            aliases=[
                "Все темы",
                "Темы",
                "Topics",
                "source_topics",
                "Сюжет",
                "Тема",
                "Topic",
            ],
        ).apply(lambda x: "; ".join(split_source_topics(x)))
        # If there is no separate list of topics, keep the main source topic as a one-item list.
        source_topics_series = source_topics_series.where(
            source_topics_series.str.strip() != "", source_main_topic_series
        )

        # For non-Brand Analytics files, source topic/story labels may be useful
        # as display tags because the file may not contain a dedicated tag block.
        raw_tag_lists = pd.Series(
            [
                unique_labels(list(tags) + [main_topic, topics], limit=8)
                for tags, main_topic, topics in zip(
                    raw_tag_lists.tolist(),
                    source_main_topic_series.tolist(),
                    source_topics_series.tolist(),
                )
            ],
            index=df.index,
            dtype="object",
        )
    relevant_series = get_text_series(
        df,
        "Релевантное",
        aliases=["Релевантность", "Relevant", "Is relevant", "source_relevant"],
    ).apply(lambda x: normalize_relevant(x, default=True))

    df["source_main_topic"] = source_main_topic_series
    df["source_topics"] = source_topics_series
    df["source_relevant"] = relevant_series

    # Narrow rule-based topic used before clustering. For very short replies,
    # include a small parent-post context, but do not let parent text dominate.
    parent_context = get_text_series(
        df,
        "Текст родительского поста",
        aliases=["Родительский пост", "parent_text"],
    ).str.slice(0, 300)
    df["microtopic"] = [
        classify_microtopic((text if len(str(text)) > 45 else f"{text} {parent}"), tags)
        for text, parent, tags in zip(
            df["text_clean"].astype(str).tolist(),
            parent_context.tolist(),
            ["|".join(tags) for tags in raw_tag_lists.tolist()],
        )
    ]

    if is_brand_analytics:
        # Tags are exactly Brand Analytics system/user tags from columns after
        # `Обработано`. Do not append auto-generated semantic tags here.
        tag_lists = [
            list(tags) if list(tags) else ["Без тега"]
            for tags in raw_tag_lists.tolist()
        ]
    else:
        tag_lists = [
            infer_display_tags(text, microtopic, tags)
            for text, microtopic, tags in zip(
                df["text_clean"].astype(str).tolist(),
                df["microtopic"].astype(str).tolist(),
                raw_tag_lists.tolist(),
            )
        ]
    df["tags"] = ["|".join(tags) for tags in tag_lists]
    df["tag_count"] = [len(tags) for tags in tag_lists]

    def as_int(*col_names: str) -> pd.Series:
        """Parse integer metrics from exports.

        Brand Analytics can export numbers as strings with regular spaces,
        non-breaking spaces, narrow non-breaking spaces, quotes or other
        formatting. Empty values are treated as 0.
        """
        series = pd.Series([""] * len(df), index=df.index, dtype="object")
        for col_name in col_names:
            if col_name in df.columns:
                candidate = df[col_name].fillna("").astype(str)
                # Prefer the first existing column that has at least one value.
                # If it is completely empty, keep looking through aliases.
                if candidate.str.strip().ne("").any():
                    series = candidate
                    break
                series = candidate

        cleaned = (
            series.fillna("")
            .astype(str)
            .str.replace("\ufeff", "", regex=False)
            .str.replace("\u00a0", "", regex=False)
            .str.replace("\u202f", "", regex=False)
            .str.replace(" ", "", regex=False)
            .str.replace("\t", "", regex=False)
            .str.replace(r"[^0-9\-]", "", regex=True)
        )
        return pd.to_numeric(cleaned, errors="coerce").fillna(0).astype(int)

    df["duplicate_count"] = as_int("Количество дублей", "Дублей", "Duplicates")
    df["audience"] = as_int(*SOURCE_METRIC_ALIASES["audience"])
    df["views"] = as_int(*SOURCE_METRIC_ALIASES["reach"])
    df["engagement"] = as_int(*SOURCE_METRIC_ALIASES["engagement"])
    # Составляющие вовлечённости нужны метрикам ER и ERR: если выгрузка отдаёт
    # их отдельно, реакции считаются точнее, чем по сводной колонке.
    df["likes"] = as_int("Лайки", "Likes", "likes")
    df["comments"] = as_int("Комментарии", "Комментариев", "Comments", "comments")
    df["reposts"] = as_int("Репосты", "Reposts", "Shares", "reposts")

    sentiment_series = get_text_series(
        df, "Тональность", aliases=["sentiment", "Окраска", "Тон"]
    )
    toxicity_series = get_text_series(df, "Токсичность", aliases=["toxicity", "toxic"])
    df["is_negative"] = sentiment_series.str.lower().str.contains("негатив", na=False)
    df["is_toxic"] = toxicity_series.str.strip().ne("")

    keep_cols = {
        "message_id": "message_id",
        "№": "source_row_no",
        "date": "date",
        "datetime": "datetime",
        "Тип": "message_type",
        "Ссылка": "message_link",
        "Сообщение": "message_raw",
        "Заголовок": "title",
        "Оценка": "rating",
        "Товар": "product",
        # Поля системы-источника, которые раньше обрывались на канонизации.
        # «Роль объекта» отвечает, о бренде ли сообщение или он упомянут
        # вскользь; остальное — портрет автора и данные площадки.
        "Роль объекта": "object_role",
        "Язык": "language",
        "Пол": "author_gender",
        "Возраст": "author_age",
        "Место": "place",
        "Адрес": "address",
        "Цитируемость СМИ": "media_citation",
        "Аудитория СМИ": "media_audience",
        # Поля Медиалогии. Сводная «Аудитория» собрана из двух, но исходные
        # числа сохраняются: у площадки и у автора это разные величины.
        "Хеш сообщения": "source_hash",
        "Аудитория блога": "chat_audience",
        "Аудитория автора": "author_audience",
        "СМ Индекс": "media_index",
        "Семейный статус": "author_family_status",
        "Образование": "author_education",
        "Статус на площадке": "author_status",
        "Объекты": "source_objects",
        "Аспекты": "source_aspects",
        "Мнения": "opinions",
        "Спам": "is_spam_source",
        "Объявления": "is_ad_source",
        "Примечание": "analyst_note",
        "Автораспознанный текст": "recognized_raw",
        "text_clean": "text_clean",
        "text_source": "text_source",
        "Текст родительского поста": "parent_text",
        "Ссылка на родительский пост": "parent_link",
        "Дата публикации родительского поста": "parent_date",
        "Площадка": "platform",
        "Тип площадки": "platform_type",
        "Автор": "author",
        "Профиль автора": "author_profile",
        "Тип автора": "author_type",
        "author_id": "author_id",
        "Блог": "chat_title",
        "Профиль блога": "chat_profile",
        "Тип блога": "chat_type",
        "chat_id": "chat_id",
        "Тональность": "sentiment",
        "Токсичность": "toxicity",
        "WOM": "wom",
        "Страна": "country",
        "Регион": "region",
        "Город": "city",
        "Количество дублей": "duplicate_count_raw",
        "duplicate_count": "duplicate_count",
        "audience": "audience",
        "views": "views",
        "engagement": "engagement",
        "likes": "likes",
        "comments": "comments",
        "reposts": "reposts",
        "tags": "tags",
        "tag_count": "tag_count",
        "microtopic": "microtopic",
        "source_main_topic": "source_main_topic",
        "source_topics": "source_topics",
        "source_relevant": "source_relevant",
        "source_system": "source_system",
        "source_file": "source_file",
        "source_tag_columns": "source_tag_columns",
        "is_negative": "is_negative",
        "is_toxic": "is_toxic",
    }

    available = {k: v for k, v in keep_cols.items() if k in df.columns}
    messages = df[list(available.keys())].rename(columns=available)
    messages["date"] = messages["date"].fillna("")
    messages["datetime"] = pd.to_datetime(messages["datetime"], errors="coerce")
    # Природа сообщения считается здесь, а не при сборке инфоповодов: она
    # нужна и разделу отзывов, и фильтрам ленты, а зависит только от колонок,
    # которые уже приехали. Считать её дважды незачем.
    messages["kind"] = classify_kinds(messages)

    # Повторы ID разбираются над готовой таблицей: ID «по номеру строки»
    # (сообщение без ID, неизвестный автор или чат) к этому моменту уже
    # посчитаны по исходным номерам строк и от удаления копий не съезжают.
    new_ids, drop, repeat_stats = resolve_repeated_ids(
        message_ids=messages["message_id"].tolist(),
        source_ids=source_ids,
        texts=df["text_clean"].tolist(),
        dates=df["date"].tolist(),
        author_profiles=author_profiles,
        author_names=author_names,
        links=link_values,
        filled_counts=lambda positions: _filled_counts(raw, positions),
    )
    if drop or repeat_stats[SHARED_ID_MESSAGES]:
        messages["message_id"] = new_ids
        keep = np.ones(len(messages), dtype=bool)
        keep[drop] = False
        messages = messages.iloc[keep].reset_index(drop=True)
    if stats is not None:
        stats.update(repeat_stats)

    tag_rows = []
    for message_id, tags in zip(messages["message_id"], messages["tags"]):
        for tag in str(tags).split("|"):
            tag = tag.strip()
            if tag:
                tag_rows.append({"message_id": message_id, "tag": tag})
    message_tags = pd.DataFrame(tag_rows, columns=["message_id", "tag"])

    return messages, message_tags
