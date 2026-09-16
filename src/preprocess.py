"""
Preprocess Telegram chat CSV into normalized analytical tables.

Usage:
    python src/preprocess.py --input data/chats.csv --output data/processed

До версии, где этот файл занимал 2237 строк, здесь лежала вся логика
конвейера — от чистки текста до сборки инфоповодов. Модуль распилен на
services/{text_cleaning,tag_parsing,microtopics,message_normalize,
discussion_build,discussion_titles,clustering,event_assembly}.py по
границам ответственности; этот файл — только оркестрация шагов
(build_processed_tables и его обёртки) и ре-экспорт для обратной
совместимости: внешний код (services/ingest.py, scripts/loadtest_pipeline.py,
tests/) продолжает делать `from preprocess import X` теми же именами.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from io_utils import read_source_csv, write_table, write_manifest
from services.story_recovery import (
    DEFAULT_MIN_AUTHORS as DEFAULT_MIN_EVENT_AUTHORS,
    DEFAULT_MIN_MESSAGES as DEFAULT_MIN_EVENT_MESSAGES,
    DEFAULT_SIMILARITY as DEFAULT_STORY_SIMILARITY,
    recover_stories,
)

# Ре-экспорт: имена ниже раньше были определены прямо здесь. Внешний код
# импортирует их как `from preprocess import <имя>` — при распиле это должно
# продолжать работать без единой правки на стороне вызывающих модулей.
from services.text_cleaning import (
    stable_hash,
    normalize_spaces,
    get_text_series,
    pick_first_non_empty,
    chat_key_from_link,
    clean_text,
    parse_datetime,
)
from services.tag_parsing import (
    detect_tag_columns,
    split_tag_text,
    split_source_topics,
    normalize_relevant,
    source_topic_bucket_value,
    GENERIC_EMPTY_LABELS,
    normalize_label,
    unique_labels,
    label_microtopic,
    row_tags,
    infer_display_tags,
    tag_set,
    tag_signature,
    main_tag,
    tag_set_from_series,
)
from services.microtopics import regex_any, classify_microtopic
from services.message_normalize import is_brand_analytics_dataframe, normalize_messages
from services.discussion_build import (
    topic_bucket_for,
    should_start_new_discussion,
    _DISCUSSION_MESSAGE_COLUMNS,
    make_discussions,
)
from services.discussion_titles import build_title, summarize_event
from services.clustering import (
    split_labels_by_fixed_time_window,
    dynamic_threshold,
    cluster_sparse_greedy,
    cluster_discussions_tfidf,
    cluster_discussions_embeddings,
    refine_labels_by_tag,
    split_labels_by_time_gap,
)
from services.event_assembly import (
    make_events_from_source_stories,
    RESIDUAL_CLUSTER_LABEL,
    apply_event_quality_gate,
    make_events,
)

__all__ = [
    "stable_hash", "normalize_spaces", "get_text_series", "pick_first_non_empty",
    "chat_key_from_link", "clean_text", "parse_datetime",
    "detect_tag_columns", "split_tag_text", "split_source_topics",
    "normalize_relevant", "source_topic_bucket_value", "GENERIC_EMPTY_LABELS",
    "normalize_label", "unique_labels", "label_microtopic", "row_tags",
    "infer_display_tags", "tag_set", "tag_signature", "main_tag",
    "tag_set_from_series",
    "regex_any", "classify_microtopic",
    "is_brand_analytics_dataframe", "normalize_messages",
    "topic_bucket_for", "should_start_new_discussion", "make_discussions",
    "build_title", "summarize_event",
    "split_labels_by_fixed_time_window", "dynamic_threshold",
    "cluster_sparse_greedy", "cluster_discussions_tfidf",
    "cluster_discussions_embeddings", "refine_labels_by_tag",
    "split_labels_by_time_gap",
    "make_events_from_source_stories", "RESIDUAL_CLUSTER_LABEL",
    "apply_event_quality_gate", "make_events",
    "clear_processed_tables", "build_processed_tables", "run_preprocess",
    "run_preprocess_from_dataframe", "main",
]

def clear_processed_tables(output: Path) -> None:
    """Remove stale generated tables before writing a new dataset."""
    output.mkdir(parents=True, exist_ok=True)
    for name in [
        "messages",
        "message_tags",
        "discussions",
        "discussion_messages",
        "events",
        "event_discussions",
    ]:
        for ext in ["csv", "parquet"]:
            path = output / f"{name}.{ext}"
            if path.exists():
                path.unlink()
    manifest = output / "manifest.json"
    if manifest.exists():
        manifest.unlink()


def build_processed_tables(
    raw: pd.DataFrame,
    output: str | Path = "data/processed",
    source_file: str = "uploaded_csv",
    window_minutes: int = 60,
    cluster_method: str = "tfidf",
    similarity_threshold: float = 0.28,
    event_gap_hours: float = 3.0,
    event_window_hours: float = 16.0,
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    *,
    story_similarity: float = DEFAULT_STORY_SIMILARITY,
    story_min_authors: int = DEFAULT_MIN_EVENT_AUTHORS,
    story_min_messages: int = DEFAULT_MIN_EVENT_MESSAGES,
) -> dict:
    """Build all generated dashboard tables from a raw dataframe and return manifest."""
    output = Path(output)
    clear_processed_tables(output)

    tag_cols = detect_tag_columns(raw)
    is_brand_analytics = is_brand_analytics_dataframe(raw)

    messages, message_tags = normalize_messages(raw, tag_cols)

    if is_brand_analytics:
        # Сюжетами Brand Analytics размечена примерно четверть сообщений;
        # остальное платформа сваливала в один псевдоповод «Без сюжета», который
        # перевешивал любой настоящий инфоповод. Досчитываем сюжеты до сборки
        # обсуждений: дальше по конвейеру событие собирается именно по ним.
        recovered = recover_stories(
            messages,
            min_authors=story_min_authors,
            min_messages=story_min_messages,
            similarity=story_similarity,
        )
        messages["source_main_topic"] = recovered["story"]
        messages["story_origin"] = recovered["story_origin"]
        # Сюжет из выгрузки авторитетнее любого досчёта, поэтому список тем
        # обновляется только там, где он пустовал.
        if "source_topics" in messages.columns:
            missing_topics = messages["source_topics"].fillna("").astype(str).str.strip() == ""
            messages.loc[missing_topics, "source_topics"] = messages.loc[
                missing_topics, "source_main_topic"
            ]

    discussions, discussion_messages = make_discussions(
        messages, window_minutes=window_minutes
    )

    if is_brand_analytics:
        # Brand Analytics mode: events are source stories (`Сюжет`), not tag/text
        # clusters. Tags from columns after `Обработано` stay available in
        # messages/message_tags for analytics and filtering.
        events, event_discussions = make_events_from_source_stories(discussions)
        cluster_method_used = "brand_analytics_story"
    else:
        # Filter empty discussions from clustering, but preserve them as singleton events.
        clusterable = discussions[
            discussions["discussion_text"].fillna("").str.len() > 10
        ].copy()
        non_clusterable = discussions.drop(clusterable.index).copy()

        if cluster_method == "none":
            labels = pd.Series(range(len(clusterable)), index=clusterable.index)
        elif cluster_method == "embeddings":
            labels = cluster_discussions_embeddings(
                clusterable,
                similarity_threshold=similarity_threshold,
                model_name=embedding_model,
            )
        else:
            labels = cluster_discussions_tfidf(
                clusterable,
                similarity_threshold=similarity_threshold,
                max_gap_hours=event_gap_hours,
                max_event_span_hours=event_window_hours,
            )

        if len(clusterable):
            labels = refine_labels_by_tag(labels, clusterable)
            labels = split_labels_by_time_gap(
                labels, clusterable, max_gap_hours=event_gap_hours
            )
            labels = split_labels_by_fixed_time_window(
                labels, clusterable, window_hours=event_window_hours
            )

        if len(non_clusterable):
            start_label = int(labels.max()) + 1 if len(labels) else 0
            singleton_labels = pd.Series(
                range(start_label, start_label + len(non_clusterable)),
                index=non_clusterable.index,
            )
            all_discussions = pd.concat(
                [clusterable, non_clusterable], axis=0
            ).sort_index()
            all_labels = pd.concat([labels, singleton_labels]).loc[
                all_discussions.index
            ]
        else:
            all_discussions = clusterable
            all_labels = labels

        # Планка качества та же, что у сюжетов Brand Analytics: без неё
        # выгрузка Медиалогии за один день давала 1212 «инфоповодов» на 2234
        # сообщения, три четверти из них — из одного сообщения.
        all_labels = apply_event_quality_gate(
            all_labels,
            all_discussions,
            messages,
            discussion_messages,
            min_messages=story_min_messages,
            min_authors=story_min_authors,
        )
        events, event_discussions = make_events(all_discussions, all_labels)
        cluster_method_used = cluster_method

    paths = {
        "messages": str(write_table(messages, output, "messages")),
        "message_tags": str(write_table(message_tags, output, "message_tags")),
        "discussions": str(write_table(discussions, output, "discussions")),
        "discussion_messages": str(
            write_table(discussion_messages, output, "discussion_messages")
        ),
        "events": str(write_table(events, output, "events")),
        "event_discussions": str(
            write_table(event_discussions, output, "event_discussions")
        ),
    }

    manifest = {
        "source_file": source_file,
        "rows_source": int(len(raw)),
        "rows_messages": int(len(messages)),
        "rows_discussions": int(len(discussions)),
        "rows_events": int(len(events)),
        "tag_columns": tag_cols,
        "window_minutes": window_minutes,
        "cluster_method": cluster_method_used,
        "event_source": (
            "brand_analytics_story" if is_brand_analytics else "algorithmic_cluster"
        ),
        "similarity_threshold": similarity_threshold,
        "event_gap_hours": event_gap_hours,
        "event_window_hours": event_window_hours,
        "story_similarity": story_similarity,
        "story_min_authors": story_min_authors,
        "story_min_messages": story_min_messages,
        "paths": paths,
    }
    write_manifest(output, manifest)
    return manifest


def run_preprocess(
    input_path: str | Path,
    output: str | Path = "data/processed",
    window_minutes: int = 60,
    cluster_method: str = "tfidf",
    similarity_threshold: float = 0.28,
    event_gap_hours: float = 3.0,
    event_window_hours: float = 16.0,
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
) -> dict:
    """Read a source CSV, generate tables and return manifest. Usable from Streamlit."""
    input_path = Path(input_path)
    raw = read_source_csv(input_path)
    return build_processed_tables(
        raw=raw,
        output=output,
        source_file=str(input_path.resolve()),
        window_minutes=window_minutes,
        cluster_method=cluster_method,
        similarity_threshold=similarity_threshold,
        event_gap_hours=event_gap_hours,
        event_window_hours=event_window_hours,
        embedding_model=embedding_model,
    )


def run_preprocess_from_dataframe(
    raw: pd.DataFrame,
    output: str | Path = "data/processed",
    source_file: str = "uploaded_csv",
    window_minutes: int = 60,
    cluster_method: str = "tfidf",
    similarity_threshold: float = 0.28,
    event_gap_hours: float = 3.0,
    event_window_hours: float = 16.0,
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    *,
    story_similarity: float = DEFAULT_STORY_SIMILARITY,
    story_min_authors: int = DEFAULT_MIN_EVENT_AUTHORS,
    story_min_messages: int = DEFAULT_MIN_EVENT_MESSAGES,
) -> dict:
    """Generate tables from an already loaded dataframe. Usable for combining uploads."""
    return build_processed_tables(
        raw=raw,
        output=output,
        source_file=source_file,
        window_minutes=window_minutes,
        cluster_method=cluster_method,
        similarity_threshold=similarity_threshold,
        event_gap_hours=event_gap_hours,
        event_window_hours=event_window_hours,
        embedding_model=embedding_model,
        story_similarity=story_similarity,
        story_min_authors=story_min_authors,
        story_min_messages=story_min_messages,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to source CSV")
    parser.add_argument("--output", default="data/processed", help="Output directory")
    parser.add_argument("--window-minutes", type=int, default=60)
    parser.add_argument(
        "--cluster-method", choices=["tfidf", "embeddings", "none"], default="tfidf"
    )
    parser.add_argument("--similarity-threshold", type=float, default=0.28)
    parser.add_argument(
        "--event-gap-hours",
        type=float,
        default=3.0,
        help="Split clusters into separate events when the time gap is larger than this value",
    )
    parser.add_argument(
        "--event-window-hours",
        type=float,
        default=16.0,
        help="Additionally limit one event to a fixed time span",
    )
    parser.add_argument(
        "--embedding-model",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    args = parser.parse_args()

    manifest = run_preprocess(
        input_path=args.input,
        output=args.output,
        window_minutes=args.window_minutes,
        cluster_method=args.cluster_method,
        similarity_threshold=args.similarity_threshold,
        event_gap_hours=args.event_gap_hours,
        event_window_hours=args.event_window_hours,
        embedding_model=args.embedding_model,
    )

    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
