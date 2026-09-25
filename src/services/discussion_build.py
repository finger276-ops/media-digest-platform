"""Сборка обсуждений (веток) из потока сообщений.

Вынесено из preprocess.py при распиле монолита. make_discussions был самым
дорогим шагом конвейера на крупных выгрузках: построчный iterrows и десяток
операций pandas на каждое из тысяч обсуждений. Теперь сообщения один раз
сортируются целиком, а внутри групп работа идёт по обычным спискам Python —
результат тот же до байта (tests/test_discussion_build.py сверяет с прежней
реализацией), время — в разы меньше (scripts/loadtest_pipeline.py).
"""

from __future__ import annotations

from collections import Counter

import numpy as np
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


# Колонки messages, которые нужны сборке обсуждений. У messages к этому
# моменту ~70 колонок (все длинные тексты, включая message_raw/recognized_raw),
# и тащить их через сортировку и слияние незачем: под pandas 3 со строками на
# pyarrow каждая операция с кадром копирует блоки всех колонок.
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

_DISCUSSION_COLUMNS = [
    "discussion_id",
    "discussion_source",
    "title",
    "start_date",
    "end_date",
    "chat_id",
    "chat_title",
    "parent_link",
    "main_tags",
    "microtopic",
    "source_main_topic",
    "source_topics",
    "topic_bucket",
    "message_count",
    "author_count",
    "negative_count",
    "toxic_count",
    "discussion_text",
    "representative_messages",
]


def _values(frame: pd.DataFrame, column: str, default) -> list:
    """Значения колонки списком; нет колонки — default на каждую строку.

    Ровно то, что давал row.get(column, default) при обходе iterrows.
    """
    if column in frame.columns:
        return frame[column].tolist()
    return [default] * len(frame)


