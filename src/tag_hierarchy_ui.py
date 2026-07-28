# -*- coding: utf-8 -*-
"""
UI-блок «Система тегов проекта» (Этап 3).

Размещается на экране загрузки. Позволяет аналитику:
  - увидеть текущую структуру тегов проекта (если загружена);
  - загрузить новую структуру из Excel/CSV (колонки: tag / tier / parent);
  - перед сохранением увидеть построенное дерево и ошибки валидации.

Кривая структура (циклы, несуществующие родители, дубли) в базу не попадает —
save_tag_hierarchy валидирует до записи, старая структура остаётся цела.

Защищён мягкой деградацией: ошибка внутри блока не ломает экран загрузки.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.tag_hierarchy import (
    TagHierarchyError,
    parse_tag_dataframe,
    render_tree_text,
)
from services.tag_hierarchy_store import (
    load_hierarchy_meta,
    save_tag_hierarchy,
)


def _read_structure_file(uploaded) -> pd.DataFrame:
    """Прочитать Excel/CSV со структурой. Терпимо к битым стилям xlsx."""
    name = (uploaded.name or "").lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded)
    try:
        return pd.read_excel(uploaded)
    except Exception:
        uploaded.seek(0)
        return pd.read_excel(uploaded, engine="calamine")


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Привести названия колонок к tag/tier/parent (терпимо к регистру/русским)."""
    mapping = {}
    for col in df.columns:
        low = str(col).strip().lower()
        if low in ("tag", "тег", "название"):
            mapping[col] = "tag"
        elif low in ("tier", "тир", "уровень"):
            mapping[col] = "tier"
        elif low in ("parent", "родитель", "родительский тег"):
            mapping[col] = "parent"
    return df.rename(columns=mapping)


def render_tag_hierarchy_block(project_id: str) -> None:
    """Блок управления системой тегов проекта. Вызывается на экране загрузки."""
    try:
        with st.expander("🏷️ Система тегов проекта", expanded=False):
            # --- текущая структура ---
            try:
                meta = load_hierarchy_meta(project_id)
            except Exception:
                meta = None
            if meta:
                uploaded_at = str(meta.get("updated_at") or meta.get("uploaded_at") or "")[:10]
                st.success(
                    f"Загружена структура: {meta.get('tags_count', '?')} тегов, "
                    f"глубина {meta.get('max_tier', '?')} "
                    f"(файл: {meta.get('source_filename') or '—'}, обновлена {uploaded_at})"
                )
            else:
                st.info(
                    "Структура тегов ещё не загружена. Подготовьте Excel/CSV "
                    "с колонками tag / tier / parent и загрузите ниже."
                )

            st.caption(
                "Формат: tag — название тега (как колонка в выгрузке Brand Analytics "
                "или группирующий тег), tier — уровень (1 = верхний), "
                "parent — родительский тег (пусто для уровня 1). "
                "Модель — дерево: у каждого тега один родитель."
            )

            structure_file = st.file_uploader(
                "Файл структуры тегов (Excel/CSV)",
                type=["xlsx", "xls", "csv"],
                key=f"tag_structure_{project_id}",
            )
            if structure_file is None:
                return

            # --- чтение и парсинг ---
            try:
                df = _normalize_columns(_read_structure_file(structure_file))
            except Exception as exc:
                st.error(f"Не удалось прочитать файл: {exc}")
                return

            missing = [c for c in ("tag", "tier", "parent") if c not in df.columns]
            if missing:
                st.error(
                    f"В файле не найдены колонки: {', '.join(missing)}. "
                    f"Найдены: {list(df.columns)}"
                )
                return

            try:
                hierarchy = parse_tag_dataframe(df)
            except TagHierarchyError as exc:
                st.error(f"Структура не прошла проверку: {exc}")
                return

            st.markdown("**Предпросмотр дерева:**")
            st.code(render_tree_text(hierarchy), language=None)

            # --- сохранение ---
            if st.button("Сохранить структуру в проект", key=f"save_tags_{project_id}"):
                records = df.to_dict(orient="records")
                try:
                    saved = save_tag_hierarchy(
                        project_id, records, source_filename=structure_file.name
                    )
                except TagHierarchyError as exc:
                    st.error(f"Структура не прошла проверку: {exc}")
                    return
                except Exception as exc:
                    st.error(f"Не удалось сохранить структуру: {exc}")
                    return
                st.success(
                    f"Структура сохранена: {len(saved.by_tag)} тегов, "
                    f"глубина {saved.max_depth()}. Аналитика по уровням станет "
                    f"доступна после следующего этапа."
                )
    except Exception:
        # мягкая деградация: блок тегов не должен ломать экран загрузки
        return
