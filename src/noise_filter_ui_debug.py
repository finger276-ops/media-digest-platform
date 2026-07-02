# -*- coding: utf-8 -*-
"""
ОТЛАДОЧНАЯ версия UI-блока фильтра шума.
Отличие от рабочей: НЕ гасит ошибки, а показывает их на экране.
Нужна, чтобы увидеть, почему блок не появляется. Потом вернём обычную версию.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.noise_filter import filter_dataframe


def render_noise_filter_block(canonical: pd.DataFrame, text_column: str = "Текст") -> None:
    """Отладочная версия — показывает всё, что происходит."""
    st.write("🔍 DEBUG: блок фильтра шума вызван")  # маркер, что вызов дошёл

    # показываем, какие колонки реально есть
    st.write(f"🔍 DEBUG: колонки в данных: {list(canonical.columns)[:15]}")

    if text_column not in canonical.columns:
        st.warning(f"🔍 DEBUG: колонка «{text_column}» НЕ найдена! "
                   f"Возможно, текст в другой колонке.")
        return

    df, stats = filter_dataframe(canonical, text_column=text_column)
    st.write(f"🔍 DEBUG: статистика фильтра = {stats}")

    if stats.get("noise_total", 0) == 0:
        st.info("🔍 DEBUG: шума не найдено, поэтому блок пустой (это нормально).")
        return

    with st.expander("🧹 Фильтр шума (ответы продавцов, пустые отзывы)", expanded=True):
        c1, c2, c3 = st.columns(3)
        c1.metric("Помечено шумом", f"{stats['noise_total']} ({stats['noise_percent']}%)")
        c2.metric("Ответы продавцов", stats["seller_replies"])
        c3.metric("Пустые отзывы", stats["empty_reviews"])

        noise = df[df["is_noise"]]
        rows = []
        for _, row in noise.head(30).iterrows():
            text = " ".join(str(row.get(text_column, "")).split())
            rows.append({
                "Тип": ("ответ продавца" if row["noise_type"] == "seller_reply"
                        else "пустой отзыв"),
                "Текст": (text[:130] + "…") if len(text) > 130 else text,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
