# -*- coding: utf-8 -*-
"""
Аналитика по уровням системы тегов проекта — Этап 4.

Показывается в разделе «Теги» дашборда, если для проекта загружена
структура (Этап 3). Считает ТОЧНО по сообщениям:

  own      — сообщений именно с этим тегом;
  subtree  — УНИКАЛЬНЫХ сообщений с этим тегом или любым его потомком.

Сообщение с тегами «ТИСМА» и «Knauf Insulation» в поддереве Кнауфа
считается ОДИН раз (в отличие от простой суммы счётчиков тегов).

Блоки:
  1) верхний уровень — вклад каждой ветки (одна свёрнутая раскрывашка);
  2) выбор ветки внутри той же раскрывашки — разбивка по её детям;
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

# Общие настройки индикатора доли — один и тот же вид у обзора верхнего
# уровня и у разбивки внутри выбранной ветки.
def _share_column(label: str) -> "st.column_config.ProgressColumn":
    return st.column_config.ProgressColumn(
        label, format="%.1f%%", min_value=0, max_value=100
    )


_SUBTREE_HELP = (
    "Уникальных сообщений с этим тегом или любым его потомком — не сумма "
    "счётчиков, а множество: сообщение с двумя дочерними тегами одной ветки "
    "считается один раз."
)
_OWN_HELP = "Сообщений именно с этим тегом, без учёта потомков."

_COUNT_COLUMNS = {
    "Сообщений (с потомками)": st.column_config.NumberColumn(
        "Сообщений (с потомками)", help=_SUBTREE_HELP
    ),
    "Сообщений (сам тег)": st.column_config.NumberColumn(
        "Сообщений (сам тег)", help=_OWN_HELP
    ),
}


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

        # Раньше здесь было по очереди: обзор «Тир 1», bar_chart, дублирующий
        # ту же таблицу, потом выбор узла из списка с подписью «(Тир 2)»; на
        # следующем заходе — сплошная таблица-дерево с отступами и значками
        # «↳». Ни то, ни другое не понравилось: жаргон «тир» и стрелочки
        # мешали читать структуру. Здесь — одна свёрнутая раскрывашка: внутри
        # верхний уровень и выбор ветки для разбивки, без единого «Тир N»
        # и без значков вложенности.
        with st.expander("Структура тегов", expanded=False):
            top_level = table[table["Тир"] == table["Тир"].min()].copy()
            st.markdown("**Верхний уровень:**")
            st.dataframe(
                top_level[
                    ["Тег", "Доля от всех", "Сообщений (с потомками)", "Сообщений (сам тег)"]
                ],
                hide_index=True,
                width="stretch",
                column_config={"Доля от всех": _share_column("Доля от всех"), **_COUNT_COLUMNS},
            )

            branches = sorted(t for t, n in hierarchy.by_tag.items() if n.children)
            if branches:
                chosen = st.selectbox(
                    "Показать состав ветки:",
                    branches,
                    key=f"tier_branch_{project_id}",
                )
                node = hierarchy.get_node(chosen)
                if node and node.children:
                    child_names = [c.tag for c in node.children]
                    child_rows = table[table["Тег"].isin(child_names)].copy()
                    parent_subtree = int(
                        table.loc[table["Тег"] == chosen, "Сообщений (с потомками)"].iloc[0]
                    )
                    if parent_subtree > 0:
                        child_rows["Доля в ветке"] = (
                            child_rows["Сообщений (с потомками)"] / parent_subtree * 100
                        ).round(1)
                    else:
                        child_rows["Доля в ветке"] = 0.0
                    st.markdown(f"**Состав «{chosen}»** ({parent_subtree} сообщений в ветке):")
                    st.dataframe(
                        child_rows[
                            [
                                "Тег",
                                "Доля в ветке",
                                "Сообщений (с потомками)",
                                "Сообщений (сам тег)",
                            ]
                        ],
                        hide_index=True,
                        width="stretch",
                        column_config={"Доля в ветке": _share_column("Доля в ветке"), **_COUNT_COLUMNS},
                    )

        # --- Теги вне структуры (подсказка аналитику) ---
        if coverage["outside_tags"]:
            with st.expander("Теги в данных, отсутствующие в структуре", expanded=False):
                st.caption(
                    "Эти теги встречаются в сообщениях, но не входят в загруженную "
                    "структуру — возможно, стоит добавить их в дерево."
                )
                outside_df = pd.DataFrame(
                    coverage["outside_tags"], columns=["Тег (нормализован)", "Сообщений"]
                )
                st.dataframe(outside_df, hide_index=True, width="stretch")
    except Exception as exc:  # noqa: BLE001 — своя граница отказа блока
        # Мягкая деградация: аналитика по тирам не должна ломать раздел «Теги».
        # Но исчезать молча тоже нельзя — владелец узнаёт через канал ошибок.
        from services.observability import report_failure

        report_failure("блок «Аналитика по уровням тегов»", exc)
        return