class _Normalizer:
    """normalize_spaces с памятью: сюжеты, заголовки и темы повторяются на
    сотнях сообщений, а регулярные выражения — самое дорогое в сборке.

    Запоминаются только строки: у чисел 1, 1.0 и True одинаковый хеш, но
    разный str(), и общий кеш на них дал бы не тот результат.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._topics: dict[str, list[str]] = {}

    def __call__(self, value) -> str:
        if type(value) is not str:
            return normalize_spaces(value)
        text = self._seen.get(value)
        if text is None:
            text = self._seen[value] = normalize_spaces(value)
        return text

    def topics(self, value) -> list[str]:
        if type(value) is not str:
            return split_source_topics(value)
        topics = self._topics.get(value)
        if topics is None:
            topics = self._topics[value] = split_source_topics(value)
        return topics


def _group_order(frame: pd.DataFrame, key: str) -> tuple[np.ndarray, np.ndarray]:
    """Порядок строк и номер группы — как при обходе groupby(key, sort=False).

    Группы идут в порядке первого появления, строки внутри группы — в порядке
    кадра. Строки с пустым ключом groupby отбрасывает, и здесь они тоже
    выпадают: сообщение без родителя и без chat_id в обсуждения не попадало
    и раньше.
    """
    codes = frame.groupby(key, sort=False).ngroup().to_numpy(dtype=float)
    valid = np.flatnonzero(~np.isnan(codes))
    order = valid[np.argsort(codes[valid], kind="stable")]
    return order, codes[order].astype(np.int64)


def _segment_numbers(
    rows: pd.DataFrame,
    codes: np.ndarray,
    window_minutes: int,
    norm: _Normalizer | None = None,
) -> np.ndarray:
    """Номер обсуждения внутри группы для каждой строки (с 1).

    То же, что цикл «prev is None or should_start_new_discussion(prev, row)»
    по каждой группе: новая ветка начинается на первой строке группы и там,
    где should_start_new_discussion сказал бы True для пары соседних строк.
    Разности времени считаются сразу по всему кадру, а текстовые признаки —
    теми же функциями и с теми же значениями по умолчанию, что и в
    should_start_new_discussion.
    """
    n = len(rows)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    first = np.ones(n, dtype=bool)
    first[1:] = codes[1:] != codes[:-1]

    dt = rows["datetime"].reset_index(drop=True)
    prev_dt = dt.shift(1)
    gap = dt - prev_dt
    both_dated = (dt.notna() & prev_dt.notna()).to_numpy()
    over_window = (gap > pd.Timedelta(minutes=window_minutes)).to_numpy()
    over_12 = (gap > pd.Timedelta(minutes=12)).to_numpy()
    over_8 = (gap > pd.Timedelta(minutes=8)).to_numpy()

    norm = norm or _Normalizer()
    source_topics = [norm(v) for v in _values(rows, "source_main_topic", "")]
    micro = [str(v or "other") for v in _values(rows, "microtopic", "other")]
    tags = [tag_set(v) for v in _values(rows, "tags", "")]

    new = first.copy()
    for i in range(1, n):
        if first[i] or not both_dated[i]:
            continue
        if over_window[i]:
            new[i] = True
            continue
        prev_topic, topic = source_topics[i - 1], source_topics[i]
        if prev_topic and topic and prev_topic != topic:
            new[i] = True
            continue
        if micro[i - 1] != micro[i] and over_12[i]:
            new[i] = True
            continue
        prev_tags, cur_tags = tags[i - 1], tags[i]
        if prev_tags and cur_tags and not (prev_tags & cur_tags) and over_8[i]:
            new[i] = True

    running = np.cumsum(new)
    group_base = np.maximum.accumulate(np.where(first, running, 0))
    return running - group_base + 1


def make_discussions(
    messages: pd.DataFrame, window_minutes: int = 60
) -> tuple[pd.DataFrame, pd.DataFrame]:
    # Работаем с узкой копией: входной кадр не меняется, а ~55 колонок,
    # которые сборке не нужны, не копируются и не сортируются.
    needed = ["message_id"] + [
        c for c in _DISCUSSION_MESSAGE_COLUMNS if c in messages.columns and c != "message_id"
    ]
    work = messages[needed].copy()
    work["parent_link"] = (
        messages["parent_link"].fillna("").astype(str).str.strip()
        if "parent_link" in messages.columns
        else ""
    )
    work["datetime"] = pd.to_datetime(messages["datetime"], errors="coerce")
    work["sort_date"] = work["datetime"].fillna(pd.Timestamp("1970-01-01"))
    norm = _Normalizer()

    # 1) Parent post is a useful anchor, but comments under one post often drift
    # into several sub-discussions. Split large parent threads by time, tags and microtopic.
    has_parent = work["parent_link"].ne("")
    parents = work[has_parent].sort_values(["parent_link", "sort_date", "message_id"])
    order, codes = _group_order(parents, "parent_link")
    parent_rows = parents.iloc[order]
    numbers = _segment_numbers(
        parent_rows, codes, max(25, window_minutes // 2), norm
    )
    parent_ids = [
        stable_hash(f"{link}::{no:04d}", prefix="d_parent_")
        for link, no in zip(parent_rows["parent_link"].tolist(), numbers.tolist())
    ]
    link_ids = parent_ids
    link_messages = parent_rows["message_id"].tolist()
    link_sources = ["parent_link_segment"] * len(parent_ids)

    # 2) For messages without parent, segment by chat, time, tag overlap and microtopic.
    no_parent = work[~has_parent].sort_values(["chat_id", "sort_date", "message_id"])
    order, codes = _group_order(no_parent, "chat_id")
    chat_rows = no_parent.iloc[order]
    numbers = _segment_numbers(chat_rows, codes, window_minutes, norm)
    # В id идёт ключ группы, а groupby берёт им первое значение группы.
    chat_values = chat_rows["chat_id"].tolist()
    group_key = {}
    for code, value in zip(codes.tolist(), chat_values):
        group_key.setdefault(code, value)
    chat_ids = [
        f"d_time_{group_key[code]}_{no:05d}"
        for code, no in zip(codes.tolist(), numbers.tolist())
    ]
    link_ids = link_ids + chat_ids
    link_messages = link_messages + chat_rows["message_id"].tolist()
    link_sources = link_sources + ["time_window"] * len(chat_ids)

    # Одна таблица из одного списка строк, как и раньше: склейка двух
    # таблиц, одна из которых пустая, поменяла бы тип колонок. Колонки
    # задаются явно — без сообщений связей нет, а слияние по message_id
    # должно получить кадр с колонками.
    discussion_messages = pd.DataFrame(
        [
            {"discussion_id": did, "message_id": mid, "discussion_source": source}
            for did, mid, source in zip(link_ids, link_messages, link_sources)
        ],
        columns=["discussion_id", "message_id", "discussion_source"],
    )
    enriched = discussion_messages.merge(work, on="message_id", how="left")
    if enriched.empty:
        discussions = pd.DataFrame(columns=_DISCUSSION_COLUMNS)
        for col in ["start_date", "end_date"]:
            discussions[col] = pd.to_datetime(discussions[col], errors="coerce")
        return discussions, discussion_messages

    # Каждое обсуждение раньше сортировалось отдельно по (sort_date,
    # message_id). Одна устойчивая сортировка всего кадра с номером группы
    # первым ключом даёт тот же порядок строк внутри каждой группы.
    enriched["_group"] = (
        enriched.groupby("discussion_id", sort=False).ngroup().to_numpy()
    )
    enriched = enriched.sort_values(["_group", "sort_date", "message_id"])
    groups = enriched["_group"].to_numpy()
    bounds = np.flatnonzero(np.r_[True, groups[1:] != groups[:-1], True])

    # Числа по группам — одним проходом groupby, а не по кадру на группу.
    by_group = enriched.groupby("_group", sort=True)
    start_dates = by_group["datetime"].min().tolist()
    end_dates = by_group["datetime"].max().tolist()
    chat_nunique = by_group["chat_id"].nunique().tolist()
    message_counts = by_group["message_id"].nunique().tolist()
    author_counts = (
        by_group["author_id"].nunique().tolist()
        if "author_id" in enriched.columns
        else None
    )

    def _bool_sums(column: str):
        if column not in enriched.columns:
            return None
        return enriched[column].astype(bool).groupby(enriched["_group"]).sum().tolist()

    negative_counts = _bool_sums("is_negative")
    toxic_counts = _bool_sums("is_toxic")

    def _filled(column: str, fill: str):
        if column not in enriched.columns:
            return None
        return enriched[column].fillna(fill).astype(str).tolist()

    discussion_ids = enriched["discussion_id"].tolist()
    discussion_sources = enriched["discussion_source"].tolist()
    tags_values = enriched["tags"].fillna("").tolist()
    parent_texts = _filled("parent_text", "")
    # Текст каждого сообщения нормализуется один раз: он нужен и для текста
    # обсуждения, и для представительных сообщений. Представительные раньше
    # брались без fillna, и пустой текст давал «nan» — так и оставлено.
    texts = [
        normalize_spaces(v)
        for v in enriched["text_clean"].fillna("").astype(str).tolist()
    ]
    missing_text = enriched["text_clean"].isna().tolist()
    raw_texts = enriched["text_clean"].tolist()
    main_topics = _filled("source_main_topic", "")
    topic_lists = _filled("source_topics", "")
    microtopics = _filled("microtopic", "other")
    titles_values = _filled("title", "")
    chat_ids_values = enriched["chat_id"].tolist()
    chat_titles = (
        enriched["chat_title"].tolist() if "chat_title" in enriched.columns else None
    )
    parent_link_values = enriched["parent_link"].tolist()

    rows = []
    for g, (start, end) in enumerate(zip(bounds[:-1].tolist(), bounds[1:].tolist())):
        tags = sorted(
            set(
                t
                for value in tags_values[start:end]
                for t in str(value).split("|")
                if t.strip()
            )
        )
        parent_block = ""
        if parent_texts is not None:
            for value in parent_texts[start:end]:
                text = norm(value)
                if text:
                    parent_block = text[:350]
                    break
        message_texts = []
        for text in texts[start:end]:
            if text:
                message_texts.append(text)
                if len(message_texts) == 80:
                    break

        # Message text should dominate. Parent text is only a compact context; otherwise
        # comments under the same parent post become artificially too similar.
        messages_block = "\n".join(message_texts)
        if len(messages_block) < 120 and parent_block:
            discussion_text = normalize_spaces(
                (messages_block + "\n" + parent_block).strip()
            )
        else:
            discussion_text = normalize_spaces(
                (messages_block + "\n" + parent_block[:180]).strip()
            )

        source_main_topic_counts = Counter()
        if main_topics is not None:
            source_main_topic_counts = Counter(
                text
                for text in (norm(x) for x in main_topics[start:end])
                if text
            )
        source_main_topic = (
            source_main_topic_counts.most_common(1)[0][0]
            if source_main_topic_counts
            else ""
        )
        source_topic_values = []
        if topic_lists is not None:
            for value in topic_lists[start:end]:
                for topic in norm.topics(value):
                    if topic not in source_topic_values:
                        source_topic_values.append(topic)

        microtopic_counts = Counter(
            microtopics[start:end] if microtopics is not None else ["other"]
        )
        microtopic = (
            microtopic_counts.most_common(1)[0][0] if microtopic_counts else "other"
        )

        rep_messages = []
        for i in range(start, min(end, start + 5)):
            text = normalize_spaces(raw_texts[i]) if missing_text[i] else texts[i]
            if text:
                rep_messages.append(text[:300])

        # Заголовок обсуждения — материал для названия инфоповода. Без него
        # название собирается из тега, и список превращается в «пари», «пари»,
        # «винлайн»: у Медиалогии сюжетов нет, а теги повторяются на десятках
        # не связанных между собой публикаций.
        titles = []
        if titles_values is not None:
            titles = [
                text
                for text in (norm(v) for v in titles_values[start:end])
                if text
            ]
        discussion_title = Counter(titles).most_common(1)[0][0] if titles else ""

        rows.append(
            {
                "discussion_id": discussion_ids[start],
                "discussion_source": discussion_sources[start],
                "title": discussion_title,
                "start_date": start_dates[g],
                "end_date": end_dates[g],
                "chat_id": chat_ids_values[start] if chat_nunique[g] == 1 else "",
                "chat_title": chat_titles[start] if chat_titles is not None else "",
                "parent_link": parent_link_values[start],
                "main_tags": "|".join(tags),
                "microtopic": microtopic,
                "source_main_topic": source_main_topic,
                "source_topics": "; ".join(source_topic_values),
                "topic_bucket": source_topic_bucket_value(source_main_topic)
                + "::"
                + main_tag(["|".join(tags)])
                + "::"
                + microtopic,
                "message_count": int(message_counts[g]),
                "author_count": int(author_counts[g]) if author_counts is not None else 0,
                "negative_count": (
                    int(negative_counts[g]) if negative_counts is not None else 0
                ),
                "toxic_count": int(toxic_counts[g]) if toxic_counts is not None else 0,
                "discussion_text": discussion_text,
                "representative_messages": "\n---\n".join(rep_messages),
            }
        )

    discussions = pd.DataFrame(rows)
    for col in ["start_date", "end_date"]:
        discussions[col] = pd.to_datetime(discussions[col], errors="coerce")

    return discussions, discussion_messages
