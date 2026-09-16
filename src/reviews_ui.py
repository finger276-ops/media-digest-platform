# -*- coding: utf-8 -*-
"""Раздел «Отзывы»: репутация товара по отзывам покупателей.

Раздел появился из наблюдения на реальных выгрузках: почти весь негатив периода
лежит в отзывах на маркетплейсах, а не в новостях рынка. В ленте инфоповодов
его не видно и не должно быть видно — жалоба на клейкость гонта не инфоповод.
Но заказчику она важнее половины новостей, поэтому у неё свой раздел.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.metrics_compute import format_int
from services.reviews import (
    LOW_RATING,
    complaints,
    praise_phrases,
    review_overview,
    reviews_by_product,
    select_reviews,
)

# Дальше этого числа список претензий превращается в ленту, которую не читают.
MAX_COMPLAINTS_SHOWN = 60
MAX_PRODUCTS_SHOWN = 25


def _rating_bar(counts: dict[int, int], total: int) -> None:
    """Распределение оценок — строками, а не графиком.

    Пять значений не стоят холста: строка со звёздами и числом читается быстрее
    и не ломается на узком экране.
    """
    if not total:
        return
    for star in (5, 4, 3, 2, 1):
        count = int(counts.get(star, 0))
        share = count / total if total else 0
        st.write(
            f"{'★' * star}{'☆' * (5 - star)} &nbsp; **{count}** "
            f"<span style='color:#888'>({share * 100:.0f}%)</span>",
            unsafe_allow_html=True,
        )
        st.progress(min(1.0, share))


def render_reviews(messages: pd.DataFrame) -> None:
    st.subheader("Отзывы о товаре")

    reviews = select_reviews(messages)
    if reviews.empty:
        st.info(
            "За выбранный период отзывов не найдено. Раздел наполняется "
            "сообщениями с площадок отзывов и маркетплейсов — их отмечает "
            "сама система мониторинга."
        )
        return

    overview = review_overview(messages)
    total = int(overview["reviews"])
    rating_avg = overview["rating_avg"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Отзывов", format_int(total))
    c2.metric(
        "Средняя оценка",
        f"{rating_avg:.2f}" if rating_avg is not None else "—",
        help=(
            f"По {format_int(int(overview['rated']))} отзывам, где покупатель "
            "поставил оценку."
        ),
    )
    c3.metric(
        "Претензий",
        format_int(int(overview["negative"])),
        help=(
            f"Отзывы с оценкой не выше {LOW_RATING:.0f} или размеченные "
            "негативными. Одного признака мало: разметка тональности и оценка "
            "расходятся в обе стороны."
        ),
    )
    c4.metric("Товаров", format_int(int(overview["products"])))

    st.divider()

    left, right = st.columns([1, 1])
    with left:
        st.markdown("**Распределение оценок**")
        _rating_bar(overview["rating_counts"], int(overview["rated"]))
    with right:
        st.markdown("**Что отмечают как плюс**")
        praise = praise_phrases(messages, top_n=8)
        if praise:
            for phrase, count in praise:
                st.write(f"{phrase[:1].upper() + phrase[1:]} — **{count}**")
            st.caption(
                "Плюсы покупатель выбирает галочками из готового списка, "
                "поэтому они складываются в счёт."
            )
        else:
            st.caption("В отзывах периода нет размеченных плюсов товара.")

    st.divider()

    st.markdown("**Претензии покупателей**")
    complaint_rows = complaints(messages)
    if complaint_rows.empty:
        st.success("За период не пришло ни одной претензии к товару.")
    else:
        st.caption(
            "Недостатки покупатели пишут своими словами, каждый по-своему, и за "
            "месяц их считанные десятки. Частотный список по ним был бы набором "
            "случайных обрывков, поэтому претензии показаны поштучно."
        )
        shown = complaint_rows.head(MAX_COMPLAINTS_SHOWN).copy()
        shown["Оценка"] = shown["Оценка"].map(
            lambda v: "—" if pd.isna(v) else f"{float(v):.0f}"
        )
        st.dataframe(
            shown[["Оценка", "Товар", "Претензия", "Дата", "Ссылка"]],
            hide_index=True,
            width="stretch",
            column_config={
                "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="Открыть"),
                "Претензия": st.column_config.TextColumn("Претензия", width="large"),
            },
        )
        if len(complaint_rows) > MAX_COMPLAINTS_SHOWN:
            st.caption(
                f"Показаны первые {MAX_COMPLAINTS_SHOWN} из "
                f"{len(complaint_rows)} — сначала с самой низкой оценкой."
            )

    st.divider()

    st.markdown("**По товарам**")
    products = reviews_by_product(messages)
    if products.empty:
        st.caption("Товар в отзывах не указан.")
        return
    st.dataframe(
        products.head(MAX_PRODUCTS_SHOWN),
        hide_index=True,
        width="stretch",
        column_config={
            "Средняя оценка": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    if len(products) > MAX_PRODUCTS_SHOWN:
        st.caption(
            f"Показаны {MAX_PRODUCTS_SHOWN} товаров из {len(products)} — "
            "сначала те, где больше претензий."
        )
