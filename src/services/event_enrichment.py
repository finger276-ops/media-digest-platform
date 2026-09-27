# -*- coding: utf-8 -*-
"""Обогащение сообщений связями с инфоповодами и агрегация инфоповодов.

Framework-independent: строит из сырых events/discussions/messages таблицы,
которые дальше использует дашборд (event_id/event_title на каждом сообщении,
сгруппированные по нормализованному заголовку инфоповоды с авто-описанием).
"""

from __future__ import annotations

import pandas as pd

from .event_titles import normalize_event_title
from .story_recovery import is_residual_title
from .tag_parsing import PLATFORM_RUBRIC_TAG_KEYS


def enrich_messages(
    messages: pd.DataFrame,
    event_discussions: pd.DataFrame,
    discussion_messages: pd.DataFrame,
    events: pd.DataFrame,
) -> pd.DataFrame:
    """Attach event ids/titles to messages safely.

    Some project profiles, especially Brand Analytics story-based imports, may
    have generated event rows but no discussion/event link table in older
    periods. The previous version assumed that the merge through
    discussion_messages/event_discussions always created an `event_id` column and
    crashed with KeyError when it did not.
    """
    if messages is None or messages.empty:
        return messages if isinstance(messages, pd.DataFrame) else pd.DataFrame()

    out = messages.copy()
    if "message_id" not in out.columns:
        out["message_id"] = out.index.astype(str)
    out["message_id"] = out["message_id"].fillna("").astype(str)

    # 1) Preferred path: message -> discussion -> event links.
    if (
        isinstance(event_discussions, pd.DataFrame)
        and isinstance(discussion_messages, pd.DataFrame)
        and not event_discussions.empty
        and not discussion_messages.empty
        and "discussion_id" in event_discussions.columns
        and "discussion_id" in discussion_messages.columns
    ):
        link = discussion_messages.merge(
            event_discussions, on="discussion_id", how="left"
        )
        if "message_id" in link.columns and "event_id" in link.columns:
            msg_event = (
                link[["message_id", "event_id"]]
                .dropna(subset=["message_id"])
                .drop_duplicates("message_id")
            )
            msg_event["message_id"] = msg_event["message_id"].fillna("").astype(str)
            out = out.merge(
                msg_event, on="message_id", how="left", suffixes=("", "_linked")
            )
            if "event_id_linked" in out.columns:
                if "event_id" not in out.columns:
                    out["event_id"] = out["event_id_linked"]
                else:
                    out["event_id"] = out["event_id"].fillna(out["event_id_linked"])
                out = out.drop(columns=["event_id_linked"])

    # 2) Fallback for story-based BA periods: map source topic/story to event.
    if "event_id" not in out.columns:
        out["event_id"] = ""
    out["event_id"] = out["event_id"].fillna("").astype(str)

    if (
        isinstance(events, pd.DataFrame)
        and not events.empty
        and "event_id" in events.columns
    ):
        events_work = events.copy()
        events_work["event_id"] = events_work["event_id"].fillna("").astype(str)

        needs_fallback = out["event_id"].str.strip().eq("").all()
        if needs_fallback:
            topic_map: dict[str, str] = {}
            if "source_main_topic" in events_work.columns:
                for _, row in events_work.dropna(subset=["event_id"]).iterrows():
                    key = str(row.get("source_main_topic") or "").strip().lower()
                    if key and key not in topic_map:
                        topic_map[key] = str(row.get("event_id"))
            if "event_title" in events_work.columns:
                for _, row in events_work.dropna(subset=["event_id"]).iterrows():
                    key = str(row.get("event_title") or "").strip().lower()
                    if key and key not in topic_map:
                        topic_map[key] = str(row.get("event_id"))

            if topic_map:
                source_col = None
                for candidate in ["source_main_topic", "source_topics", "event_title"]:
                    if candidate in out.columns:
                        source_col = candidate
                        break
                if source_col:
                    keys = (
                        out[source_col]
                        .fillna("")
                        .astype(str)
                        .str.split(";")
                        .str[0]
                        .str.strip()
                        .str.lower()
                    )
                    out["event_id"] = keys.map(topic_map).fillna(out["event_id"])

        # Add/fill event title without requiring a merge key to exist.
        if "event_title" in events_work.columns:
            title_map = (
                events_work.drop_duplicates("event_id")
                .set_index("event_id")["event_title"]
                .fillna("")
                .astype(str)
                .to_dict()
            )
            mapped_titles = (
                out["event_id"].fillna("").astype(str).map(title_map).fillna("")
            )
            if "event_title" not in out.columns:
                out["event_title"] = mapped_titles
            else:
                current = out["event_title"].fillna("").astype(str)
                out["event_title"] = current.where(
                    current.str.strip().ne(""), mapped_titles
                )

    if "event_title" not in out.columns:
        out["event_title"] = ""
    return out


