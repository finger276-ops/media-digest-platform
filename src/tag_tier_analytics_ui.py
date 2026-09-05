# -*- coding: utf-8 -*-
"""
Аналитика по уровням (тирам) системы тегов проекта — Этап 4.

Показывается в разделе «Теги» дашборда, если для проекта загружена
структура (Этап 3). Считает ТОЧНО по сообщениям:

  own      — сообщений именно с этим тегом;
  subtree  — УНИКАЛЬНЫХ сообщений с этим тегом или любым его потомком.

Сообщение с тегами «ТИСМА» и «Knauf Insulation» в поддереве Кнауфа
считается ОДИН раз (в отличие от простой суммы счётчиков тегов).

Блоки:
  1) обзор Тир 1 — вклад каждой ветки;
  2) drill-down — выбор узла и разбивка по его детям;
  3) покрытие — сколько сообщений размечено структурой, какие теги
     из данных в структуру не входят (подсказка аналитику).

Мягкая деградация: любая ошибка внутри не ломает раздел «Теги».
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.tag_compute import normalize_tag_key, split_pipe_values
from services.tag_hierarchy import TagHierarchy
from services.tag_hierarchy_store import load_tag_hierarchy


# ---------------------------------------------------------------------------
# Подсчёт по сообщениям
# ---------------------------------------------------------------------------

def _message_tag_sets(messages: pd.DataFrame) -> list[set[str]]:
    """Для каждого сообщения — множество нормализованных тегов."""
    if messages is None or messages.empty or "tags" not in messages.columns:
        return []
    out: list[set[str]] = []
    for raw in messages["tags"].fillna("").astype(str).tolist():
        out.append({normalize_tag_key(t) for t in split_pipe_values(raw)})
    return out


def compute_tier_aggregates(
    hierarchy: TagHierarchy, messages: pd.DataFrame
) -> tuple[pd.DataFrame, dict]:
    """
    Точная агрегация по дереву на уровне сообщений.

    Возвращает (таблица по всем узлам, сводка покрытия).
    """
    tag_sets = _message_tag_sets(messages)
    total = len(tag_sets)

    # нормализованное имя -> каноническое имя узла
    norm_to_tag = {normalize_tag_key(t): t for t in hierarchy.by_tag}

    rows = []
    for tag, node in hierarchy.by_tag.items():
        own_key = normalize_tag_key(tag)
        subtree_keys = {own_key} | {
            normalize_tag_key(d.tag) for d in node.descendants()
        }
        own = sum(1 for s in tag_sets if own_key in s)
        subtree = sum(1 for s in tag_sets if s & subtree_keys)
        rows.append(
            {
                "Тег": tag,
                "Тир": node.tier,
                "Родитель": node.parent or "",
                "Сообщений (сам тег)": own,
                "Сообщений (с потомками)": subtree,
                "Доля от всех": round(subtree / total * 100, 1) if total else 0.0,
            }
        )
    table = pd.DataFrame(rows).sort_values(
        ["Тир", "Сообщений (с потомками)"], ascending=[True, False]
    )

    # покрытие структурой
    hierarchy_keys = set(norm_to_tag)
    covered = sum(1 for s in tag_sets if s & hierarchy_keys)
    outside: dict[str, int] = {}
    for s in tag_sets:
        for key in s - hierarchy_keys:
            outside[key] = outside.get(key, 0) + 1
    coverage = {
        "total": total,
        "covered": covered,
        "covered_pct": round(covered / total * 100, 1) if total else 0.0,
        "outside_tags": sorted(outside.items(), key=lambda x: -x[1])[:15],
    }
    return table, coverage


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def render_tier_analytics_block(
    messages: pd.DataFrame, project_id: str | None = None
) -> None:
    """Блок иерархической аналитики в разделе «Теги»."""
    try:
        if not project_id:
            return
        try:
            hierarchy = load_tag_hierarchy(project_id)
        except Exception:
            hierarchy = None
        if hierarchy is None:
            st.caption(
                "💡 Для аналитики по уровням загрузите систему тегов проекта "
                "(экран «Загрузка» → «Система тегов проекта»)."
            )
            return

        table, coverage = compute_tier_aggregates(hierarchy, messages)
        if table.empty:
            return

        st.subheader("Аналитика по уровням тегов")
        st.caption(
            f"Структура: {len(hierarchy.by_tag)} тегов, глубина {hierarchy.max_depth()}. "
            f"Покрытие: {coverage['covered']} из {coverage['total']} сообщений "
            f"({coverage['covered_pct']}%) имеют хотя бы один тег из структуры."
        )

        # --- 1. Обзор Тир 1 ---
        tier1 = table[table["Тир"] == 1].copy()
        if not tier1.empty:
            st.markdown("**Верхний уровень (Тир 1):**")
            chart_df = tier1.set_index("Тег")["Сообщений (с потомками)"]
            st.bar_chart(chart_df)
            st.dataframe(
                tier1[
                    ["Тег", "Сообщений (с потомками)", "Сообщений (сам тег)", "Доля от всех"]
                ],
                hide_index=True,
                use_container_width=True,
            )

        # --- 2. Drill-down ---
        parents = [
            t for t, n in hierarchy.by_tag.items() if n.children
        ]
        if parents:
            parents_sorted = sorted(
                parents, key=lambda t: (hierarchy.tier_of(t) or 99, t)
            )
            chosen = st.selectbox(
                "Провалиться в узел:",
                parents_sorted,
                format_func=lambda t: f"{t} (Тир {hierarchy.tier_of(t)})",
                key=f"tier_drill_{project_id}",
            )
            node = hierarchy.get_node(chosen)
            if node and node.children:
                child_names = [c.tag for c in node.children]
                child_rows = table[table["Тег"].isin(child_names)].copy()
                parent_subtree = int(
                    table.loc[table["Тег"] == chosen, "Сообщений (с потомками)"].iloc[0]
                )
                if parent_subtree > 0:
                    child_rows["Доля в узле"] = (
                        child_rows["Сообщений (с потомками)"] / parent_subtree * 100
                    ).round(1)
                else:
                    child_rows["Доля в узле"] = 0.0
                st.markdown(f"**Внутри «{chosen}»** ({parent_subtree} сообщений в ветке):")
                chart_df = child_rows.set_index("Тег")["Сообщений (с потомками)"]
                st.bar_chart(chart_df)
                st.dataframe(
                    child_rows[
                        [
                            "Тег",
                            "Сообщений (с потомками)",
                            "Сообщений (сам тег)",
                            "Доля в узле",
                        ]
                    ],
                    hide_index=True,
                    use_container_width=True,
                )

        # --- 3. Теги вне структуры (подсказка аналитику) ---
        if coverage["outside_tags"]:
            with st.expander("Теги в данных, отсутствующие в структуре", expanded=False):
                st.caption(
                    "Эти теги встречаются в сообщениях, но не входят в загруженную "
                    "структуру — возможно, стоит добавить их в дерево."
                )
                outside_df = pd.DataFrame(
                    coverage["outside_tags"], columns=["Тег (нормализован)", "Сообщений"]
                )
                st.dataframe(outside_df, hide_index=True, use_container_width=True)
    except Exception:
        # мягкая деградация: аналитика по тирам не должна ломать раздел «Теги»
        return
