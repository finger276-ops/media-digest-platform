# -*- coding: utf-8 -*-
"""
Система иерархических тегов проекта.

Замысел: у каждого клиента своя система тегов, размеченная в Brand Analytics.
Аналитик описывает ИЕРАРХИЮ этих тегов в Excel/CSV и загружает в проект.
Платформа строит дерево и агрегирует аналитику по уровням (тирам).

Формат входного файла (колонки):
    tag       — название тега (должно совпадать с колонкой-тегом в выгрузке BA)
    tier      — уровень в иерархии (1 = верхний, 2, 3, ... произвольная глубина)
    parent    — родительский тег (пусто для тегов Тир 1)

Пример:
    tag              tier   parent
    Продукция        1
    Кнауф Норд       2      Продукция
    Тисма            2      Продукция
    Конкуренты       1
    ТЕХНОНИКОЛЬ      2      Конкуренты

Модель — ДЕРЕВО: у каждого тега ровно один родитель (или ни одного для Тир 1).
Это гарантирует, что агрегаты по тирам не пересекаются и суммируются корректно.

Модуль самостоятельный: читает, ВАЛИДИРУЕТ (циклы, висячие родители,
несоответствие тира), строит дерево. Не трогает остальной код платформы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------

@dataclass
class TagNode:
    """Узел дерева тегов."""
    tag: str
    tier: int
    parent: Optional[str] = None
    children: list["TagNode"] = field(default_factory=list)

    def descendants(self) -> list["TagNode"]:
        """Все потомки узла (рекурсивно) — для агрегации поддерева."""
        result = []
        for child in self.children:
            result.append(child)
            result.extend(child.descendants())
        return result


@dataclass
class TagHierarchy:
    """Полная иерархия тегов проекта."""
    roots: list[TagNode] = field(default_factory=list)   # узлы Тир 1
    by_tag: dict[str, TagNode] = field(default_factory=dict)  # быстрый доступ по имени

    def all_tags(self) -> list[str]:
        return list(self.by_tag.keys())

    def get_node(self, tag: str) -> Optional[TagNode]:
        return self.by_tag.get(tag)

    def tier_of(self, tag: str) -> Optional[int]:
        node = self.by_tag.get(tag)
        return node.tier if node else None

    def max_depth(self) -> int:
        return max((n.tier for n in self.by_tag.values()), default=0)


class TagHierarchyError(ValueError):
    """Ошибка валидации структуры тегов."""


# ---------------------------------------------------------------------------
# Разбор и валидация
# ---------------------------------------------------------------------------

def _normalize(value) -> str:
    """Нормализация имени тега: строка, обрезка пробелов. nan/None -> пусто."""
    if value is None:
        return ""
    text = str(value).strip()
    # pandas читает пустые ячейки Excel как nan
    if text.lower() in ("nan", "none", "nat"):
        return ""
    return text


def parse_tag_rows(rows: list[dict]) -> TagHierarchy:
    """
    Построить иерархию из списка строк {tag, tier, parent}.

    Валидирует:
      - обязательность tag и tier;
      - уникальность тегов;
      - существование указанных родителей;
      - согласованность тира (tier ребёнка = tier родителя + 1);
      - отсутствие циклов;
      - у Тир 1 не должно быть родителя, у Тир 2+ — должен быть.

    Бросает TagHierarchyError с понятным сообщением при проблеме.
    """
    # --- первичный разбор ---
    nodes: dict[str, TagNode] = {}
    raw: list[tuple[str, int, str]] = []

    for i, row in enumerate(rows, start=1):
        tag = _normalize(row.get("tag"))
        parent = _normalize(row.get("parent"))
        tier_raw = row.get("tier")

        if not tag:
            raise TagHierarchyError(f"Строка {i}: пустое имя тега.")
        try:
            tier = int(tier_raw)
        except (TypeError, ValueError):
            raise TagHierarchyError(
                f"Строка {i} (тег «{tag}»): тир должен быть числом, получено «{tier_raw}»."
            )
        if tier < 1:
            raise TagHierarchyError(
                f"Строка {i} (тег «{tag}»): тир должен быть ≥ 1."
            )
        if tag in nodes:
            raise TagHierarchyError(f"Тег «{tag}» встречается несколько раз (теги должны быть уникальны).")

        nodes[tag] = TagNode(tag=tag, tier=tier, parent=parent or None)
        raw.append((tag, tier, parent))

    # --- проверка родителей и тиров ---
    for tag, tier, parent in raw:
        if tier == 1:
            if parent:
                raise TagHierarchyError(
                    f"Тег «{tag}» на Тир 1, но у него указан родитель «{parent}». "
                    "У тегов верхнего уровня не должно быть родителя."
                )
        else:
            if not parent:
                raise TagHierarchyError(
                    f"Тег «{tag}» на Тир {tier}, но у него не указан родитель."
                )
            if parent not in nodes:
                raise TagHierarchyError(
                    f"Тег «{tag}»: указан родитель «{parent}», которого нет в списке тегов."
                )
            parent_tier = nodes[parent].tier
            if tier != parent_tier + 1:
                raise TagHierarchyError(
                    f"Тег «{tag}» на Тир {tier}, а его родитель «{parent}» на Тир {parent_tier}. "
                    f"Ожидался Тир {parent_tier + 1} (уровни должны идти подряд)."
                )

    # --- сборка дерева + проверка циклов ---
    roots: list[TagNode] = []
    for tag, node in nodes.items():
        if node.parent:
            nodes[node.parent].children.append(node)
        else:
            roots.append(node)

    _check_no_cycles(nodes)

    return TagHierarchy(roots=roots, by_tag=nodes)


def _check_no_cycles(nodes: dict[str, TagNode]) -> None:
    """Убедиться, что в родительских связях нет циклов."""
    for start_tag in nodes:
        seen = set()
        current = nodes[start_tag]
        while current.parent:
            if current.parent in seen:
                raise TagHierarchyError(
                    f"Обнаружен цикл в иерархии рядом с тегом «{current.parent}»."
                )
            seen.add(current.tag)
            current = nodes[current.parent]


def parse_tag_dataframe(df, tag_col="tag", tier_col="tier", parent_col="parent") -> TagHierarchy:
    """Построить иерархию из DataFrame (Excel/CSV, загруженного аналитиком)."""
    required = {tag_col, tier_col}
    missing = required - set(df.columns)
    if missing:
        raise TagHierarchyError(
            f"В файле структуры тегов не хватает колонок: {missing}. "
            f"Нужны колонки: {tag_col}, {tier_col}, {parent_col} (parent — опционально)."
        )
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "tag": r.get(tag_col),
            "tier": r.get(tier_col),
            "parent": r.get(parent_col) if parent_col in df.columns else "",
        })
    return parse_tag_rows(rows)


# ---------------------------------------------------------------------------
# Агрегация аналитики по иерархии
# ---------------------------------------------------------------------------

def aggregate_by_hierarchy(
    hierarchy: TagHierarchy,
    tag_counts: dict[str, int],
) -> dict[str, dict]:
    """
    Свернуть счётчики по тегам в агрегаты по дереву.

    tag_counts: {имя_тега: сколько сообщений} — обычно из колонок-тегов выгрузки.
    Возвращает по каждому узлу: собственный счётчик и счётчик поддерева
    (сам узел + все потомки). Для Тир 1 счётчик поддерева = вся ветка.

    Так как дерево (один родитель), поддеревья узлов одного тира НЕ пересекаются —
    агрегаты корректно суммируются.
    """
    result: dict[str, dict] = {}
    for tag, node in hierarchy.by_tag.items():
        own = tag_counts.get(tag, 0)
        subtree = own + sum(tag_counts.get(d.tag, 0) for d in node.descendants())
        result[tag] = {
            "tag": tag,
            "tier": node.tier,
            "parent": node.parent,
            "own_count": own,          # сообщений именно с этим тегом
            "subtree_count": subtree,  # с этим тегом или любым потомком
        }
    return result


def render_tree_text(hierarchy: TagHierarchy, tag_counts: Optional[dict[str, int]] = None) -> str:
    """Текстовое дерево для предпросмотра (с числами, если переданы)."""
    lines = []

    def walk(node: TagNode, depth: int):
        indent = "  " * depth
        if tag_counts is not None:
            own = tag_counts.get(node.tag, 0)
            sub = own + sum(tag_counts.get(d.tag, 0) for d in node.descendants())
            lines.append(f"{indent}{node.tag}  (own={own}, subtree={sub})")
        else:
            lines.append(f"{indent}{node.tag}")
        for child in node.children:
            walk(child, depth + 1)

    for root in hierarchy.roots:
        walk(root, 0)
    return "\n".join(lines)
