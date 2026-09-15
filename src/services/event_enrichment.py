# -*- coding: utf-8 -*-
"""Обогащение сообщений связями с инфоповодами и агрегация инфоповодов.

Framework-independent: строит из сырых events/discussions/messages таблицы,
которые дальше использует дашборд (event_id/event_title на каждом сообщении,
сгруппированные по нормализованному заголовку инфоповоды с авто-описанием).
"""

from __future__ import annotations

import pandas as pd

from .event_titles import normalize_event_title


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


def build_event_description(group: pd.DataFrame) -> str:
    """Build a neutral, source-agnostic event description."""
    text = " ".join(
        group.get("event_summary", pd.Series(dtype=str)).fillna("").astype(str).tolist()
    )
    tags = " | ".join(
        sorted(
            set(
                "|".join(
                    group.get("main_tags", pd.Series(dtype=str)).fillna("").astype(str)
                ).split("|")
            )
            - {""}
        )
    )
    low = f"{text} {tags}".lower().replace("ё", "е")
    patterns = [
        (
            "проблемы, жалобы и негативный опыт",
            [
                "жалоб",
                "проблем",
                "негатив",
                "ошиб",
                "не работает",
                "плохо",
                "брак",
                "дефект",
            ],
        ),
        (
            "цены, стоимость и условия",
            ["цен", "стоим", "скид", "акци", "тариф", "услов", "дорого", "дешев"],
        ),
        (
            "качество продукта или услуги",
            ["качеств", "материал", "характерист", "свойств", "надежн", "эффектив"],
        ),
        (
            "наличие, поставки и логистика",
            ["достав", "налич", "склад", "постав", "логист", "срок", "отгруз"],
        ),
        (
            "монтаж, применение и эксплуатация",
            [
                "монтаж",
                "установ",
                "примен",
                "использ",
                "эксплуатац",
                "строител",
                "утепл",
                "изоляц",
            ],
        ),
        (
            "документы, сертификаты и требования",
            [
                "сертифик",
                "документ",
                "декларац",
                "гост",
                "снип",
                "требован",
                "стандарт",
            ],
        ),
        (
            "безопасность и риски",
            ["безопас", "пожар", "огне", "горюч", "опасн", "токсич"],
        ),
        (
            "экология и энергоэффективность",
            ["эколог", "энергоэфф", "энергосбереж", "устойчив", "переработ"],
        ),
        ("конкуренты и сравнение", ["конкур", "аналог", "сравнен", "рынок", "бренд"]),
        (
            "клиентский сервис и поддержка",
            ["поддерж", "сервис", "менеджер", "дилер", "магазин", "клиент"],
        ),
    ]
    signals = []
    for label, keys in patterns:
        if any(k in low for k in keys):
            signals.append(label)
    if signals:
        return "В теме обсуждались: " + "; ".join(signals[:5]) + "."
    return (
        f"В теме обсуждались: {tags}."
        if tags
        else "В теме обсуждались связанные сообщения выбранного периода."
    )


def pick_event_description(group: pd.DataFrame) -> str:
    """Return manual description if present; otherwise build an automatic one."""
    for col in ["display_description", "manual_description", "event_description"]:
        if col in group.columns:
            vals = [
                str(x).strip() for x in group[col].fillna("").tolist() if str(x).strip()
            ]
            if vals:
                return vals[0]
    return build_event_description(group)


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
            "tags": " | ".join(
                sorted(
                    set(
                        "|".join(
                            group.get("main_tags", pd.Series(dtype=str))
                            .fillna("")
                            .astype(str)
                        ).split("|")
                    )
                    - {""}
                )
            ),
            "start_date": pd.to_datetime(
                group.get("start_date"), errors="coerce"
            ).min(),
            "end_date": pd.to_datetime(group.get("end_date"), errors="coerce").max(),
            "message_count": int(
                pd.to_numeric(group.get("message_count", 0), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "chat_count": int(
                pd.to_numeric(group.get("chat_count", 0), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "negative_count": int(
                pd.to_numeric(group.get("negative_count", 0), errors="coerce")
                .fillna(0)
                .sum()
            ),
            "importance_score": float(
                pd.to_numeric(group.get("importance_score", 0), errors="coerce")
                .fillna(0)
                .max()
            ),
            "event_ids": (
                list(group["event_id"].astype(str))
                if "event_id" in group.columns
                else []
            ),
        }
        row["negative_share"] = (
            row["negative_count"] / row["message_count"] if row["message_count"] else 0
        )
        rows.append(row)
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["importance_score", "message_count"], ascending=False)
    return out
