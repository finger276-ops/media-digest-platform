# -*- coding: utf-8 -*-
"""Раздел «Индексы бренда».

Показывает рассчитанные индексы (BPI, NSS, SES, ToneVolumeScore, SOV,
ReachScore, ER, ERR), расшифровку каждого расчёта, динамику по периодам и
настройки. Здесь же загружается выгрузка по категории, без которой нельзя
посчитать SOV и ReachScore.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from services import category_store
from services.brand_metrics import (
    BPI_AVAILABLE_METRICS,
    DEFAULT_BPI_WEIGHTS,
    METRIC_TITLES,
    compute_brand_metrics,
    merge_settings,
    metrics_by_period,
    metrics_to_frame,
)
from services.cached_store import clear_platform_caches, update_project
from services.ingest import IngestError, read_canonical_bytes

METRIC_ORDER = ["BPI", "NSS", "SES", "TVS", "SOV", "ReachScore", "ER", "ERR"]

SOURCE_SYSTEM_OPTIONS = {
    "auto": "Автоопределение",
    "brand_analytics": "Brand Analytics",
    "mediologia": "Медиалогия CSV",
    "mediologia_excel": "Медиалогия Excel",
    "generic": "Универсальный CSV/Excel",
}


def format_metric(card: dict[str, Any]) -> str:
    if not card.get("available"):
        return "—"
    value = float(card["value"])
    return f"{value:+.2f}".replace(".", ",") + " %"


def format_input_value(value: Any) -> str:
    """Одна колонка расшифровки хранит и числа, и пояснения — приводим к тексту."""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        return f"{value:.2f}".replace(".", ",")
    return str(value)


def project_metric_settings(project_settings: dict[str, Any] | None) -> dict[str, Any]:
    return merge_settings((project_settings or {}).get("brand_metrics"))


def _save_metric_settings(
    project_id: str, project_settings: dict[str, Any], new_settings: dict[str, Any]
) -> None:
    settings = dict(project_settings or {})
    settings["brand_metrics"] = new_settings
    update_project(project_id, settings=settings)
    clear_platform_caches(project_id)


# ---------------------------------------------------------------------------
# Карточки метрик
# ---------------------------------------------------------------------------


def _card_label(key: str, card: dict[str, Any]) -> str:
    """Подпись карточки: аббревиатура плюс человеческое название.

    Одна аббревиатура понятна аналитику, но не клиенту, который открыл дашборд.
    """
    code = str(card.get("code") or key)
    title = str(card.get("title") or METRIC_TITLES.get(key, ("", ""))[1])
    return f"{code} · {title}" if title else code


def render_metric_cards(cards: dict[str, dict[str, Any]]) -> None:
    for row_keys in (["BPI", "NSS", "SES", "TVS"], ["SOV", "ReachScore", "ER", "ERR"]):
        columns = st.columns(4)
        for column, key in zip(columns, row_keys):
            card = cards.get(key, {})
            with column:
                st.metric(_card_label(key, card), format_metric(card))
                if not card.get("available"):
                    st.caption(card.get("reason", ""))


def render_metric_details(cards: dict[str, dict[str, Any]]) -> None:
    with st.expander("Как считается каждая метрика", expanded=False):
        for key in METRIC_ORDER:
            card = cards.get(key)
            if not card:
                continue
            st.markdown(f"**{card['code']} — {card['title']}**")
            st.caption(card["hint"])
            st.code(card["formula"], language="text")
            if card["available"] and card["inputs"]:
                rows = [
                    {"Показатель": str(name), "Значение": format_input_value(value)}
                    for name, value in card["inputs"].items()
                    if value is not None
                ]
                if rows:
                    st.dataframe(
                        pd.DataFrame(rows), use_container_width=True, hide_index=True
                    )
                st.markdown(f"Результат: **{format_metric(card)}**")
            else:
                st.info(card["reason"] or "Метрика недоступна для этого периода.")
            st.divider()


# ---------------------------------------------------------------------------
# Динамика
# ---------------------------------------------------------------------------


def render_metrics_dynamics(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    benchmarks: dict[str, dict[str, Any]],
    settings: dict[str, Any],
) -> None:
    if len(period_ids) < 2:
        return

    frame = metrics_by_period(
        messages, period_ids, benchmarks=benchmarks, settings=settings
    )
    if frame.empty:
        return

    labels = {}
    if not periods.empty and "period_id" in periods.columns:
        labels = {
            str(row["period_id"]): str(row.get("period_name") or row["period_id"])
            for _, row in periods.iterrows()
        }
    frame["Период"] = frame["period_id"].map(lambda pid: labels.get(pid, pid))

    metric_columns = [
        col for col in frame.columns if col not in {"period_id", "Период"}
    ]
    available = [col for col in metric_columns if frame[col].notna().any()]
    if not available:
        return

    st.subheader("Динамика индексов")
    default = [col for col in ["BPI", "NSS", "SES"] if col in available] or available[:3]
    chosen = st.multiselect(
        "Метрики на графике", available, default=default, key="brand_metrics_chart"
    )
    if not chosen:
        return

    long = frame.melt(
        id_vars=["Период"], value_vars=chosen, var_name="Метрика", value_name="Значение"
    ).dropna(subset=["Значение"])
    if long.empty:
        return

    chart = (
        alt.Chart(long)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                "Период:N",
                sort=list(frame["Период"]),
                title="",
                axis=alt.Axis(labelAngle=0, labelLimit=140),
            ),
            y=alt.Y("Значение:Q", title="%"),
            color=alt.Color("Метрика:N", title=""),
            tooltip=["Период", "Метрика", alt.Tooltip("Значение:Q", format=".2f")],
        )
        .properties(height=320)
    )
    st.altair_chart(chart, use_container_width=True)
    st.dataframe(
        frame[["Период"] + chosen], use_container_width=True, hide_index=True
    )


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


def render_metric_settings(
    project_id: str, project_settings: dict[str, Any], settings: dict[str, Any]
) -> None:
    with st.expander("Настройка индекса BPI", expanded=False):
        st.caption(
            "BPI собирается из выбранных метрик с весами. Сумма весов нормализуется "
            "автоматически, поэтому можно задавать любые пропорции."
        )
        current_weights = settings.get("bpi_weights", DEFAULT_BPI_WEIGHTS)

        with st.form("brand_metrics_settings"):
            chosen = st.multiselect(
                "Метрики в составе индекса",
                list(BPI_AVAILABLE_METRICS.keys()),
                default=[k for k in current_weights if k in BPI_AVAILABLE_METRICS]
                or list(DEFAULT_BPI_WEIGHTS.keys()),
                format_func=lambda k: f"{METRIC_TITLES.get(k, (k, k))[0]} — {METRIC_TITLES.get(k, (k, k))[1]}",
            )
            weights: dict[str, float] = {}
            if chosen:
                columns = st.columns(min(4, len(chosen)))
                for index, key in enumerate(chosen):
                    with columns[index % len(columns)]:
                        weights[key] = st.number_input(
                            f"Вес {METRIC_TITLES.get(key, (key, key))[0]}",
                            min_value=0.0,
                            max_value=1.0,
                            value=float(current_weights.get(key, 0.2)),
                            step=0.05,
                            key=f"weight_{key}",
                        )

            nss_basis = st.radio(
                "NSS считать по",
                ["events", "messages"],
                index=0 if settings.get("nss_basis", "events") == "events" else 1,
                format_func=lambda v: (
                    "инфоповодам — баланс повестки"
                    if v == "events"
                    else "сообщениям — тогда совпадёт с ToneVolumeScore"
                ),
                horizontal=False,
            )
            sov_basis = st.radio(
                "SOV считать по",
                ["messages", "reach"],
                index=0 if settings.get("sov_basis", "messages") == "messages" else 1,
                format_func=lambda v: (
                    "упоминаниям — как в примере гайда"
                    if v == "messages"
                    else "охвату — как в формуле гайда"
                ),
                horizontal=False,
            )
            submitted = st.form_submit_button("Сохранить настройки", type="primary")

        if submitted:
            scales = {BPI_AVAILABLE_METRICS[k] for k in weights}
            if len(scales) > 1:
                st.warning(
                    "В индекс попали метрики разных шкал: баланс тональности живёт в "
                    "диапазоне −100…100, а доли — в 0…100. Значение останется "
                    "сравнимым между периодами одного проекта, но не с отраслевыми "
                    "бенчмарками."
                )
            _save_metric_settings(
                project_id,
                project_settings,
                {
                    "bpi_weights": weights or dict(DEFAULT_BPI_WEIGHTS),
                    "nss_basis": nss_basis,
                    "sov_basis": sov_basis,
                },
            )
            st.success("Настройки сохранены.")
            st.rerun()


# ---------------------------------------------------------------------------
# Категорийная выгрузка
# ---------------------------------------------------------------------------


def render_category_upload(
    project_id: str, periods: pd.DataFrame, period_ids: list[str], role_can_edit: bool
) -> None:
    st.subheader("Выгрузка по категории")
    st.caption(
        "SOV и ReachScore сравнивают бренд с конкурентами, поэтому им нужна выгрузка "
        "по всей категории. Платформа сохранит только агрегаты по брендам — "
        "сообщения конкурентов в базу не попадают."
    )

    try:
        saved = category_store.load_benchmarks(project_id, period_ids)
    except Exception as exc:  # noqa: BLE001
        st.warning(
            "Таблица категорийных бенчмарков недоступна. Выполните в Supabase "
            "скрипт sql/platform_brand_metrics_schema.sql."
        )
        st.caption(f"Техническая ошибка: {exc}")
        return

    if saved:
        for period_id, record in saved.items():
            label = period_id
            if not periods.empty and "period_id" in periods.columns:
                match = periods[periods["period_id"].astype(str) == str(period_id)]
                if not match.empty:
                    label = str(match.iloc[0].get("period_name") or period_id)
            with st.expander(f"Категория за период «{label}»", expanded=False):
                frame = pd.DataFrame(record.get("brands") or [])
                if not frame.empty:
                    view = frame.rename(
                        columns={
                            "brand": "Бренд",
                            "messages": "Сообщения",
                            "audience": "Аудитория",
                            "reach": "Охват",
                            "engagement": "Вовлечённость",
                            "is_own": "Наш бренд",
                        }
                    )
                    st.dataframe(view, use_container_width=True, hide_index=True)
                st.caption(
                    f"Файл: {record.get('source_filename') or '—'} · "
                    f"обновлено: {str(record.get('updated_at') or '')[:16].replace('T', ' ')}"
                )
                if role_can_edit and st.button(
                    "Удалить данные категории", key=f"del_bench_{period_id}"
                ):
                    category_store.delete_benchmark(project_id, period_id)
                    st.success("Данные удалены.")
                    st.rerun()

    if not role_can_edit:
        return

    st.markdown("**Загрузить выгрузку по категории**")
    if periods.empty:
        st.info("Сначала загрузите обычную выгрузку проекта — к ней привяжется категория.")
        return

    period_options = {
        str(row["period_id"]): str(row.get("period_name") or row["period_id"])
        for _, row in periods.iterrows()
    }
    target_period = st.selectbox(
        "К какому периоду относится выгрузка",
        list(period_options.keys()),
        format_func=lambda pid: period_options.get(pid, pid),
        key="category_target_period",
    )
    source_system = st.selectbox(
        "Формат выгрузки",
        list(SOURCE_SYSTEM_OPTIONS.keys()),
        format_func=lambda key: SOURCE_SYSTEM_OPTIONS[key],
        key="category_source_system",
    )
    uploaded = st.file_uploader(
        "CSV или Excel по всей категории",
        type=["csv", "xlsx", "xls", "xlsm"],
        key="category_file",
    )
    if uploaded is None:
        return

    try:
        table = read_canonical_bytes(uploaded.getvalue(), uploaded.name, source_system)
    except IngestError as exc:
        st.error(str(exc))
        return

    st.success(f"Файл прочитан: {len(table):,} строк".replace(",", " "))

    candidates = category_store.candidate_brand_columns(table)
    mode = st.radio(
        "Как в файле различаются бренды",
        [category_store.BRAND_MODE_COLUMNS, category_store.BRAND_MODE_VALUES],
        format_func=lambda m: (
            "Каждый бренд — отдельная колонка (обычный вариант Brand Analytics)"
            if m == category_store.BRAND_MODE_COLUMNS
            else "Название бренда записано значением в одной колонке"
        ),
        key="category_brand_mode",
    )

    if mode == category_store.BRAND_MODE_COLUMNS:
        brand_columns = st.multiselect(
            "Колонки брендов",
            candidates or list(table.columns),
            default=candidates[:12],
            key="category_brand_columns",
        )
        brands = category_store.aggregate_by_brand_columns(table, brand_columns)
        brand_source = ", ".join(brand_columns)
    else:
        brand_column = st.selectbox(
            "Колонка с названием бренда",
            candidates or list(table.columns),
            key="category_brand_column",
        )
        brands = category_store.aggregate_by_brand_values(table, brand_column)
        brand_source = str(brand_column)

    if brands is None or brands.empty:
        st.warning("По выбранным колонкам не нашлось ни одного бренда с данными.")
        return

    own_brand = st.selectbox(
        "Какой из брендов наш",
        brands["brand"].astype(str).tolist(),
        key="category_own_brand",
    )
    brands = category_store.mark_own_brand(brands, own_brand)

    st.dataframe(
        brands.rename(
            columns={
                "brand": "Бренд",
                "messages": "Сообщения",
                "audience": "Аудитория",
                "reach": "Охват",
                "engagement": "Вовлечённость",
                "is_own": "Наш бренд",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    if st.button("Сохранить данные категории", type="primary"):
        try:
            category_store.save_benchmark(
                project_id=project_id,
                period_id=target_period,
                brands=brands,
                own_brand=own_brand,
                brand_mode=mode,
                brand_source=brand_source,
                source_filename=uploaded.name,
                messages_total=int(len(table)),
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Не удалось сохранить: {exc}")
            return
        st.success("Данные категории сохранены — SOV и ReachScore посчитаются.")
        st.rerun()


# ---------------------------------------------------------------------------
# Точка входа раздела
# ---------------------------------------------------------------------------


def render_brand_metrics_page(
    project_id: str,
    project_settings: dict[str, Any],
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    selected_period_ids: list[str],
    *,
    role_can_edit: bool = False,
) -> None:
    st.subheader("Индексы бренда")
    st.caption(
        "Метрики считаются автоматически по выбранным периодам. Формулы и входные "
        "числа раскрываются под карточками — цифру всегда можно проверить."
    )

    settings = project_metric_settings(project_settings)

    benchmarks: dict[str, dict[str, Any]] = {}
    try:
        benchmarks = category_store.load_benchmarks(project_id, selected_period_ids)
    except Exception:  # noqa: BLE001 - раздел работает и без категорийных данных
        benchmarks = {}

    cards = compute_brand_metrics(
        messages,
        benchmark=category_store.merged_benchmark(benchmarks),
        settings=settings,
    )

    render_metric_cards(cards)
    render_metric_details(cards)

    table = metrics_to_frame(cards)
    if not table.empty:
        st.dataframe(table, use_container_width=True, hide_index=True)
        st.download_button(
            "Скачать метрики в CSV",
            table.to_csv(index=False).encode("utf-8-sig"),
            file_name="brand_metrics.csv",
            mime="text/csv",
        )

    render_metrics_dynamics(messages, periods, selected_period_ids, benchmarks, settings)

    if role_can_edit:
        render_metric_settings(project_id, project_settings, settings)
    st.divider()
    render_category_upload(project_id, periods, selected_period_ids, role_can_edit)
