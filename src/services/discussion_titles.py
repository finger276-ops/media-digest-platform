"""Заголовок и краткое описание инфоповода из текстов обсуждения.

Вынесено из preprocess.py при распиле монолита. Не путать с
services/event_titles.py — там про автоматическую склейку похожих заголовков
инфоповодов в UI, а не про построение заголовка при сборке.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from settings import MICROTOPIC_TITLES, TITLE_RULES

from .tag_parsing import normalize_label


def build_title(
    tag: str,
    keywords: list[str],
    all_tags: Iterable[str] | None = None,
    microtopic: str = "other",
    phrases: list[str] | None = None,
    source_main_topic: str = "",
    source_topics: str = "",
) -> str:
    """Build a human-readable event title for any project domain."""
    all_tags = {normalize_label(x) for x in (all_tags or []) if normalize_label(x)}
    kw = set(keywords)
    phrases = phrases or []
    tag = normalize_label(tag) or "Прочие обсуждения"
    source_main_topic = normalize_label(source_main_topic)

    # Source-provided themes from Brand Analytics/Mediologia/etc. are usually
    # better than any generic dictionary. Prefer them for non-empty values.
    if source_main_topic:
        return source_main_topic

    for rule in TITLE_RULES:
        if all_tags & set(rule.get("tags", set())) and (
            not rule.get("keywords") or kw & set(rule.get("keywords", set()))
        ):
            return rule["title"]

    # Микротемы выручают, когда в файле нет человеческой разметки.
    if microtopic in MICROTOPIC_TITLES and microtopic not in {"other", "general"}:
        title = MICROTOPIC_TITLES[microtopic]
        if (
            tag
            and tag not in {"Прочие обсуждения", "Общие обсуждения", title}
            and not tag.startswith("label_")
        ):
            return tag
        return title

    if tag and tag not in {"Без тега", "Прочие обсуждения", "Общие обсуждения"}:
        return tag

    if phrases:
        phrase = normalize_label(phrases[0])
        if phrase:
            return phrase[:1].upper() + phrase[1:]
    if keywords:
        return f"Обсуждение: {', '.join(keywords[:3])}"
    return "Прочие обсуждения"


def summarize_event(
    group: pd.DataFrame, tag: str, keywords: list[str], phrases: list[str]
) -> str:
    """Fallback thesis-style description for processed files.

    The dashboard recalculates a richer description from the final message set,
    but this keeps regenerated events.csv readable even outside Streamlit.
    """
    details = [x for x in (phrases[:4] or keywords[:6]) if str(x).strip()]
    if details:
        return "В теме обсуждались: " + "; ".join(details[:6]) + "."
    return f"В теме обсуждались сообщения по направлению «{tag}»."
