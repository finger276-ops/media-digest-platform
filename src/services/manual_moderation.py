# -*- coding: utf-8 -*-
"""Ручная модерация инфоповодов и сообщений: чтение и применение правок.

Правки (скрытие сообщения, объединение инфоповодов, перенос сообщения между
темами и т.д.) хранятся построчно в platform_manual_rows через
services.cached_store; здесь они собираются в единое состояние
(get_manual_state) и накатываются поверх обработанных events/messages
(apply_manual_overrides) — без Streamlit, чтобы использовать тот же код и в
UI, и в фоновых пересчётах.
"""

from __future__ import annotations

import uuid
from typing import Any

import pandas as pd

from .cached_store import delete_manual, list_manual, save_manual
from .event_titles import normalize_event_title
from .metrics_compute import sentiment_masks


def manual_payloads(manual_df: pd.DataFrame, table_name: str) -> list[dict[str, Any]]:
    if manual_df is None or manual_df.empty or "table_name" not in manual_df.columns:
        return []
    rows = manual_df[manual_df["table_name"].astype(str) == table_name]
    payloads: list[dict[str, Any]] = []
    for _, row in rows.iterrows():
        payload = row.get("payload") or {}
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("_row_key", str(row.get("row_key") or ""))
            payloads.append(payload)
    return payloads


def get_manual_state(project_id: str) -> dict[str, Any]:
    try:
        manual_df = list_manual(project_id)
    except Exception:
        manual_df = pd.DataFrame()

    hidden_messages: set[str] = set()
    hidden_message_keys: dict[str, str] = {}
    for payload in manual_payloads(manual_df, "message_hidden"):
        message_id = str(
            payload.get("message_id")
            or payload.get("_row_key", "").replace("message_hidden::", "")
        )
        if message_id:
            hidden_messages.add(message_id)
            hidden_message_keys[message_id] = str(
                payload.get("_row_key") or f"message_hidden::{message_id}"
            )

    irrelevant_pairs: set[tuple[str, str]] = set()
    irrelevant_keys: dict[tuple[str, str], str] = {}
    for payload in manual_payloads(manual_df, "message_irrelevant"):
        event_id = str(payload.get("event_id") or "")
        message_id = str(payload.get("message_id") or "")
        if event_id and message_id:
            pair = (event_id, message_id)
            irrelevant_pairs.add(pair)
            irrelevant_keys[pair] = str(
                payload.get("_row_key")
                or f"message_irrelevant::{event_id}::{message_id}"
            )

    move_map: dict[str, str] = {}
    for payload in manual_payloads(manual_df, "message_moves"):
        message_id = str(
            payload.get("message_id")
            or payload.get("_row_key", "").replace("message_move::", "")
        )
        target_event_id = str(payload.get("target_event_id") or "")
        if message_id and target_event_id:
            move_map[message_id] = target_event_id

    event_edits: dict[str, dict[str, Any]] = {}
    for payload in manual_payloads(manual_df, "event_edits"):
        event_id = str(
            payload.get("event_id")
            or payload.get("_row_key", "").replace("event_edit::", "")
        )
        if event_id:
            event_edits[event_id] = payload

    event_merges: dict[str, str] = {}
    for payload in manual_payloads(manual_df, "event_merges"):
        source_event_id = str(
            payload.get("source_event_id")
            or payload.get("_row_key", "").replace("event_merge::", "")
        )
        target_event_id = str(payload.get("target_event_id") or "")
        if source_event_id and target_event_id:
            event_merges[source_event_id] = target_event_id

    manual_events = manual_payloads(manual_df, "manual_events")

    # Заголовки, которые аналитик запретил склеивать автоматически: страховка
    # на случай, когда алгоритм счёл две разные темы одной.
    title_merge_blocks: set[str] = set()
    for payload in manual_payloads(manual_df, "title_merge_blocks"):
        title = str(
            payload.get("title")
            or payload.get("_row_key", "").replace("title_merge_block::", "")
        ).strip()
        if title:
            title_merge_blocks.add(title)

    return {
        "manual_df": manual_df,
        "hidden_messages": hidden_messages,
        "hidden_message_keys": hidden_message_keys,
        "irrelevant_pairs": irrelevant_pairs,
        "irrelevant_keys": irrelevant_keys,
        "move_map": move_map,
        "event_edits": event_edits,
        "event_merges": event_merges,
        "manual_events": manual_events,
        "title_merge_blocks": title_merge_blocks,
    }


