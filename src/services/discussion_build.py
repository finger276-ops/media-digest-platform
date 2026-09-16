"""Сборка обсуждений (веток) из потока сообщений.

Вынесено из preprocess.py при распиле монолита. make_discussions — самый
дорогой по времени шаг конвейера на крупных выгрузках (см.
scripts/loadtest_pipeline.py и комментарий к _DISCUSSION_MESSAGE_COLUMNS ниже).
"""

from __future__ import annotations

from collections import Counter

import pandas as pd

from .tag_parsing import main_tag, source_topic_bucket_value, split_source_topics, tag_set
from .text_cleaning import normalize_spaces, stable_hash


def topic_bucket_for(row: pd.Series) -> str:
    tag = main_tag([row.get("main_tags", row.get("tags", ""))])
    microtopic = str(row.get("microtopic", "other") or "other")
    source_topic = source_topic_bucket_value(row.get("source_main_topic", ""))
    return f"{source_topic}::{tag}::{microtopic}"


def should_start_new_discussion(
    prev_row: pd.Series, row: pd.Series, window_minutes: int
) -> bool:
    if pd.isna(prev_row["datetime"]) or pd.isna(row["datetime"]):
        return False

    gap = row["datetime"] - prev_row["datetime"]
    if gap > pd.Timedelta(minutes=window_minutes):
        return True

    prev_source_topic = normalize_spaces(prev_row.get("source_main_topic", ""))
    curr_source_topic = normalize_spaces(row.get("source_main_topic", ""))
    if (
        prev_source_topic
        and curr_source_topic
        and prev_source_topic != curr_source_topic
    ):
        return True

    prev_micro = str(prev_row.get("microtopic", "other") or "other")
    curr_micro = str(row.get("microtopic", "other") or "other")
    if prev_micro != curr_micro and gap > pd.Timedelta(minutes=12):
        return True

    prev_tags = tag_set(prev_row.get("tags", ""))
    curr_tags = tag_set(row.get("tags", ""))
    if (
        prev_tags
        and curr_tags
        and not (prev_tags & curr_tags)
        and gap > pd.Timedelta(minutes=8)
    ):
        return True

    return False


# Единственные колонки messages, которые читает цикл по группам ниже
# (discussion_id → атрибуты обсуждения). У messages к этому моменту ~70
# колонок (все длинные тексты, включая message_raw/recognized_raw/parent_text),
# и слияние ПОЛНОГО кадра на каждое сообщение — самая дорогая строка функции:
# под pandas 3 со строками на pyarrow каждый .sort_values()/.head()/.iterrows()
# внутри группы копирует и «переплетает» блоки всех колонок, а групп на
# крупной выгрузке — тысячи. Профиль на 5 000 синтетических сообщений
# (scripts/loadtest_pipeline.py) показал на этом шаге 86 секунд, из которых
# добрая половина — работа со столбцами, которые цикл ниже не трогает вовсе.
_DISCUSSION_MESSAGE_COLUMNS = (
    "text_clean",
    "tags",
    "parent_text",
    "source_main_topic",
    "source_topics",
    "microtopic",
    "title",
    "chat_id",
    "chat_title",
    "parent_link",
    "author_id",
    "is_negative",
    "is_toxic",
    "datetime",
    "sort_date",
)


