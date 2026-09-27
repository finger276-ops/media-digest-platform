# -*- coding: utf-8 -*-
"""Пересборка периода из сохранённого исходного файла.

Исходный файл каждой выгрузки лежит в хранилище, а таблицы периода собраны
тем алгоритмом, который был у платформы в день загрузки. Пересборка прогоняет
тот же файл через текущий конвейер: улучшения алгоритмов (названия
инфоповодов, признак метрик в выгрузке, досчёт сюжетов) доезжают до старых
периодов без поиска файлов и повторной загрузки руками.

Что сохраняется:

- название, даты, статус и дата загрузки периода, комментарий аналитика;
- ручные правки. Правки сообщений привязаны к ID сообщения, а он стабилен —
  это хеш ссылки или ID из выгрузки. Правки инфоповодов привязаны к ID
  инфоповода, а у выгрузок не из Brand Analytics он — порядковый номер
  кластера (e_00012): после пересборки номера перетасуются. Поэтому каждый
  старый инфоповод сопоставляется с новым по составу сообщений, и ключи
  правок переписываются на новые ID. Правка, которой не нашлось пары,
  откладывается в архив (rebuild_orphans) — иначе она прилипла бы к чужому
  инфоповоду, получившему тот же номер.

Запись безопасна для работающего периода: новые строки пишутся поверх
старых, устаревшие удаляются только после этого (см.
platform_store.save_processed_tables, keep_old_rows_until_written).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

import platform_store as store

from .cached_store import clear_platform_caches, delete_manual, save_manual
from .ingest import IngestError, build_period_tables, read_canonical_bytes

# Порог совпадения состава: доля общих сообщений от объединения (Жаккар).
# Половина — инфоповод «тот же», если больше половины его сообщений остались
# вместе; меньше — это уже другая тема, и правку безопаснее отложить.
MATCH_THRESHOLD = 0.5
EVENT_TABLES = ("event_edits", "event_merges", "message_moves", "message_irrelevant")
EVENT_ID_FIELDS = ("event_id", "source_event_id", "target_event_id")
ORPHANS_TABLE = "rebuild_orphans"


class RebuildError(IngestError):
    """Пересобрать нельзя: понятный текст для аналитика."""


@dataclass
class ManualPlan:
    """Что сделать с ручными правками после пересборки."""

    rewrite: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)
    """(старый ключ, новый ключ, таблица, новый payload)."""
    orphan: list[tuple[str, str, dict[str, Any], str]] = field(default_factory=list)
    """(ключ, таблица, payload, причина)."""
    unchanged: int = 0


def _prefixed(period_id: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    prefix = f"{period_id}__"
    return text if text.startswith(prefix) else prefix + text


def event_members(
    event_discussions: pd.DataFrame | None,
    discussion_messages: pd.DataFrame | None,
    period_id: str,
) -> dict[str, set[str]]:
    """Состав инфоповодов: ID инфоповода (с префиксом периода) → ID сообщений."""
    if (
        not isinstance(event_discussions, pd.DataFrame)
        or event_discussions.empty
        or not isinstance(discussion_messages, pd.DataFrame)
        or discussion_messages.empty
        or not {"event_id", "discussion_id"} <= set(event_discussions.columns)
        or not {"discussion_id", "message_id"} <= set(discussion_messages.columns)
    ):
        return {}
    links = event_discussions[["event_id", "discussion_id"]].astype(str)
    msgs = discussion_messages[["discussion_id", "message_id"]].astype(str)
    # ID обсуждений бывают с префиксом периода и без — приводим к одному виду.
    links = links.assign(discussion_id=links["discussion_id"].map(lambda v: _prefixed(period_id, v)))
    msgs = msgs.assign(discussion_id=msgs["discussion_id"].map(lambda v: _prefixed(period_id, v)))
    merged = links.merge(msgs, on="discussion_id", how="inner")
    members: dict[str, set[str]] = {}
    for event_id, message_id in zip(merged["event_id"], merged["message_id"]):
        if str(message_id).strip():
            members.setdefault(_prefixed(period_id, event_id), set()).add(str(message_id))
    return members


def match_events(
    old: dict[str, set[str]],
    new: dict[str, set[str]],
    threshold: float = MATCH_THRESHOLD,
) -> dict[str, str]:
    """Старый ID инфоповода → новый, по наибольшему совпадению состава.

    Совпадение — доля общих сообщений от объединения. Ниже порога пары нет:
    правку такого инфоповода переносить некуда. Тот же ID при таком же
    совпадении предпочтительнее (у сюжетов Brand Analytics ID стабилен).
    """
    by_message: dict[str, set[str]] = {}
    for new_id, messages in new.items():
        for message_id in messages:
            by_message.setdefault(message_id, set()).add(new_id)
    mapping: dict[str, str] = {}
    for old_id, messages in old.items():
        if not messages:
            continue
        candidates: set[str] = set()
        for message_id in messages:
            candidates |= by_message.get(message_id, set())
        best_id, best_score = "", 0.0
        for new_id in sorted(candidates):
            union = len(messages | new[new_id])
            score = len(messages & new[new_id]) / union if union else 0.0
            if score > best_score or (score == best_score and new_id == old_id):
                best_id, best_score = new_id, score
        if best_id and best_score >= threshold:
            mapping[old_id] = best_id
    return mapping


def plan_manual_rows(
    manual_rows: pd.DataFrame | None, period_id: str, mapping: dict[str, str]
) -> ManualPlan:
    """Какие ручные правки переписать на новые ID, а какие отложить.

    Трогаются только правки, ссылающиеся на инфоповоды этого периода (ID с
    префиксом «{period_id}__»). Инфоповоды других периодов и ручные
    инфоповоды аналитика остаются как есть.
    """
    plan = ManualPlan()
    if not isinstance(manual_rows, pd.DataFrame) or manual_rows.empty:
        return plan
    prefix = f"{period_id}__"
    rows = manual_rows[manual_rows["table_name"].astype(str).isin(EVENT_TABLES)]
    taken: set[str] = set(manual_rows["row_key"].astype(str))
    # Сначала правки без изменения ключа: их ключи заняты законно.
    pending: list[tuple[str, str, str, dict[str, Any], dict[str, Any]]] = []
    for _, row in rows.iterrows():
        key = str(row.get("row_key") or "")
        table = str(row.get("table_name") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        refs = {
            part for part in key.split("::") if part.startswith(prefix)
        } | {
            str(payload.get(name)) for name in EVENT_ID_FIELDS
            if str(payload.get(name) or "").startswith(prefix)
        }
        if not refs:
            continue
        missing = sorted(ref for ref in refs if ref not in mapping)
        if missing:
            plan.orphan.append((key, table, dict(payload), "нет инфоповода с тем же составом сообщений"))
            taken.discard(key)
            continue
        new_key = "::".join(mapping.get(part, part) for part in key.split("::"))
        new_payload = dict(payload)
        for name in EVENT_ID_FIELDS:
            if str(new_payload.get(name) or "") in mapping:
                new_payload[name] = mapping[str(new_payload[name])]
        if (
            table == "event_merges"
            and new_payload.get("source_event_id")
            and new_payload.get("source_event_id") == new_payload.get("target_event_id")
        ):
            plan.orphan.append((key, table, dict(payload), "оба инфоповода собрались в один"))
            taken.discard(key)
            continue
        if new_key == key and new_payload == payload:
            plan.unchanged += 1
            continue
        pending.append((key, new_key, table, dict(payload), new_payload))
    # Ключи, которые освобождаются переписыванием, можно занимать заново.
    for key, *_rest in pending:
        taken.discard(key)
    for key, new_key, table, payload, new_payload in pending:
        if new_key in taken:
            # Два старых инфоповода собрались в один новый: правка первого
            # остаётся, вторую откладываем, а не перезаписываем молча.
            plan.orphan.append((key, table, payload, "на этот инфоповод уже перенесена другая правка"))
            continue
        taken.add(new_key)
        plan.rewrite.append((key, new_key, table, new_payload))
    return plan


def apply_manual_plan(project_id: str, period_id: str, plan: ManualPlan, *, stamp: str) -> None:
    """Записать план: сначала архив отложенных, потом удалить старые ключи и
    записать новые. Все payload уже в памяти, так что порядок «удалить, затем
    записать» не теряет данных при обмене ключами между двумя правками."""
    for key, table, payload, reason in plan.orphan:
        save_manual(
            project_id,
            ORPHANS_TABLE,
            f"rebuild_orphan::{period_id}::{key}",
            {
                "period_id": period_id,
                "original_table": table,
                "original_row_key": key,
                "payload": payload,
                "reason": reason,
                "rebuilt_at": stamp,
            },
        )
    for key, _table, _payload, _reason in plan.orphan:
        delete_manual(project_id, key)
    for old_key, new_key, _table, _payload in plan.rewrite:
        if old_key != new_key:
            delete_manual(project_id, old_key)
    for _old_key, new_key, table, payload in plan.rewrite:
        save_manual(project_id, table, new_key, payload)


def _period_row(project_id: str, period_id: str) -> dict[str, Any]:
    periods = store.list_periods(project_id, include_inactive=True)
    if periods.empty or "period_id" not in periods.columns:
        raise RebuildError("Период не найден.")
    rows = periods[periods["period_id"].astype(str) == str(period_id)]
    if rows.empty:
        raise RebuildError("Период не найден.")
    return rows.iloc[0].to_dict()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return str(value) or None
    return None if pd.isna(stamp) else stamp.isoformat()


def can_rebuild(period: dict[str, Any] | pd.Series) -> bool:
    manifest = period.get("manifest") if hasattr(period, "get") else None
    return isinstance(manifest, dict) and bool(str(manifest.get("storage_path") or "").strip())


def rebuild_period(
    project_id: str, period_id: str, *, work_dir: str | Path = "data/platform"
) -> dict[str, Any]:
    """Пересобрать период текущим конвейером; вернуть сводку."""
    period = _period_row(project_id, period_id)
    manifest = period.get("manifest") if isinstance(period.get("manifest"), dict) else {}
    storage_path = str(manifest.get("storage_path") or "").strip()
    if not storage_path:
        raise RebuildError(
            "Исходный файл этого периода не сохранён — пересобрать не из чего. "
            "Загрузите файл заново на странице «Загрузка файла»."
        )
    try:
        file_bytes = store.download_storage_file(storage_path)
    except Exception as exc:  # noqa: BLE001 — нет файла объясняется ниже
        raise RebuildError(
            "Исходный файл периода не найден в хранилище. Загрузите его заново "
            "на странице «Загрузка файла»."
        ) from exc
    if not file_bytes:
        raise RebuildError(
            "Исходный файл периода не найден в хранилище. Загрузите его заново "
            "на странице «Загрузка файла»."
        )

    source_filename = str(period.get("source_filename") or Path(storage_path).name)
    canonical = read_canonical_bytes(
        file_bytes, source_filename, str(manifest.get("source_system") or "auto")
    )
    if canonical is None or canonical.empty:
        raise RebuildError("В исходном файле не найдено ни одной строки сообщений.")

    old_members = event_members(
        store.load_table(project_id, [period_id], "event_discussions"),
        store.load_table(project_id, [period_id], "discussion_messages"),
        period_id,
    )
    pipeline_manifest, tables, algo = build_period_tables(
        canonical,
        project_id=project_id,
        source_filename=source_filename,
        params=manifest.get("algorithm_params"),
        output_dir=Path(work_dir) / project_id / period_id / "rebuild",
    )
    new_members = event_members(
        tables.get("event_discussions"), tables.get("discussion_messages"), period_id
    )
    mapping = match_events(old_members, new_members)
    plan = plan_manual_rows(store.list_manual(project_id), period_id, mapping)

    stamp = store.now_iso()
    new_manifest = {
        **manifest,
        **pipeline_manifest,
        "storage_path": storage_path,
        "algorithm_params": algo,
        "rebuilt_at": stamp,
        "rebuild_count": int(manifest.get("rebuild_count") or 0) + 1,
    }
    store.save_processed_tables(
        project_id=project_id,
        period_id=period_id,
        period_name=str(period.get("period_name") or ""),
        source_filename=source_filename,
        tables=tables,
        manifest=new_manifest,
        date_from=period.get("date_from"),
        date_to=period.get("date_to"),
        replace=True,
        status=str(period.get("status") or "active"),
        uploaded_at=_iso(period.get("uploaded_at")),
        keep_old_rows_until_written=True,
    )
    apply_manual_plan(project_id, period_id, plan, stamp=stamp)
    clear_platform_caches(project_id)

    return {
        "period_id": period_id,
        "period_name": str(period.get("period_name") or ""),
        "messages_before": int(manifest.get("rows_messages") or 0),
        "messages": int(len(tables.get("messages", pd.DataFrame()))),
        "events_before": int(manifest.get("rows_events") or 0),
        "events": int(len(tables.get("events", pd.DataFrame()))),
        "events_matched": len(mapping),
        "events_old": len(old_members),
        "edits_moved": len(plan.rewrite),
        "edits_kept": plan.unchanged,
        "edits_orphaned": len(plan.orphan),
    }
