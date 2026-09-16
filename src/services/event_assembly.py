"""Сборка инфоповодов: из сюжетов Brand Analytics или из кластеров обсуждений.

Вынесено из preprocess.py при распиле монолита. Не путать с
services/event_enrichment.py — там про обогащение уже СОХРАНЁННЫХ таблиц
для дашборда (enrich_messages/aggregate_events), а здесь — про сборку
инфоповодов из discussions на этапе обработки выгрузки.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pandas as pd

from settings import CRITICAL_TAG_WEIGHTS, MICROTOPIC_TITLES
from services.event_titles import normalize_event_title
from services.ru_text import top_keywords, top_phrases
from services.story_recovery import (
    DEFAULT_MIN_AUTHORS as DEFAULT_MIN_EVENT_AUTHORS,
    DEFAULT_MIN_MESSAGES as DEFAULT_MIN_EVENT_MESSAGES,
    RESIDUAL_STORY_TITLE,
    is_residual_title,
)

from .discussion_titles import build_title, summarize_event
from .tag_parsing import label_microtopic, main_tag, split_source_topics, tag_set_from_series
from .text_cleaning import normalize_spaces, stable_hash


def make_events_from_source_stories(
    discussions: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build one information event per Brand Analytics `Сюжет`.

    Brand Analytics already contains a human/system story column. For these
    exports tags are analytical dimensions, not event boundaries, so we avoid
    clustering by tag or text and group discussions directly by the story.
    """
    if discussions is None or discussions.empty:
        return pd.DataFrame(), pd.DataFrame(columns=["event_id", "discussion_id"])

    d = discussions.copy()
    if "source_main_topic" not in d.columns:
        d["source_main_topic"] = ""
    d["__story"] = d["source_main_topic"].fillna("").astype(str).map(normalize_spaces)
    d["__story"] = d["__story"].replace("", RESIDUAL_STORY_TITLE)
    # Идентификатор сюжета считаем по нормализованной форме: кавычки, ё/е,
    # регистр и многоточие в конце — это тот же сюжет, а не новый. Название
    # при этом остаётся исходным, в формулировке Brand Analytics.
    d["__story_key"] = d["__story"].map(normalize_event_title).replace("", "без сюжета")
    d["event_id"] = d["__story_key"].apply(lambda x: stable_hash(x, prefix="e_story_"))
    event_discussions = d[["event_id", "discussion_id"]].copy()

    rows = []
    for event_id, group in d.groupby("event_id", sort=False):
        # В группе могут оказаться несколько написаний одного сюжета —
        # показываем то, за которым стоит больше сообщений.
        story_counts = (
            group.groupby("__story")["message_count"].sum().sort_values(ascending=False)
            if "message_count" in group.columns
            else group["__story"].value_counts()
        )
        story = str(
            story_counts.index[0] if len(story_counts) else RESIDUAL_STORY_TITLE
        )
        keywords = top_keywords(
            group["discussion_text"].fillna("").astype(str), top_n=7
        )
        phrases = top_phrases(group["discussion_text"].fillna("").astype(str), top_n=5)
        start = pd.to_datetime(group["start_date"], errors="coerce").min()
        end = pd.to_datetime(group["end_date"], errors="coerce").max()
        msg_count = int(group["message_count"].sum())
        discussion_count = int(group["discussion_id"].nunique())
        chat_count = (
            int(group["chat_id"].replace("", np.nan).nunique())
            if "chat_id" in group
            else 0
        )
        author_count = (
            int(group["author_count"].sum()) if "author_count" in group else 0
        )
        negative_count = (
            int(group["negative_count"].sum()) if "negative_count" in group else 0
        )
        toxic_count = int(group["toxic_count"].sum()) if "toxic_count" in group else 0
        all_tags = sorted(
            tag_set_from_series(group.get("main_tags", pd.Series(dtype=str)))
        )
        tag = main_tag(group.get("main_tags", pd.Series(dtype=str)))
        negative_share = negative_count / msg_count if msg_count else 0.0
        toxic_share = toxic_count / msg_count if msg_count else 0.0
        tag_weight = max([CRITICAL_TAG_WEIGHTS.get(t, 1.0) for t in all_tags] or [1.0])
        importance_score = (
            math.log1p(msg_count) * 2
            + math.log1p(max(chat_count, 1)) * 2.5
            + math.log1p(max(author_count, 1)) * 0.8
            + negative_share * 4
            + toxic_share * 2
            + tag_weight
        )
        rows.append(
            {
                "event_id": event_id,
                "event_title": story,
                "event_summary": summarize_event(group, story, keywords, phrases),
                "main_tag": tag,
                "microtopic": label_microtopic(story),
                "main_tags": "|".join(all_tags),
                "source_main_topic": story,
                "source_topics": story,
                "keywords": "|".join(keywords),
                "key_phrases": "|".join(phrases),
                "start_date": start,
                "end_date": end,
                "discussion_count": discussion_count,
                "message_count": msg_count,
                "chat_count": chat_count,
                "author_count": author_count,
                "negative_count": negative_count,
                "toxic_count": toxic_count,
                "negative_share": round(negative_share, 4),
                "toxic_share": round(toxic_share, 4),
                "importance_score": round(float(importance_score), 2),
                "status": "новый",
                "is_hidden": False,
                "is_residual": is_residual_title(story),
                "event_source": "brand_analytics_story",
            }
        )

    events = pd.DataFrame(rows)
    if not events.empty:
        # Остаточная корзина сортируется последней, а не по важности. Её вес
        # считается по тем же формулам и закономерно выходит наибольшим —
        # в ней сотни сообщений, — но это свойство мешка, а не события.
        # Обнулять вес нельзя: он честно показывает объём остатка.
        events = events.sort_values(
            ["is_residual", "importance_score", "message_count"],
            ascending=[True, False, False],
        )
    return events, event_discussions


