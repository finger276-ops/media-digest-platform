# -*- coding: utf-8 -*-
"""
UI-блок фильтра шума для экрана загрузки.

Показывает статистику по шуму маркетплейсов (ответы продавцов, пустые отзывы).
Информационный блок — ничего не удаляет.

Колонка текста в canonical платформы называется «Сообщение».
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from metric_cards_ui import metric_card, render_metric_row
from services.noise_filter import filter_dataframe


def render_noise_filter_block(canonical: pd.DataFrame, text_column: str = "Сообщение") -> None:
    """Показать блок статистики фильтра шума. Всегда виден при загрузке."""
    try:
        with st.expander("🧹 Фильтр шума в выгрузке", expanded=True):
            if text_column not in canonical.columns:
                st.warning(
                    f"Колонка «{text_column}» не найдена. "
                    f"Доступные колонки: {list(canonical.columns)}"
                )
                return

            df, stats = filter_dataframe(canonical, text_column=text_column)

            if stats.get("error"):
                st.warning(f"Фильтр: {stats['error']}")
                return

            render_metric_row(
                [
                    metric_card(
                        "Помечено шумом",
                        f"{stats['noise_total']} ({stats['noise_percent']}%)",
                        help_text="Сообщения без смысловой нагрузки. Не удаляются — только помечаются.",
                    ),
                    metric_card("Ответы продавцов", stats["seller_replies"]),
                    metric_card("Пустые отзывы", stats["empty_reviews"]),
                ],
                columns=3,
            )

            if stats["noise_total"] == 0:
                st.info(
                    "В этой выгрузке фильтр не нашёл шума (ответов продавцов "
                    "и пустых отзывов). Это нормально для небольших выгрузок."
                )
                return

            noise = df[df["is_noise"]]
            st.markdown("**Что помечено (проверьте, ничего лишнего не попало):**")
            rows = []
            for _, row in noise.head(30).iterrows():
                text = " ".join(str(row.get(text_column, "")).split())
                rows.append({
                    "Тип": (
                        "ответ продавца"
                        if row["noise_type"] == "seller_reply"
                        else "пустой отзыв"
                    ),
                    "Текст": (text[:130] + "…") if len(text) > 130 else text,
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.caption(
                "Блок информационный: показывает шум, но НЕ удаляет сообщения."
            )
    except Exception as exc:
        # покажем ошибку вместо молчаливого проглатывания
        st.warning(f"Фильтр шума: не удалось построить блок ({exc})")