def pick_event_description(group: pd.DataFrame) -> str:
    """Описание инфоповода — только то, что написал аналитик.

    Раньше без ручного описания платформа собирала своё: «В теме обсуждались:
    цены, стоимость и условия; качество продукта…» — рубриками, подобранными
    под одного заказчика. Оно попадало в таблицу и карточку, а в форме правки
    подставлялось в поле и после «Сохранить» становилось «ручным». Пустое поле
    честнее: карточка инфоповода и так пишет, сколько в нём сообщений.
    """
    for col in ["display_description", "manual_description", "event_description"]:
        if col in group.columns:
            vals = [
                str(x).strip() for x in group[col].fillna("").tolist() if str(x).strip()
            ]
            if vals:
                return vals[0]
    return ""


def _numbers(group: pd.DataFrame, column: str) -> pd.Series:
    """Числовая колонка группы, а ноль — когда колонки нет.

    Здесь был скрытый отказ: `group.get(column, 0)` при отсутствии колонки
    возвращает скаляр, у которого потом вызывался `.fillna` — защита выглядела
    защитой, но роняла агрегацию с AttributeError на любом кадре без
    необязательной колонки.
    """
    if column not in group.columns:
        return pd.Series([0] * len(group), index=group.index, dtype="float64")
    return pd.to_numeric(group[column], errors="coerce").fillna(0)


def _dates(group: pd.DataFrame, column: str) -> pd.Series:
    """Колонка дат группы; пустая — когда колонки нет."""
    if column not in group.columns:
        return pd.Series([pd.NaT] * len(group), index=group.index, dtype="datetime64[ns]")
    return pd.to_datetime(group[column], errors="coerce")


def aggregate_events(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    df = events.copy()
    df["title"] = (
        df.get("event_title", "")
        .fillna("Без названия")
        .astype(str)
        .replace("", "Без названия")
    )
    # Нормализация вместо простого lower(): регистр, ё/е, кавычки, тире,
    # многоточия и пробелы внутри чисел не должны разводить один сюжет по
    # разным инфоповодам. Смысловая склейка близких заголовков идёт отдельным
    # шагом (merge_similar_events), чтобы её можно было выключить и проверить.
    df["group_key"] = df["title"].map(normalize_event_title)
    rows = []
    for key, group in df.groupby("group_key", dropna=False):
        variants = list(dict.fromkeys(group["title"].astype(str).tolist()))
        row = {
            "group_key": key,
            "title": variants[0] if variants else "Без названия",
            "title_variants": variants,
            "merged_titles": max(0, len(variants) - 1),
            "description": pick_event_description(group),
            # Рубрики платформы в теги инфоповода не идут — как и в теги
            # сообщений (services.tag_compute.clean_display_tags).
            "tags": " | ".join(
                sorted(
                    tag
                    for tag in set(
                        "|".join(
                            group.get("main_tags", pd.Series(dtype=str))
                            .fillna("")
                            .astype(str)
                        ).split("|")
                    )
                    - {""}
                    if tag.strip().lower().replace("ё", "е")
                    not in PLATFORM_RUBRIC_TAG_KEYS
                )
            ),
            "start_date": _dates(group, "start_date").min(),
            "end_date": _dates(group, "end_date").max(),
            "message_count": int(_numbers(group, "message_count").sum()),
            "chat_count": int(_numbers(group, "chat_count").sum()),
            "negative_count": int(_numbers(group, "negative_count").sum()),
            "importance_score": float(_numbers(group, "importance_score").max()),
            "event_ids": (
                list(group["event_id"].astype(str))
                if "event_id" in group.columns
                else []
            ),
            # Остаточная корзина — не инфоповод, и в рейтинге важности ей не
            # место. Признак должен пережить агрегацию, иначе дашборд снова
            # поставит мешок из сотен сообщений первым.
            "is_residual": bool(
                group.get("is_residual", pd.Series([False] * len(group))).fillna(False).any()
            )
            or is_residual_title(variants[0] if variants else ""),
        }
        row["negative_share"] = (
            row["negative_count"] / row["message_count"] if row["message_count"] else 0
        )
        rows.append(row)
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(
            ["is_residual", "importance_score", "message_count"],
            ascending=[True, False, False],
        )
    return out
