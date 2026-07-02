# -*- coding: utf-8 -*-
"""
UI-блок фильтра шума для экрана загрузки.

Показывает статистику по шуму маркетплейсов (ответы продавцов, пустые отзывы)
в загруженной выгрузке. Информационный блок — ничего не удаляет.

Защищён мягкой деградацией: если что-то пойдёт не так, блок просто
не отобразится, а загрузка данных продолжится штатно.

Колонка текста в canonical платформы называется «Сообщение».
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.noise_filter import filter_dataframe


def render_noise_filter_block(canonical: pd.DataFrame, text_column: str = "Сообщение") -> None:
    """
    Показать блок статистики фильтра шума.

    Полностью защищён: любая ошибка внутри не ломает страницу загрузки.
    """
    try:
        if text_column not in canonical.columns:
            return  # молча пропускаем, если нет колонки текста

        df, stats = filter_dataframe(canonical, text_column=text_column)
        if stats.get("error") or stats.get("noise_total", 0) == 0:
            return  # нечего показывать

        with st.expander("🧹 Фильтр шума (ответы продавцов, пустые отзывы)", expanded=True):
            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Помечено шумом",
                f"{stats['noise_total']} ({stats['noise_percent']}%)",
                help="Сообщения без смысловой нагрузки для аналитики. Не удаляются — только помечаются.",
            )
            c2.metric("Ответы продавцов", stats["seller_replies"])
            c3.metric("Пустые отзывы", stats["empty_reviews"])

            noise = df[df["is_noise"]]
            if len(noise):
                st.markdown("**Что помечено (проверьте, ничего лишнего не попало):**")
                rows = []
                for _, row in noise.head(30).iterrows():
                    text = str(row.get(text_column, ""))
                    text = " ".join(text.split())
                    rows.append({
                        "Тип": (
                            "ответ продавца"
                            if row["noise_type"] == "seller_reply"
                            else "пустой отзыв"
                        ),
                        "Текст": (text[:130] + "…") if len(text) > 130 else text,
                    })
                st.dataframe(
                    pd.DataFrame(rows), use_container_width=True, hide_index=True
                )
            st.caption(
                "Блок информационный: показывает шум в выгрузке, но НЕ удаляет "
                "сообщения. Данные сохраняются полностью."
            )
    except Exception:
        return