RESIDUAL_CLUSTER_LABEL = -1


def apply_event_quality_gate(
    labels: pd.Series,
    discussions: pd.DataFrame,
    messages: pd.DataFrame,
    discussion_messages: pd.DataFrame,
    *,
    min_messages: int = DEFAULT_MIN_EVENT_MESSAGES,
    min_authors: int = DEFAULT_MIN_EVENT_AUTHORS,
) -> pd.Series:
    """Слить в остаточную корзину кластеры, которые инфоповодом не являются.

    Планка та же, что у восстановления сюжетов Brand Analytics: инфоповод — это
    когда о чём-то пишут разные люди, а не когда один автор опубликовал что-то
    один раз. Без неё выгрузка Медиалогии за один день давала 1212 «инфоповодов»
    на 2234 сообщения, 72% из них — из единственного сообщения. Списком на
    тысячу строк пользоваться нельзя.

    Авторы считаются по сообщениям, а не суммированием author_count обсуждений:
    один и тот же автор в двух обсуждениях кластера — всё ещё один автор.
    """
    if labels is None or len(labels) == 0:
        return labels

    discussion_to_label = pd.Series(
        list(labels), index=list(discussions.loc[labels.index, "discussion_id"])
    )
    link = discussion_messages.copy()
    link["_label"] = link["discussion_id"].map(discussion_to_label)
    link = link[link["_label"].notna()]
    if link.empty:
        return labels

    author_column = "author" if "author" in messages.columns else None
    if author_column:
        authors = messages.set_index("message_id")[author_column].astype(str).str.strip()
        link["_author"] = link["message_id"].map(authors).fillna("")
    else:
        link["_author"] = ""

    stats = link.groupby("_label").agg(
        messages=("message_id", "nunique"),
        authors=("_author", lambda s: s[s != ""].nunique()),
    )
    # Автор не указан ни у кого — судить по авторам нечем, остаётся объём.
    if int(stats["authors"].sum()) == 0:
        weak = stats.index[stats["messages"] < min_messages]
    else:
        weak = stats.index[
            (stats["messages"] < min_messages) | (stats["authors"] < min_authors)
        ]
    if not len(weak):
        return labels
    return labels.where(~labels.isin(set(weak)), RESIDUAL_CLUSTER_LABEL)