def manual_versions(manual_state: dict[str, Any] | None) -> dict[str, Any]:
    """Версии правок из снимка страницы: row_key → updated_at.

    Нужны для условного сохранения: редактор пишет поверх той версии, которую
    видел. Отсутствие ключа в словаре означает «записи не было» — при
    сохранении это ожидание None, и появление строки со стороны считается
    конфликтом.
    """
    df = (manual_state or {}).get("manual_df")
    if not isinstance(df, pd.DataFrame) or df.empty or "row_key" not in df.columns:
        return {}
    versions: dict[str, Any] = {}
    updated = df["updated_at"] if "updated_at" in df.columns else None
    for position, row_key in enumerate(df["row_key"].astype(str)):
        value = updated.iloc[position] if updated is not None else None
        versions[row_key] = None if pd.isna(value) else value
    return versions


def blocked_title_merges(manual_state: dict[str, Any] | None) -> set[str]:
    """Нормализованные заголовки, которые нельзя склеивать автоматически."""
    raw = (manual_state or {}).get("title_merge_blocks") or set()
    return {normalize_event_title(x) for x in raw if str(x).strip()}


def append_manual_events(
    events: pd.DataFrame, manual_events: list[dict[str, Any]]
) -> pd.DataFrame:
    if not manual_events:
        return events
    rows = []
    for payload in manual_events:
        event_id = str(payload.get("event_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not event_id or not title:
            continue
        rows.append(
            {
                "event_id": event_id,
                "event_title": title,
                "event_summary": str(payload.get("description") or ""),
                "display_description": str(payload.get("description") or ""),
                "main_tags": str(payload.get("tags") or "Ручной инфоповод"),
                "start_date": pd.NaT,
                "end_date": pd.NaT,
                "message_count": 0,
                "chat_count": 0,
                "negative_count": 0,
                "importance_score": 0,
                "status": str(payload.get("status") or "active"),
                "is_manual_event": True,
            }
        )
    if not rows:
        return events
    extra = pd.DataFrame(rows)
    if events is None or events.empty:
        return extra
    return pd.concat([events, extra], ignore_index=True, sort=False)


def recompute_event_counts(
    events: pd.DataFrame, messages: pd.DataFrame
) -> pd.DataFrame:
    if (
        events is None
        or events.empty
        or messages is None
        or messages.empty
        or "event_id" not in messages.columns
    ):
        return events
    out = events.copy()
    msg = messages.copy()
    msg["event_id"] = msg["event_id"].fillna("").astype(str)
    msg = msg[msg["event_id"].str.strip() != ""]
    if msg.empty:
        return out
    # Маска негатива — один раз на весь кадр, а не заново в каждом инфоповоде:
    # пересчёт идёт на каждом перезапуске страницы при сужении гранулярности.
    count_negative = any(
        c in msg.columns for c in ("sentiment", "is_negative", "_is_negative_bool")
    )
    if count_negative:
        msg["_negative_mask"] = sentiment_masks(msg)[1].astype(bool).to_numpy()
    grouped = msg.groupby("event_id", dropna=False)
    for event_id, group in grouped:
        mask = out["event_id"].astype(str) == str(event_id)
        if not mask.any():
            continue
        out.loc[mask, "message_count"] = int(len(group))
        if "chat_title" in group.columns:
            out.loc[mask, "chat_count"] = int(
                group["chat_title"]
                .fillna("")
                .astype(str)
                .replace("", pd.NA)
                .dropna()
                .nunique()
            )
        elif "chat_id" in group.columns:
            out.loc[mask, "chat_count"] = int(
                group["chat_id"]
                .fillna("")
                .astype(str)
                .replace("", pd.NA)
                .dropna()
                .nunique()
            )
        if count_negative:
            out.loc[mask, "negative_count"] = int(group["_negative_mask"].sum())
        if "datetime" in group.columns:
            dt = pd.to_datetime(group["datetime"], errors="coerce").dropna()
            if not dt.empty:
                out.loc[mask, "start_date"] = dt.min()
                out.loc[mask, "end_date"] = dt.max()
    return out


def apply_manual_overrides(
    project_id: str, events: pd.DataFrame, messages: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    state = get_manual_state(project_id)
    events_out = events.copy() if events is not None else pd.DataFrame()
    messages_out = messages.copy() if messages is not None else pd.DataFrame()

    events_out = append_manual_events(events_out, state["manual_events"])
    if not events_out.empty:
        for col in [
            "event_id",
            "event_title",
            "event_summary",
            "main_tags",
            "status",
            "display_description",
        ]:
            if col not in events_out.columns:
                events_out[col] = ""

    # Apply event-level manual edits.
    if not events_out.empty:
        for event_id, payload in state["event_edits"].items():
            mask = events_out["event_id"].astype(str) == str(event_id)
            if not mask.any():
                continue
            if str(payload.get("title") or "").strip():
                events_out.loc[mask, "event_title"] = str(payload.get("title")).strip()
            if "description" in payload:
                events_out.loc[mask, "display_description"] = str(
                    payload.get("description") or ""
                ).strip()
                events_out.loc[mask, "event_summary"] = str(
                    payload.get("description") or ""
                ).strip()
            if str(payload.get("tags") or "").strip():
                events_out.loc[mask, "main_tags"] = str(payload.get("tags")).strip()
            if str(payload.get("status") or "").strip():
                events_out.loc[mask, "status"] = str(payload.get("status")).strip()

    # Merge events by redirecting source title to target title.
    if not events_out.empty and state["event_merges"]:
        title_map = {
            str(row.get("event_id")): str(row.get("event_title") or "Без названия")
            for _, row in events_out.iterrows()
        }
        summary_map = {
            str(row.get("event_id")): str(
                row.get("display_description") or row.get("event_summary") or ""
            )
            for _, row in events_out.iterrows()
        }
        for source_event_id, target_event_id in state["event_merges"].items():
            mask = events_out["event_id"].astype(str) == str(source_event_id)
            if mask.any():
                events_out.loc[mask, "event_title"] = title_map.get(
                    str(target_event_id), str(target_event_id)
                )
                events_out.loc[mask, "display_description"] = summary_map.get(
                    str(target_event_id), ""
                )
                events_out.loc[mask, "merged_into"] = str(target_event_id)

    if not messages_out.empty:
        if "message_id" not in messages_out.columns:
            messages_out["message_id"] = messages_out.index.astype(str)
        messages_out["message_id"] = messages_out["message_id"].fillna("").astype(str)

        if state["hidden_messages"]:
            messages_out = messages_out[
                ~messages_out["message_id"].isin(state["hidden_messages"])
            ].copy()

        if "event_id" in messages_out.columns:
            messages_out["event_id"] = messages_out["event_id"].fillna("").astype(str)
            if state["event_merges"]:
                messages_out["event_id"] = messages_out["event_id"].map(
                    lambda x: state["event_merges"].get(str(x), str(x))
                )
            if state["move_map"]:
                messages_out["event_id"] = messages_out.apply(
                    lambda r: state["move_map"].get(
                        str(r.get("message_id")), str(r.get("event_id") or "")
                    ),
                    axis=1,
                )

    # Refresh titles in messages after moves/merges.
    if (
        not events_out.empty
        and not messages_out.empty
        and "event_id" in messages_out.columns
    ):
        title_map = {
            str(row.get("event_id")): str(row.get("event_title") or "Без названия")
            for _, row in events_out.iterrows()
        }
        messages_out["event_title"] = (
            messages_out["event_id"]
            .fillna("")
            .astype(str)
            .map(title_map)
            .fillna(messages_out.get("event_title", ""))
        )

    # Инфоповод, который объединили с другим, отдал свои сообщения цели, но
    # recompute_event_counts обновляет счётчики только у инфоповодов, у
    # которых сейчас есть сообщения. Источник оставался со старыми
    # счётчиками, получал заголовок цели — и в таблице (строки с одним
    # заголовком складываются) цель показывала 6 сообщений вместо 4, 2
    # негативных вместо 1. Обнуляем источник ДО пересчёта: если на нём всё же
    # есть сообщения (цепочка e1→e2→e3 не транзитивна, сообщения e1 уезжают
    # ровно на e2), пересчёт вернёт ему настоящие числа. Цель, которой нет в
    # выборке, не трогаем: сообщения тогда ни на ком, и обнуление спрятало
    # бы их совсем.
    if not events_out.empty and state["event_merges"]:
        known_ids = set(events_out["event_id"].astype(str))
        merged_sources = [
            source
            for source, target in state["event_merges"].items()
            if str(target) in known_ids
        ]
        if merged_sources:
            source_mask = events_out["event_id"].astype(str).isin(merged_sources)
            for column in ("message_count", "chat_count", "negative_count"):
                if column in events_out.columns:
                    events_out.loc[source_mask, column] = 0

    events_out = recompute_event_counts(events_out, messages_out)

    # Hide events after counts are recomputed.
    if not events_out.empty and "status" in events_out.columns:
        events_out = events_out[
            ~events_out["status"]
            .fillna("")
            .astype(str)
            .str.lower()
            .isin(["hidden", "deleted", "archived"])
        ].copy()

    return events_out, messages_out, state


UNDO_HIDDEN = "hidden"
UNDO_MERGE = "merge"
UNDO_MOVE = "move"


def manual_undo_items(
    manual_state: dict[str, Any] | None,
    event_titles: dict[str, str] | None = None,
    message_texts: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Правки, которые можно отменить: скрытые инфоповоды, объединения, переносы.

    Раньше отменить из интерфейса можно было только «Не склеивать». Скрытая
    по ошибке тема исчезала из таблицы вместе с кнопками правки, объединённая
    не имела пути назад, и такая ошибка оставалась у всех пользователей
    проекта. Здесь только список и подписи — сама отмена в undo_manual_item.
    """
    state = manual_state or {}
    titles = event_titles or {}
    texts = message_texts or {}
    items: list[dict[str, Any]] = []

    for event_id, payload in sorted((state.get("event_edits") or {}).items()):
        if str(payload.get("status") or "").strip().lower() != "hidden":
            continue
        title = str(payload.get("title") or "").strip() or titles.get(str(event_id), "")
        items.append(
            {
                "kind": UNDO_HIDDEN,
                "row_key": str(payload.get("_row_key") or f"event_edit::{event_id}"),
                "event_id": str(event_id),
                "label": f"Скрыт инфоповод «{title or event_id}»",
                "payload": dict(payload),
            }
        )

    for source, target in sorted((state.get("event_merges") or {}).items()):
        target_title = titles.get(str(target), str(target))
        items.append(
            {
                "kind": UNDO_MERGE,
                "row_key": f"event_merge::{source}",
                "event_id": str(source),
                "label": f"Инфоповод {source} объединён с «{target_title}»",
                "payload": {"source_event_id": str(source), "target_event_id": str(target)},
            }
        )

    for message_id, target in sorted((state.get("move_map") or {}).items()):
        target_title = titles.get(str(target), str(target))
        text = " ".join(str(texts.get(str(message_id), "")).split())
        snippet = (text[:70] + "…") if len(text) > 70 else text
        items.append(
            {
                "kind": UNDO_MOVE,
                "row_key": f"message_move::{message_id}",
                "event_id": str(target),
                "label": (
                    f"Сообщение «{snippet or message_id}» перенесено в «{target_title}»"
                ),
                "payload": {"message_id": str(message_id), "target_event_id": str(target)},
            }
        )
    return items


def undo_manual_item(project_id: str, item: dict[str, Any]) -> None:
    """Отменить одну правку из manual_undo_items.

    Скрытие отменяется возвратом статуса active с прежними названием,
    описанием и тегами: удалить строку целиком значило бы заодно стереть
    правки текста, сделанные до скрытия. Объединение и перенос — это
    отдельные строки, и отмена их просто удаляет.
    """
    kind = item.get("kind")
    row_key = str(item.get("row_key") or "")
    if not row_key:
        return
    if kind == UNDO_HIDDEN:
        payload = {
            key: value
            for key, value in dict(item.get("payload") or {}).items()
            if not str(key).startswith("_")
        }
        payload["status"] = "active"
        save_manual(project_id, "event_edits", row_key, payload)
    elif kind in (UNDO_MERGE, UNDO_MOVE):
        delete_manual(project_id, row_key)


def create_manual_event(
    project_id: str,
    title: str,
    description: str = "",
    tags: str = "",
    status: str = "active",
) -> str:
    event_id = "manual_" + uuid.uuid4().hex[:12]
    save_manual(
        project_id,
        "manual_events",
        f"manual_event::{event_id}",
        {
            "event_id": event_id,
            "title": title.strip(),
            "description": description.strip(),
            "tags": tags.strip() or "Ручной инфоповод",
            "status": status,
        },
    )
    return event_id


def event_select_options(
    events_agg: pd.DataFrame, exclude_event_ids: set[str] | None = None
) -> list[tuple[str, str]]:
    exclude_event_ids = exclude_event_ids or set()
    options: list[tuple[str, str]] = []
    if events_agg is None or events_agg.empty:
        return options
    for _, row in events_agg.iterrows():
        ids = [str(x) for x in row.get("event_ids", [])]
        if not ids:
            continue
        if set(ids) & exclude_event_ids:
            continue
        label = f"{row.get('title') or ids[0]} · {int(row.get('message_count') or 0)} сообщ."
        options.append((ids[0], label))
    return options
