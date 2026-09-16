"""Кластеризация обсуждений в инфоповоды по тексту (алгоритмическая ветка).

Вынесено из preprocess.py при распиле монолита. Применяется, когда в выгрузке
нет готовой разметки сюжетов Brand Analytics (см. services/message_normalize.py
is_brand_analytics_dataframe) — тогда инфоповоды собираются кластеризацией
текстов обсуждений.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from services.ru_text import tokenize_ru

from .discussion_build import topic_bucket_for
from .tag_parsing import main_tag, source_topic_bucket_value


def split_labels_by_fixed_time_window(
    labels: pd.Series,
    discussions: pd.DataFrame,
    window_hours: float = 12.0,
) -> pd.Series:
    """
    Дополнительное дробление широких кластеров на временные волны.
    Это защищает от ситуации, когда большой тег вроде «яндекс» или «Коэффициент»
    склеивает обсуждения за несколько дней в один инфоповод.
    """
    if window_hours <= 0 or len(discussions) == 0:
        return labels.astype(int)

    d = discussions.copy()
    d["_label"] = labels.loc[d.index].astype(int)
    d["_start"] = pd.to_datetime(d["start_date"], errors="coerce")
    fallback = pd.Timestamp("1970-01-01")
    d["_bucket"] = d["_start"].fillna(fallback).astype("int64") // int(
        pd.Timedelta(hours=window_hours).value
    )

    mapping = {}
    next_label = 0
    result = pd.Series(index=d.index, dtype=int)
    for idx, row in d.iterrows():
        key = (int(row["_label"]), int(row["_bucket"]))
        if key not in mapping:
            mapping[key] = next_label
            next_label += 1
        result.loc[idx] = mapping[key]
    return result.astype(int)


def dynamic_threshold(bucket: str, base: float) -> float:
    """Stricter threshold for broad/noisy buckets."""
    if (
        "general_yandex" in bucket
        or "::general" in bucket
        or bucket.endswith("::other")
    ):
        return min(0.72, base + 0.12)
    if "coeff_priority" in bucket:
        return min(0.68, base + 0.07)
    if "app_bug" in bucket or "app_orders" in bucket:
        return min(0.66, base + 0.05)
    return base


def cluster_sparse_greedy(
    group: pd.DataFrame,
    threshold: float,
    max_gap_hours: float,
    max_event_span_hours: float,
    max_features: int,
) -> pd.Series:
    """
    Fast clustering inside a narrow topic+time bucket.

    We still use connected components, but only after splitting by microtopic and
    fixed time bucket. This removes the worst source of false merges: long chains
    across broad tags over several days.
    """
    from scipy.sparse.csgraph import connected_components
    from sklearn.feature_extraction.text import TfidfVectorizer

    if len(group) == 0:
        return pd.Series([], dtype=int)
    if len(group) == 1:
        return pd.Series([0], index=group.index, dtype=int)

    texts = group["discussion_text"].fillna("").astype(str).tolist()
    min_df = 1 if len(group) < 12 else 2
    vectorizer = TfidfVectorizer(
        tokenizer=tokenize_ru,
        token_pattern=None,
        ngram_range=(1, 2),
        min_df=min_df,
        max_df=0.72,
        max_features=max_features,
        sublinear_tf=True,
    )
    try:
        X = vectorizer.fit_transform(texts)
    except ValueError:
        return pd.Series(range(len(group)), index=group.index, dtype=int)

    nonzero_mask = np.asarray(X.getnnz(axis=1) > 0).ravel()
    result = pd.Series(index=group.index, dtype=int)
    if int(nonzero_mask.sum()) < 2:
        result.loc[:] = range(len(group))
        return result.astype(int)

    Xn = X[nonzero_mask]
    sim = Xn @ Xn.T
    sim.setdiag(0)
    sim.data[sim.data < threshold] = 0
    sim.eliminate_zeros()

    _, labels = connected_components(sim, directed=False, return_labels=True)
    result.iloc[np.where(nonzero_mask)[0]] = labels

    next_label = int(labels.max()) + 1 if len(labels) else 0
    for pos in np.where(~nonzero_mask)[0]:
        result.iloc[pos] = next_label
        next_label += 1

    return result.astype(int)


def cluster_discussions_tfidf(
    discussions: pd.DataFrame,
    similarity_threshold: float = 0.38,
    max_features: int = 6000,
    max_gap_hours: float = 0.75,
    max_event_span_hours: float = 8.0,
) -> pd.Series:
    texts = discussions["discussion_text"].fillna("").astype(str)
    if len(texts) == 0:
        return pd.Series([], dtype=int)
    if len(texts) == 1:
        return pd.Series([0], index=discussions.index)

    d = discussions.copy()
    if "topic_bucket" not in d.columns:
        d["topic_bucket"] = d.apply(topic_bucket_for, axis=1)

    # Narrow time bucket before lexical clustering. This is the main protection
    # against putting several independent waves into one information event.
    d["_start"] = pd.to_datetime(d["start_date"], errors="coerce")
    fallback = pd.Timestamp("1970-01-01")
    bucket_hours = max(1.0, float(max_event_span_hours))
    d["_time_bucket"] = d["_start"].fillna(fallback).astype("int64") // int(
        pd.Timedelta(hours=bucket_hours).value
    )
    d["_cluster_bucket"] = (
        d["topic_bucket"].astype(str) + "::t" + d["_time_bucket"].astype(str)
    )

    result = pd.Series(index=d.index, dtype=int)
    next_label = 0

    # Cluster independently inside narrow topic + time buckets.
    for bucket, group in d.groupby("_cluster_bucket", sort=False):
        topic_part = str(group["topic_bucket"].iloc[0]) if len(group) else str(bucket)
        bucket_threshold = dynamic_threshold(topic_part, similarity_threshold)
        local_labels = cluster_sparse_greedy(
            group,
            threshold=bucket_threshold,
            max_gap_hours=max_gap_hours,
            max_event_span_hours=max_event_span_hours,
            max_features=max_features,
        )
        unique_local = {
            int(v): i + next_label for i, v in enumerate(sorted(local_labels.unique()))
        }
        result.loc[group.index] = local_labels.map(unique_local)
        next_label += len(unique_local)

    return result.astype(int)


def cluster_discussions_embeddings(
    discussions: pd.DataFrame,
    similarity_threshold: float = 0.68,
    model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
) -> pd.Series:
    from sentence_transformers import SentenceTransformer
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.preprocessing import normalize

    texts = discussions["discussion_text"].fillna("").astype(str).tolist()
    if len(texts) == 0:
        return pd.Series([], dtype=int)
    if len(texts) == 1:
        return pd.Series([0], index=discussions.index)

    model = SentenceTransformer(model_name)
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)
    embeddings = normalize(embeddings)

    distance_threshold = max(0.01, min(0.99, 1.0 - similarity_threshold))
    kwargs = {
        "n_clusters": None,
        "distance_threshold": distance_threshold,
        "linkage": "average",
        "compute_full_tree": True,
    }
    try:
        clustering = AgglomerativeClustering(metric="cosine", **kwargs)
    except TypeError:
        clustering = AgglomerativeClustering(affinity="cosine", **kwargs)
    labels = clustering.fit_predict(embeddings)
    return pd.Series(labels, index=discussions.index)


def refine_labels_by_tag(labels: pd.Series, discussions: pd.DataFrame) -> pd.Series:
    """
    Prevent obviously unrelated broad tags from being merged too aggressively.
    We split every cluster by its main tag.
    """
    new_labels = []
    label_map = {}
    next_id = 0
    for i, row in discussions.iterrows():
        raw_label = int(labels.loc[i])
        mt = main_tag([row.get("main_tags", "")])
        source_topic = source_topic_bucket_value(row.get("source_main_topic", ""))
        key = (raw_label, source_topic, mt)
        if key not in label_map:
            label_map[key] = next_id
            next_id += 1
        new_labels.append(label_map[key])
    return pd.Series(new_labels, index=discussions.index)


def split_labels_by_time_gap(
    labels: pd.Series,
    discussions: pd.DataFrame,
    max_gap_hours: float = 12.0,
) -> pd.Series:
    """
    Split broad clusters into separate information events when discussions are far apart in time.
    This is important for live chats: one broad topic can recur for several days, but each wave may be a separate info event.
    """
    d = discussions.copy()
    d["_label"] = labels.loc[d.index].astype(int)
    d["_start"] = pd.to_datetime(d["start_date"], errors="coerce")
    d["_end"] = pd.to_datetime(d["end_date"], errors="coerce")

    new_labels = pd.Series(index=d.index, dtype=int)
    next_label = 0

    for _, group in d.sort_values(["_label", "_start"]).groupby("_label", sort=False):
        current_label = next_label
        next_label += 1
        prev_end = None

        for idx, row in group.iterrows():
            start = row["_start"]
            end = row["_end"]

            if prev_end is not None and pd.notna(start) and pd.notna(prev_end):
                if start - prev_end > pd.Timedelta(hours=max_gap_hours):
                    current_label = next_label
                    next_label += 1

            new_labels.loc[idx] = current_label

            if pd.notna(end):
                prev_end = (
                    max(prev_end, end)
                    if prev_end is not None and pd.notna(prev_end)
                    else end
                )
            elif pd.notna(start):
                prev_end = (
                    max(prev_end, start)
                    if prev_end is not None and pd.notna(prev_end)
                    else start
                )

    return new_labels.astype(int)