def make_events(
    discussions: pd.DataFrame,
    labels: pd.Series,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = discussions.copy()
    d["cluster_label"] = labels.values
    d["event_id"] = d["cluster_label"].apply(
        lambda x: "e_residual" if int(x) == RESIDUAL_CLUSTER_LABEL else f"e_{int(x):05d}"
    )

    event_discussions = d[["event_id", "discussion_id"]].copy()

    rows = []
    for event_id, group in d.groupby("event_id", sort=True):
        is_residual = event_id == "e_residual"
        tag = main_tag(group["main_tags"])
        # Самый частый заголовок кластера. Если публикации кластера озаглавлены
        # по-разному, берётся повторяющийся: он и описывает общий сюжет.
        cluster_titles = [
            normalize_spaces(value)
            for value in group.get("title", pd.Series(dtype=str)).fillna("").astype(str)
            if normalize_spaces(value)
        ]
        cluster_title = (
            Counter(cluster_titles).most_common(1)[0][0] if cluster_titles else ""
        )
        keywords = top_keywords(
            group["discussion_text"].fillna("").astype(str), top_n=7
        )
        phrases = top_phrases(group["discussion_text"].fillna("").astype(str), top_n=5)
        microtopic_counter = Counter(
            group.get("microtopic", pd.Series(["other"])).fillna("other").astype(str)
        )
        microtopic = (
            microtopic_counter.most_common(1)[0][0] if microtopic_counter else "other"
        )

        source_main_topic_counter = Counter(
            normalize_spaces(x)
            for x in group.get("source_main_topic", pd.Series(dtype=str))
            .fillna("")
            .astype(str)
            if normalize_spaces(x)
        )
        source_main_topic = (
            source_main_topic_counter.most_common(1)[0][0]
            if source_main_topic_counter
            else ""
        )
        source_topic_values = []
        for value in (
            group.get("source_topics", pd.Series(dtype=str)).fillna("").astype(str)
        ):
            for topic in split_source_topics(value):
                if topic not in source_topic_values:
                    source_topic_values.append(topic)

        start = pd.to_datetime(group["start_date"], errors="coerce").min()
        end = pd.to_datetime(group["end_date"], errors="coerce").max()
        msg_count = int(group["message_count"].sum())
        discussion_count = int(group["discussion_id"].nunique())
        chat_count = (
            int(group["chat_id"].replace("", np.nan).nunique())
            if "chat_id" in group
            else 0
        )
        author_count = (
            int(group["author_count"].sum()) if "author_count" in group else 0
        )
        negative_count = (
            int(group["negative_count"].sum()) if "negative_count" in group else 0
        )
        toxic_count = int(group["toxic_count"].sum()) if "toxic_count" in group else 0

        all_tags = sorted(tag_set_from_series(group["main_tags"]))
        tag_weight = max([CRITICAL_TAG_WEIGHTS.get(t, 1.0) for t in all_tags] or [1.0])
        negative_share = negative_count / msg_count if msg_count else 0.0
        toxic_share = toxic_count / msg_count if msg_count else 0.0

        importance_score = (
            math.log1p(msg_count) * 2
            + math.log1p(max(chat_count, 1)) * 2.5
            + math.log1p(max(author_count, 1)) * 0.8
            + negative_share * 4
            + toxic_share * 2
            + tag_weight
        )

        rows.append(
            {
                "event_id": event_id,
                "event_title": (
                    RESIDUAL_STORY_TITLE
                    if is_residual
                    else build_title(
                        tag,
                        keywords,
                        all_tags,
                        microtopic=microtopic,
                        phrases=phrases,
                        # Заголовок публикации — готовая формулировка события и
                        # потому лучше тега: тег повторяется на десятках не
                        # связанных публикаций, и список превращается в «пари»,
                        # «пари», «винлайн».
                        source_main_topic=source_main_topic or cluster_title,
                        source_topics="; ".join(source_topic_values),
                    )
                ),
                "event_summary": summarize_event(
                    group,
                    source_main_topic or MICROTOPIC_TITLES.get(microtopic, tag),
                    keywords,
                    phrases,
                ),
                "main_tag": tag,
                "microtopic": microtopic,
                "main_tags": "|".join(all_tags),
                "source_main_topic": source_main_topic,
                "source_topics": "; ".join(source_topic_values),
                "keywords": "|".join(keywords),
                "key_phrases": "|".join(phrases),
                "start_date": start,
                "end_date": end,
                "discussion_count": discussion_count,
                "message_count": msg_count,
                "chat_count": chat_count,
                "author_count": author_count,
                "negative_count": negative_count,
                "toxic_count": toxic_count,
                "negative_share": round(negative_share, 4),
                "toxic_share": round(toxic_share, 4),
                "importance_score": round(float(importance_score), 2),
                "status": "новый",
                "is_hidden": False,
                "is_residual": is_residual,
            }
        )

    # Пустой список строк даёт DataFrame без колонок, и сортировка по ним
    # падала бы KeyError'ом — так выгрузка, из которой не разобралось ни одного
    # сообщения, роняла обработку вместо понятного «данных нет».
    # make_events_from_source_stories выше уже защищён так же.
    events = pd.DataFrame(rows)
    if not events.empty:
        # Остаточная корзина сортируется последней, а не по важности: в ней
        # сотни сообщений, и вес у неё закономерно наибольший — но это объём
        # мешка, а не значимость события.
        events = events.sort_values(
            ["is_residual", "importance_score", "message_count"],
            ascending=[True, False, False],
        )
    return events, event_discussions
