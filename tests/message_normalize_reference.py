"""Прежний разбор выгрузки — эталон для tests/test_message_normalize.py.

Это normalize_messages из src/services/message_normalize.py в том виде, в
каком он был до ускорения (коммит e4cfe2c): df.apply(row_tags, axis=1),
обход Series напрямую, микротема из tests/microtopics_reference.py. Заморожен
намеренно: тест сверяет с ним новый разбор на случайных выгрузках.

Единственное дополнение после заморозки — правило повторов ID (версия
4.12.7, resolve_repeated_ids_reference ниже). Оно меняет результат разбора,
поэтому без него эталон перестал бы совпадать с разбором на любой выгрузке с
повтором. Правило написано здесь заново, в лоб — построчно, без кода из
src: сверка на случайных выгрузках с повторами ID и ссылок проверяет и его.
"""

from __future__ import annotations

import pandas as pd

from services.message_kinds import classify_kinds

from microtopics_reference import classify_microtopic
from services.tag_parsing import (
    infer_display_tags,
    normalize_relevant,
    row_tags,
    split_source_topics,
    unique_labels,
)
from services.text_cleaning import (
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


def _blank(value) -> bool:
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def resolve_repeated_ids_reference(
    raw: pd.DataFrame,
    messages: pd.DataFrame,
    *,
    source_id: pd.Series,
    text: pd.Series,
    date: pd.Series,
    author_profile: pd.Series,
    author_name: pd.Series,
    link: pd.Series,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Правило повторов ID построчно.

    Одно сообщение — тот же текст и дата; автор различает строки с теми же
    текстом и датой, только если среди них два разных непустых автора;
    ссылка — строки с теми же текстом, датой и автором, только если среди
    них две разные непустые ссылки. От сообщения остаётся одна строка, самая
    заполненная, при равенстве первая. Разные сообщения под одним ID
    остаются все: самое раннее по дате (без даты — в конце, дальше текст,
    автор, ссылка) — с исходным ID, остальные — с ID из исходного ID, текста
    и даты, плюс автор, если у другого сообщения те же текст и дата, плюс
    ссылка, если совпадает и автор. Строки без ID и ссылки не трогаются.
    Каждая строка сравнивается со всеми строками своей группы — медленно, но
    без хитростей.
    """
    stats = {"duplicate_rows_removed": 0, "shared_id_messages": 0}
    ids = list(messages["message_id"])
    by_id: dict[str, list[int]] = {}
    for pos in range(len(messages)):
        if str(source_id.iloc[pos]).strip():
            by_id.setdefault(ids[pos], []).append(pos)
    removed: set[int] = set()
    changed = False
    for group in by_id.values():
        if len(group) < 2:
            continue
        changed = True
        t = {p: text.iloc[p] for p in group}
        d = {p: date.iloc[p] for p in group}
        author = {p: pick_first_non_empty(author_profile.iloc[p], author_name.iloc[p]) for p in group}
        url = {p: normalize_spaces(link.iloc[p]) for p in group}
        key_of: dict[int, tuple] = {}
        for p in group:
            twins = [q for q in group if t[q] == t[p] and d[q] == d[p]]
            if len(set(author[q] for q in twins if author[q])) >= 2:
                a = author[p]
                twins = [q for q in twins if author[q] == a]
            else:
                a = ""
            u = url[p] if len(set(url[q] for q in twins if url[q])) >= 2 else ""
            key_of[p] = (t[p], d[p], a, u)
        keys = []
        for p in group:
            if key_of[p] not in keys:
                keys.append(key_of[p])
        anchor = sorted(keys, key=lambda k: (k[1] == "", k[1], k[0], k[2], k[3]))[0]
        for key in keys:
            rows = [p for p in group if key_of[p] == key]
            filled = [
                sum(0 if _blank(v) else 1 for v in raw.iloc[p].tolist()) for p in rows
            ]
            best = rows[filled.index(max(filled))]
            for p in rows:
                if p != best:
                    removed.add(p)
            if key != anchor:
                parts = [str(source_id.iloc[group[0]]), key[0], key[1]]
                if any(k[0] == key[0] and k[1] == key[1] for k in keys if k != key):
                    parts.append("author=" + key[2])
                if any(k[:3] == key[:3] for k in keys if k != key):
                    parts.append("link=" + key[3])
                ids[best] = stable_hash("\x1f".join(parts), prefix="m_")
        stats["duplicate_rows_removed"] += len(group) - len(keys)
        if len(keys) > 1:
            stats["shared_id_messages"] += len(keys)
    if changed:
        messages = messages.copy()
        messages["message_id"] = ids
        messages = messages.iloc[
            [p for p in range(len(messages)) if p not in removed]
        ].reset_index(drop=True)
    return messages, stats


def normalize_messages_reference(
    raw: pd.DataFrame, tag_cols: list[str], *, stats: dict | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
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

    text_pairs = [
        clean_text(message, recognized)
        for message, recognized in zip(message_series, recognized_series)
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
    source_id = id_series.where(id_series.str.strip() != "", link_series)
    df["message_id"] = [
        stable_hash(v if str(v).strip() else f"row_{i}", prefix="m_")
        for i, v in enumerate(source_id.tolist())
    ]

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
            zip(blog_profile, blog_title, link_series)
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
    df["author_id"] = [
        stable_hash(
            pick_first_non_empty(profile, author, fallback=f"unknown_author_{i}"),
            prefix="a_",
        )
        for i, (profile, author) in enumerate(zip(author_profile, author_name))
    ]

    is_brand_analytics = is_brand_analytics_dataframe(df)
    raw_tag_lists = df.apply(
        lambda r: row_tags(r, tag_cols, include_topic_fields=not is_brand_analytics),
        axis=1,
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
                    raw_tag_lists, source_main_topic_series, source_topics_series
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
            df["text_clean"].astype(str),
            parent_context,
            ["|".join(tags) for tags in raw_tag_lists],
        )
    ]

    if is_brand_analytics:
        # Tags are exactly Brand Analytics system/user tags from columns after
        # `Обработано`. Do not append auto-generated semantic tags here.
        tag_lists = [
            list(tags) if list(tags) else ["Без тега"] for tags in raw_tag_lists
        ]
    else:
        tag_lists = [
            infer_display_tags(text, microtopic, tags)
            for text, microtopic, tags in zip(
                df["text_clean"].astype(str),
                df["microtopic"].astype(str),
                raw_tag_lists,
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
    df["audience"] = as_int("Аудитория", "Audience", "audience")
    df["views"] = as_int(
        "Просмотры", "Просмотров", "Охват", "Views", "views", "Reach", "reach"
    )
    df["engagement"] = as_int(
        "Вовлечённость", "Вовлеченность", "Engagement", "engagement"
    )
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

    messages, repeat_stats = resolve_repeated_ids_reference(
        raw,
        messages,
        source_id=source_id,
        text=df["text_clean"],
        date=df["date"],
        author_profile=author_profile,
        author_name=author_name,
        link=link_series,
    )
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
