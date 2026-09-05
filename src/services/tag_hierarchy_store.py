# -*- coding: utf-8 -*-
"""
Хранение иерархии тегов проекта в Supabase (Этап 2).

Таблица: platform_tag_hierarchies (см. sql/tag_hierarchy_schema.sql) —
одна строка на проект, вся структура целиком в jsonb.

Схема работы:
  1) аналитик готовит Excel (tag/tier/parent);
  2) UI (Этап 3) читает его в DataFrame и вызывает save_tag_hierarchy();
     перед сохранением структура ПРОВЕРЯЕТСЯ парсером (циклы, родители, дубли) —
     кривая структура в базу не попадёт;
  3) аналитика (Этап 4) вызывает load_tag_hierarchy() и агрегирует по дереву.

Модуль зависит только от platform_store (клиент Supabase) и tag_hierarchy
(парсер/валидатор). Ничего в существующем коде не меняет.
"""

from __future__ import annotations

from typing import Optional

from platform_store import get_supabase_client, now_iso
from services.tag_hierarchy import (
    TagHierarchy,
    TagHierarchyError,
    parse_tag_rows,
)

TABLE = "platform_tag_hierarchies"


# ---------------------------------------------------------------------------
# Сохранение
# ---------------------------------------------------------------------------

def save_tag_hierarchy(
    project_id: str,
    records: list[dict],
    source_filename: str = "",
) -> TagHierarchy:
    """
    Проверить и сохранить структуру тегов проекта.

    records: список {"tag": ..., "tier": ..., "parent": ...} (из Excel).
    Перед записью структура прогоняется через парсер — при ошибках
    (цикл, несуществующий родитель, дубль) бросается TagHierarchyError,
    и в базу ничего не пишется.

    Возвращает построенную иерархию (для показа дерева в UI).
    """
    # 1) валидация — кривое в базу не пускаем
    hierarchy = parse_tag_rows(records)

    # 2) нормализованное представление для хранения
    structure = [
        {
            "tag": node.tag,
            "tier": node.tier,
            "parent": node.parent or "",
        }
        for node in hierarchy.by_tag.values()
    ]

    payload = {
        "project_id": project_id,
        "structure": structure,
        "source_filename": source_filename or "",
        "tags_count": len(structure),
        "max_tier": hierarchy.max_depth(),
        "updated_at": now_iso(),
    }

    client = get_supabase_client()
    client.table(TABLE).upsert(payload, on_conflict="project_id").execute()
    return hierarchy


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------

def load_tag_hierarchy(project_id: str) -> Optional[TagHierarchy]:
    """
    Загрузить иерархию тегов проекта.

    Возвращает TagHierarchy или None, если структура не загружалась.
    Если сохранённая структура вдруг не проходит валидацию (не должно
    случаться — валидируем на входе), возвращает None, не роняя аналитику.
    """
    client = get_supabase_client()
    resp = (
        client.table(TABLE)
        .select("structure")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    structure = rows[0].get("structure") or []
    if not structure:
        return None
    try:
        records = [
            {
                "tag": item.get("tag", ""),
                "tier": item.get("tier", ""),
                "parent": item.get("parent", ""),
            }
            for item in structure
        ]
        return parse_tag_rows(records)
    except TagHierarchyError:
        return None


def load_hierarchy_meta(project_id: str) -> Optional[dict]:
    """Метаданные сохранённой структуры (для показа в настройках проекта)."""
    client = get_supabase_client()
    resp = (
        client.table(TABLE)
        .select("source_filename,tags_count,max_tier,uploaded_at,updated_at")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------

def delete_tag_hierarchy(project_id: str) -> None:
    """Удалить структуру тегов проекта (например, перед загрузкой новой с нуля)."""
    client = get_supabase_client()
    client.table(TABLE).delete().eq("project_id", project_id).execute()