def make_discussions(
    messages: pd.DataFrame, window_minutes: int = 60
) -> tuple[pd.DataFrame, pd.DataFrame]:
    messages = messages.copy()
    messages["parent_link"] = (
        messages.get("parent_link", "").fillna("").astype(str).str.strip()
    )
    messages["datetime"] = pd.to_datetime(messages["datetime"], errors="coerce")
    messages["sort_date"] = messages["datetime"].fillna(pd.Timestamp("1970-01-01"))

    discussion_links = []

    # 1) Parent post is a useful anchor, but comments under one post often drift
    # into several sub-discussions. Split large parent threads by time, tags and microtopic.
    has_parent = messages["parent_link"].ne("")
    for parent_link, group in (
        messages[has_parent]
        .sort_values(["parent_link", "sort_date", "message_id"])
        .groupby("parent_link", sort=False)
    ):
        current_no = 0
        prev = None
        for _, row in group.iterrows():
            if prev is None or should_start_new_discussion(
                prev, row, max(25, window_minutes // 2)
            ):
                current_no += 1
            did = stable_hash(f"{parent_link}::{current_no:04d}", prefix="d_parent_")
            discussion_links.append(
                {
                    "discussion_id": did,
                    "message_id": row["message_id"],
                    "discussion_source": "parent_link_segment",
                }
            )
            prev = row

    # 2) For messages without parent, segment by chat, time, tag overlap and microtopic.
    no_parent = (
        messages[~has_parent].sort_values(["chat_id", "sort_date", "message_id"]).copy()
    )
    for chat_id, group in no_parent.groupby("chat_id", sort=False):
        current_no = 0
        prev = None
        for _, row in group.iterrows():
            if prev is None:
                current_no += 1
            else:
                if should_start_new_discussion(prev, row, window_minutes):
                    current_no += 1
            did = f"d_time_{chat_id}_{current_no:05d}"
            discussion_links.append(
                {
                    "discussion_id": did,
                    "message_id": row["message_id"],
                    "discussion_source": "time_window",
                }
            )
            prev = row

    # Колонки задаём явно: без сообщений список связей пуст, а DataFrame из
    # пустого списка не имеет колонок — слияние по message_id падало бы
    # KeyError'ом на выгрузке без единого разобранного сообщения (неделя без
    # упоминаний или файл с одними заголовками — штатная ситуация).
    discussion_messages = pd.DataFrame(
        discussion_links, columns=["discussion_id", "message_id", "discussion_source"]
    )
    # Сливаем только то, что читает цикл ниже, а не все ~70 колонок messages —
    # см. комментарий к _DISCUSSION_MESSAGE_COLUMNS.
    merge_cols = ["message_id"] + [
        c for c in _DISCUSSION_MESSAGE_COLUMNS if c in messages.columns
    ]
    enriched = discussion_messages.merge(
        messages[merge_cols], on="message_id", how="left"
    )

    rows = []
    for did, group in enriched.groupby("discussion_id", sort=False):
        group = group.sort_values(["sort_date", "message_id"])

        tags = sorted(
            set(
                t
                for tags in group["tags"].fillna("")
                for t in str(tags).split("|")
                if t.strip()
            )
        )
        parent_texts = [
            normalize_spaces(x)
            for x in group.get("parent_text", pd.Series([], dtype=str))
            .fillna("")
            .astype(str)
            .unique()
            if normalize_spaces(x)
        ]
        message_texts = [
            normalize_spaces(x)
            for x in group["text_clean"].fillna("").astype(str).tolist()
            if normalize_spaces(x)
        ]

        # Message text should dominate. Parent text is only a compact context; otherwise
        # comments under the same parent post become artificially too similar.
        parent_block = "\n".join(parent_texts[:1])[:350]
        messages_block = "\n".join(message_texts[:80])
        if len(messages_block) < 120 and parent_block:
            discussion_text = normalize_spaces(
                (messages_block + "\n" + parent_block).strip()
            )
        else:
            discussion_text = normalize_spaces(
                (messages_block + "\n" + parent_block[:180]).strip()
            )

        source_main_topic_counts = Counter(
            normalize_spaces(x)
            for x in group.get("source_main_topic", pd.Series(dtype=str))
            .fillna("")
            .astype(str)
            if normalize_spaces(x)
        )
        source_main_topic = (
            source_main_topic_counts.most_common(1)[0][0]
            if source_main_topic_counts
            else ""
        )
        source_topic_values = []
        for value in (
            group.get("source_topics", pd.Series(dtype=str)).fillna("").astype(str)
        ):
            for topic in split_source_topics(value):
                if topic not in source_topic_values:
                    source_topic_values.append(topic)

        microtopic_counts = Counter(
            group.get("microtopic", pd.Series(["other"])).fillna("other").astype(str)
        )
        microtopic = (
            microtopic_counts.most_common(1)[0][0] if microtopic_counts else "other"
        )

        rep_messages = []
        for _, r in group.head(5).iterrows():
            text = normalize_spaces(r.get("text_clean", ""))
            if text:
                rep_messages.append(text[:300])

        # Заголовок обсуждения — материал для названия инфоповода. Без него
        # название собирается из тега, и список превращается в «пари», «пари»,
        # «винлайн»: у Медиалогии сюжетов нет, а теги повторяются на десятках
        # не связанных между собой публикаций.
        titles = [
            normalize_spaces(value)
            for value in group.get("title", pd.Series(dtype=str)).fillna("").astype(str)
            if normalize_spaces(value)
        ]
        discussion_title = (
            Counter(titles).most_common(1)[0][0] if titles else ""
        )

        rows.append(
            {
                "discussion_id": did,
                "discussion_source": group["discussion_source"].iloc[0],
                "title": discussion_title,
                "start_date": group["datetime"].min(),
                "end_date": group["datetime"].max(),
                "chat_id": (
                    group["chat_id"].iloc[0] if group["chat_id"].nunique() == 1 else ""
                ),
                "chat_title": (
                    group["chat_title"].iloc[0] if "chat_title" in group else ""
                ),
                "parent_link": (
                    group["parent_link"].iloc[0] if "parent_link" in group else ""
                ),
                "main_tags": "|".join(tags),
                "microtopic": microtopic,
                "source_main_topic": source_main_topic,
                "source_topics": "; ".join(source_topic_values),
                "topic_bucket": source_topic_bucket_value(source_main_topic)
                + "::"
                + main_tag(["|".join(tags)])
                + "::"
                + microtopic,
                "message_count": int(group["message_id"].nunique()),
                "author_count": (
                    int(group["author_id"].nunique()) if "author_id" in group else 0
                ),
                "negative_count": (
                    int(group["is_negative"].astype(bool).sum())
                    if "is_negative" in group
                    else 0
                ),
                "toxic_count": (
                    int(group["is_toxic"].astype(bool).sum())
                    if "is_toxic" in group
                    else 0
                ),
                "discussion_text": discussion_text,
                "representative_messages": "\n---\n".join(rep_messages),
            }
        )

    discussions = pd.DataFrame(rows)
    for col in ["start_date", "end_date"]:
        discussions[col] = pd.to_datetime(discussions[col], errors="coerce")

    return discussions, discussion_messages
