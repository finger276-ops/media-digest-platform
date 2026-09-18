# -*- coding: utf-8 -*-
"""Раздел «Индексы бренда».

Показывает рассчитанные индексы (BPI, NSS, SES, ToneVolumeScore, SOV,
ReachScore, ER, ERR), расшифровку каждого расчёта, динамику по периодам и
настройки. Здесь же загружается выгрузка по категории, без которой нельзя
посчитать SOV и ReachScore.
"""

from __future__ import annotations

import json
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
from metric_cards_ui import (
    DELTA_NEUTRAL,
    DELTA_NORMAL,
    metric_card,
    render_metric_row,
)
from services.brand_metrics import metric_direction
from services.cached_store import (
    ManualEditConflict,
    cache_version,
    clear_platform_caches,
    load_table,
    update_project,
)
from services.ingest import IngestError, read_canonical_bytes
from services.metric_notes import (
    load_note_versions,
    load_notes,
    period_key,
    save_note,
)
from services.period_comparison import ordered_period_ids, previous_period_id
from services.project_settings import category_brands_from_project_settings
from services.chart_style import LINE_INTERPOLATE, PERIOD_AXIS, fixed_color_scale

METRIC_ORDER = ["BPI", "NSS", "SES", "TVS", "SOV", "ReachScore", "ER", "ERR"]

# Домен на весь набор метрик, а не только на выбранные аналитиком: тогда
# цвет BPI не меняется в зависимости от того, что ещё отмечено в multiselect.
METRIC_COLOR_SCALE = fixed_color_scale(METRIC_ORDER)

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


@st.cache_data(show_spinner=False, max_entries=6, ttl=900)
def _cached_previous_metrics(
    project_id: str, period_id: str, fingerprint: str, data_version: int
) -> dict[str, float]:
    """Значения метрик прошлого периода — для динамики в карточках.

    Данные прошлого периода в дашборд не загружаются: он показывает выбранное.
    Ради одной строки изменения тянуть их каждый раз дорого, поэтому результат
    кешируется — так же, как дельты в шапке «Обзора».

    Отпечаток настроек в ключе не украшение: веса BPI и разметка брендов меняют
    значения, и без него аналитик, поправивший веса, сравнивал бы новое число
    со старым.
    """
    messages = load_table(project_id, [period_id], "messages")
    if messages is None or messages.empty:
        return {}
    settings, own, competitors = json.loads(fingerprint)
    benchmark = category_store.benchmark_from_messages(messages, own, competitors)
    cards = compute_brand_metrics(messages, benchmark=benchmark, settings=settings)
    return {
        str(card["code"]): float(card["value"])
        for card in cards.values()
        if card.get("available") and card.get("value") is not None
    }


def previous_period_metrics(
    project_id: str,
    period_id: str | None,
    settings: dict[str, Any],
    brand_map: dict[str, list[str]],
) -> dict[str, float]:
    if not project_id or not period_id:
        return {}
    fingerprint = json.dumps(
        [settings, brand_map.get("own", []), brand_map.get("competitors", [])],
        ensure_ascii=False,
        sort_keys=True,
    )
    try:
        return _cached_previous_metrics(
            str(project_id),
            str(period_id),
            fingerprint,
            cache_version(project_id, "data"),
        )
    except Exception:  # noqa: BLE001 — динамика не критична для раздела
        return {}


def _card_label(key: str, card: dict[str, Any]) -> str:
    """Подпись карточки: аббревиатура плюс человеческое название.

    Одна аббревиатура понятна аналитику, но не клиенту, который открыл дашборд.
    """
    code = str(card.get("code") or key)
    title = str(card.get("title") or METRIC_TITLES.get(key, ("", ""))[1])
    return f"{code} · {title}" if title else code


def _metric_delta(
    card: dict[str, Any], previous: dict[str, float] | None
) -> tuple[str | None, str]:
    """Изменение к прошлому периоду и цвет для него.

    Цвет показывает направление движения, а не приговор: толкование даёт
    аналитик в столбце «Вывод». Метрика без заданного направления остаётся
    серой — платформа не берётся судить о том, чего не знает.
    """
    if not previous or not card.get("available"):
        return None, DELTA_NEUTRAL
    code = str(card.get("code") or "")
    before = previous.get(code)
    value = card.get("value")
    if before is None or value is None:
        return None, DELTA_NEUTRAL
    change = float(value) - float(before)
    if abs(change) < 0.005:
        return None, DELTA_NEUTRAL
    color = DELTA_NORMAL if metric_direction(code) == "up" else DELTA_NEUTRAL
    return f"{change:+.2f} п.п.".replace(".", ","), color


def render_metric_cards(
    cards: dict[str, dict[str, Any]],
    previous: dict[str, float] | None = None,
) -> None:
    unavailable: list[tuple[str, str]] = []
    row = []
    for key in METRIC_ORDER:
        card = cards.get(key, {})
        delta, color = _metric_delta(card, previous)
        row.append(
            metric_card(
                _card_label(key, card),
                format_metric(card),
                delta=delta,
                delta_color=color,
                help_text=str(card.get("hint") or ""),
            )
        )
        if not card.get("available") and card.get("reason"):
            unavailable.append((_card_label(key, card), str(card["reason"])))
    render_metric_row(row, columns=4)
    # Причины вынесены под полосу: внутри карточки длинный текст ломает её
    # высоту, и ряд перестаёт быть рядом.
    for label, reason in unavailable:
        st.caption(f"{label}: {reason}")


def render_metric_conclusions(
    project_id: str,
    cards: dict[str, dict[str, Any]],
    period_ids: list[str],
    *,
    role_can_edit: bool = False,
) -> None:
    """Таблица метрик с выводом аналитика вместо формулы.

    Формулы с экрана убраны намеренно: это собственная методика платформы, а не
    то, что заказчик должен читать в отчёте. Цифру он получает вместе с
    выводом, а устройство расчёта — предмет отдельного разговора, если спросит.

    Вывод пишет человек. «NSS +4,35%» — это число, а не мысль: что оно значит
    для бренда, зависит от рынка, от событий периода и от того, чего компания
    добивалась. Машине такой вывод не составить.
    """
    notes = load_notes(project_id, period_ids)
    table = metrics_to_frame(cards, notes)
    if table.empty:
        return

    if not role_can_edit:
        st.dataframe(table, width="stretch", hide_index=True)
        _render_metrics_download(table)
        return

    st.caption(
        "Столбец «Вывод» заполняется вручную: что метрика означает для бренда. "
        "Текст сохраняется для выбранного периода и попадает в выгрузку."
    )
    editor_key = f"metric_notes_{project_id}_{period_key(period_ids)}"
    # Версии выводов замораживаются при первом показе таблицы: пока аналитик
    # пишет, кеш с TTL может подтянуть чужую правку, и проверка версии при
    # сохранении сравнила бы «свежее со свежим», пропустив перезапись.
    versions_key = f"manual_versions::{editor_key}"
    if editor_key not in st.session_state or versions_key not in st.session_state:
        st.session_state[versions_key] = load_note_versions(project_id, period_ids)
    versions = st.session_state[versions_key]
    edited = st.data_editor(
        table,
        width="stretch",
        hide_index=True,
        key=editor_key,
        column_config={
            "Вывод": st.column_config.TextColumn(
                "Вывод аналитика",
                width="large",
                help="Что эта метрика говорит о бренде в выбранном периоде.",
            )
        },
        disabled=["Метрика", "Название", "Значение", "Статус"],
    )

    # Сохраняется только то, что изменилось: data_editor возвращает весь кадр
    # на каждой перерисовке, и запись всех строк подряд поднимала бы восемь
    # обращений к базе на каждое нажатие в любом месте страницы.
    changed = 0
    conflict = False
    for _, row in edited.iterrows():
        code = str(row.get("Метрика") or "").strip()
        new_note = str(row.get("Вывод") or "").strip()
        if not code or new_note == str(notes.get(code, "") or "").strip():
            continue
        try:
            save_note(
                project_id,
                period_ids,
                code,
                new_note,
                expected_updated_at=versions.get(code),
            )
            changed += 1
        except ManualEditConflict:
            conflict = True
        except Exception:  # noqa: BLE001 — вывод не стоит падения раздела
            st.warning(f"Не удалось сохранить вывод по метрике {code}.")
    if conflict:
        st.error(
            "Вывод только что изменил другой редактор — сохранение отменено, "
            "чтобы не затереть его текст. Таблица показывает свежую версию "
            "после обновления."
        )
        clear_platform_caches(project_id)
        st.session_state.pop(versions_key, None)
        # Несохранённый текст остался бы в состоянии таблицы и ушёл бы в базу
        # на следующей перерисовке — уже без предупреждения.
        try:
            st.session_state.pop(editor_key, None)
        except Exception:  # noqa: BLE001 — сброс виджета не стоит падения
            pass
    elif changed:
        # Свои сохранения сдвинули версии: замораживаем заново от свежего
        # снимка, иначе повторная правка той же ячейки увидела бы ложный
        # конфликт с самим собой.
        st.session_state.pop(versions_key, None)
        st.caption(f"Сохранено выводов: {changed}.")

    _render_metrics_download(edited)


def _render_metrics_download(table) -> None:
    st.download_button(
        "Скачать метрики в CSV",
        table.to_csv(index=False).encode("utf-8-sig"),
        file_name="brand_metrics.csv",
        mime="text/csv",
    )


def render_metric_details(cards: dict[str, dict[str, Any]]) -> None:
    """Раскрытие формул — только для владельца платформы, в настройках.

    С клиентского экрана убрано: методика расчёта не то, что заказчик читает
    сам. Но аналитику проверить цифру нужно, поэтому блок остался доступен
    там, где настраиваются веса.
    """
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
                        pd.DataFrame(rows), width="stretch", hide_index=True
                    )
                st.markdown(f"Результат: **{format_metric(card)}**")
            else:
                st.info(card["reason"] or "Метрика недоступна для этого периода.")
            st.divider()


# ---------------------------------------------------------------------------
# Динамика
# ---------------------------------------------------------------------------


def render_metrics_dynamics(
    project_id: str,
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    benchmarks: dict[str, dict[str, Any]],
    settings: dict[str, Any],
) -> None:
    all_period_ids = (
        [str(x) for x in periods["period_id"].tolist()]
        if not periods.empty and "period_id" in periods.columns
        else list(period_ids)
    )
    # Динамика по умолчанию — только по периодам, выбранным в боковой панели:
    # так дешевле (не тянет messages периодов, которые аналитик сейчас не
    # смотрит) и предсказуемее (график не меняется сам по себе от того, что
    # в проект добавили период). Полную историю можно включить явно — ниже.
    if len(period_ids) < 2 and len(all_period_ids) < 2:
        return

    labels = {}
    if not periods.empty and "period_id" in periods.columns:
        labels = {
            str(row["period_id"]): str(row.get("period_name") or row["period_id"])
            for _, row in periods.iterrows()
        }

    # Динамика нужна не всегда: на экране с карточками и выводами она занимает
    # место, а смотрят её, когда вопрос именно в движении.
    with st.expander("Динамика индексов", expanded=False):
        use_all_periods = False
        if len(all_period_ids) > len(period_ids):
            use_all_periods = st.checkbox(
                f"Учесть все периоды проекта ({len(all_period_ids)}), а не только "
                f"выбранные в боковой панели ({len(period_ids)})",
                value=False,
                key=f"brand_metrics_chart_all_periods_{project_id}",
                help=(
                    "Подгрузит сообщения периодов, которых сейчас нет в боковой "
                    "панели — первый раз это займёт время, дальше берётся из кеша."
                ),
            )

        if use_all_periods:
            work_messages = messages
            missing = [pid for pid in all_period_ids if pid not in set(period_ids)]
            if missing:
                with st.spinner(f"Загружаю сообщения ещё {len(missing)} периодов..."):
                    extra = load_table(project_id, missing, "messages")
                if not extra.empty:
                    work_messages = pd.concat(
                        [work_messages, extra], ignore_index=True, sort=False
                    )
            active_period_ids = all_period_ids
        else:
            work_messages = messages
            active_period_ids = period_ids

        if len(active_period_ids) < 2:
            st.caption("Нужно минимум два периода, чтобы построить динамику.")
            return

        # Хронологический порядок, а не порядок выбора в боковой панели. График
        # динамики рисует линию между соседними точками, и при выборе «3 апреля,
        # 1 апреля, 2 апреля» эта линия показывает движение, которого не было.
        ordered = ordered_period_ids(periods, active_period_ids)
        frame = metrics_by_period(
            work_messages, ordered, benchmarks=benchmarks, settings=settings
        )
        if frame.empty:
            st.caption("Недостаточно данных для динамики.")
            return
        frame["Период"] = frame["period_id"].map(lambda pid: labels.get(pid, pid))

        metric_columns = [
            col for col in frame.columns if col not in {"period_id", "Период"}
        ]
        available = [col for col in metric_columns if frame[col].notna().any()]
        if not available:
            st.caption("Недостаточно данных для динамики.")
            return

        default = (
            [col for col in ["BPI", "NSS", "SES"] if col in available] or available[:3]
        )
        chosen = st.multiselect(
            "Метрики на графике", available, default=default, key="brand_metrics_chart"
        )
        if not chosen:
            return

        long = frame.melt(
            id_vars=["Период"],
            value_vars=chosen,
            var_name="Метрика",
            value_name="Значение",
        ).dropna(subset=["Значение"])
        if long.empty:
            return

        chart = (
            alt.Chart(long)
            .mark_line(point=True, interpolate=LINE_INTERPOLATE)
            .encode(
                x=alt.X(
                    "Период:N",
                    sort=list(frame["Период"]),
                    title="",
                    axis=PERIOD_AXIS,
                ),
                y=alt.Y("Значение:Q", title="%"),
                color=alt.Color("Метрика:N", scale=METRIC_COLOR_SCALE, title=""),
                tooltip=["Период", "Метрика", alt.Tooltip("Значение:Q", format=".2f")],
            )
            .properties(height=320)
        )
        st.altair_chart(chart, width="stretch")
        st.dataframe(frame[["Период"] + chosen], width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


def render_brand_map_settings(
    project_id: str,
    project_settings: dict[str, Any],
    messages: pd.DataFrame,
    brand_map: dict[str, list[str]],
) -> None:
    """Разметка тегов: где свои бренды, где конкуренты, где не бренд.

    SOV и ReachScore сравнивают бренд с категорией, и до сих пор для этого
    требовалась отдельная выгрузка по всей категории. В категорийном
    мониторинге она избыточна: конкуренты уже размечены тегами в той же
    выгрузке. Не хватало одного — знания, какой тег бренд, а какой
    аналитический разрез. Отличить «Docke» от «Монтажа» по названию машина не
    может, это знание о рынке.

    Своих брендов несколько: головной, дочерние, отдельные марки. Доля голоса
    считается для группы целиком.
    """
    with st.expander("Бренды категории: свои и конкуренты", expanded=False):
        counts = category_store.brand_counts_from_tags(messages)
        if counts.empty:
            st.caption(
                "В выгрузке нет тегов, по которым можно разделить бренды. "
                "Для SOV и ReachScore загрузите выгрузку по категории ниже."
            )
            return

        st.caption(
            "Теги выгрузки, отмеченные как бренды, заменяют отдельную выгрузку "
            "по категории: SOV и ReachScore считаются прямо по этим данным. "
            "Неотмеченные теги остаются аналитическими разрезами и в расчёт "
            "не идут."
        )
        options = [str(x) for x in counts.index]
        labels = {name: f"{name} — {int(counts[name])}" for name in options}

        with st.form(f"brand_map_{project_id}"):
            own = st.multiselect(
                "Наши бренды",
                options,
                default=[x for x in brand_map["own"] if x in options],
                format_func=lambda x: labels.get(x, x),
                placeholder="Выберите теги",
                help=(
                    "Головной бренд и всё, что относится к группе: дочерние "
                    "компании, отдельные марки. Их упоминания складываются."
                ),
            )
            competitors = st.multiselect(
                "Бренды конкурентов",
                [x for x in options if x not in set(own)],
                default=[
                    x
                    for x in brand_map["competitors"]
                    if x in options and x not in set(own)
                ],
                format_func=lambda x: labels.get(x, x),
                placeholder="Выберите теги",
                help="Остальные бренды категории — знаменатель доли голоса.",
            )
            if st.form_submit_button("Сохранить разметку брендов"):
                updated = dict(project_settings or {})
                updated["category_brands"] = {
                    "own": list(own),
                    "competitors": list(competitors),
                }
                try:
                    update_project(project_id, settings=updated)
                    clear_platform_caches(project_id)
                    st.success("Разметка сохранена.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"Не удалось сохранить: {exc}")

        if brand_map["own"] and not brand_map["competitors"]:
            st.caption(
                "Отмечены только свои бренды: доля голоса выйдет 100%, пока не "
                "отмечены конкуренты."
            )
        elif brand_map["competitors"] and not brand_map["own"]:
            st.caption(
                "Не отмечен ни один свой бренд — SOV и ReachScore считать не от чего."
            )


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


def render_category_source_notice(
    benchmark: dict[str, Any] | None, brand_map: dict[str, list[str]]
) -> None:
    """Сказать, откуда взялась категория для SOV и ReachScore.

    Источников два, и загруженная выгрузка главнее разметки. Без этой подписи
    аналитик, который только что разметил бренды, видел бы прежние числа и не
    понимал, почему разметка ничего не изменила: её перебивает выгрузка,
    загруженная когда-то раньше.
    """
    if not benchmark:
        return
    brands = benchmark.get("brands") or []
    if str(benchmark.get("source") or "") == "project_tags":
        own = ", ".join(benchmark.get("own_brands") or []) or "—"
        rivals = len(brands) - len(benchmark.get("own_brands") or [])
        st.caption(
            f"Категория взята из тегов выгрузки: свои — {own}; "
            f"конкурентов — {rivals}."
        )
        return
    note = f"Категория взята из загруженной выгрузки: брендов — {len(brands)}."
    if brand_map.get("own") or brand_map.get("competitors"):
        note += (
            " Разметка тегов при этом не используется: загруженная выгрузка "
            "полнее и потому главнее."
        )
    st.caption(note)


def render_category_upload(
    project_id: str, periods: pd.DataFrame, period_ids: list[str], role_can_edit: bool
) -> None:
    st.caption(
        "Если конкурентов нет в выгрузке проекта — например, тема мониторинга "
        "настроена только на свой бренд, — их можно принести отдельной "
        "выгрузкой по всей категории. Платформа сохранит только агрегаты по "
        "брендам: сообщения конкурентов в базу не попадают."
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
            # Рамка, а не раскрывашка: весь блок теперь сам лежит в
            # раскрывашке, а вложенные Streamlit не допускает.
            with st.container(border=True):
                st.markdown(f"**Категория за период «{label}»**")
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
                    st.dataframe(view, width="stretch", hide_index=True)
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
            placeholder="Выберите колонки",
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
        width="stretch",
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
# Диапазон дат
# ---------------------------------------------------------------------------


def _date_range_filter(
    messages: pd.DataFrame, project_id: str
) -> tuple[pd.DataFrame, bool]:
    """Сузить сообщения до произвольного диапазона дат внутри выбранных периодов.

    Карточки считались одним числом на весь текущий выбор периодов целиком -
    посмотреть "как дела за эту неделю" внутри длинного периода было нельзя.
    Диапазон необязательный: по умолчанию открыт на весь выбор и ничего не
    меняет, пока аналитик не сузит его сам.

    Возвращает (сообщения, сужен ли диапазон) - второе нужно вызывающему,
    чтобы честно спрятать "изменение к предыдущему периоду" (для
    произвольного куска периода "предыдущий период" не определён) и
    предупредить про SOV/ReachScore (см. render_brand_metrics_page).
    """
    if (
        not isinstance(messages, pd.DataFrame)
        or messages.empty
        or "datetime" not in messages.columns
    ):
        return messages, False
    dt = pd.to_datetime(messages["datetime"], errors="coerce")
    valid = dt.dropna()
    if valid.empty:
        return messages, False
    min_date, max_date = valid.min().date(), valid.max().date()
    if min_date == max_date:
        return messages, False

    with st.expander("Диапазон дат", expanded=False):
        st.caption(
            f"По умолчанию — весь выбранный период "
            f"({min_date.strftime('%d.%m.%Y')}–{max_date.strftime('%d.%m.%Y')}). "
            "Сузьте, чтобы посмотреть индексы только за часть периода — "
            "например, за одну неделю."
        )
        col1, col2 = st.columns(2)
        with col1:
            start = st.date_input(
                "С",
                value=min_date,
                min_value=min_date,
                max_value=max_date,
                format="DD.MM.YYYY",
                key=f"brand_metrics_range_from_{project_id}",
            )
        with col2:
            end = st.date_input(
                "По",
                value=max_date,
                min_value=min_date,
                max_value=max_date,
                format="DD.MM.YYYY",
                key=f"brand_metrics_range_to_{project_id}",
            )
        if start > end:
            st.warning("Начало диапазона позже конца — показан весь период.")
            return messages, False
        if start == min_date and end == max_date:
            return messages, False
        mask = (dt.dt.date >= start) & (dt.dt.date <= end)
        filtered = messages[mask.fillna(False)]
        st.caption(
            f"Выбрано: {len(filtered):,} сообщений из {len(messages):,}.".replace(
                ",", " "
            )
        )
        return filtered, True


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
        "Метрики считаются автоматически по выбранным периодам. Что каждая из "
        "них означает для бренда — в столбце «Вывод»."
    )

    scoped_messages, range_active = _date_range_filter(messages, project_id)

    settings = project_metric_settings(project_settings)

    benchmarks: dict[str, dict[str, Any]] = {}
    try:
        benchmarks = category_store.load_benchmarks(project_id, selected_period_ids)
    except Exception:  # noqa: BLE001 - раздел работает и без категорийных данных
        benchmarks = {}

    # Бренды конкурентов чаще всего уже размечены тегами в самой выгрузке
    # проекта: у RUFLEX это Docke и Tegola, у Кнауфа свои. Тогда отдельная
    # загрузка по категории не нужна — она просила бы те же данные второй раз.
    brand_map = category_brands_from_project_settings(project_settings)
    benchmark = category_store.benchmark_from_messages(
        scoped_messages, brand_map["own"], brand_map["competitors"]
    )
    # Загруженная выгрузка по категории главнее: в ней есть бренды, которых нет
    # в теговой разметке проекта, то есть картина рынка шире. Она же не знает
    # про сужение диапазона дат — выгружается на период целиком (см. caption
    # ниже, когда diапазон активен и используется именно этот источник).
    if benchmarks:
        benchmark = category_store.merged_benchmark(benchmarks) or benchmark

    cards = compute_brand_metrics(scoped_messages, benchmark=benchmark, settings=settings)

    # «Предыдущий период» не определён для произвольного куска периода —
    # честнее спрятать дельту, чем сравнить сужение с чем-то, что ему не
    # соответствует.
    previous = (
        None
        if range_active
        else previous_period_metrics(
            project_id,
            previous_period_id(periods, selected_period_ids),
            settings,
            brand_map,
        )
    )
    render_metric_cards(cards, previous)
    if previous:
        st.caption("Изменения — к предыдущему периоду.")
    elif range_active:
        st.caption(
            "Изменение к предыдущему периоду не показано: выбран произвольный "
            "диапазон дат внутри периода."
        )
    render_category_source_notice(benchmark, brand_map)
    if range_active and benchmarks:
        st.caption(
            "SOV и ReachScore по-прежнему сравниваются с категорией за весь "
            "период загрузки — выгрузка по категории не разбита по дням, "
            "поэтому сужение диапазона дат их не меняет."
        )
    render_metric_conclusions(
        project_id, cards, selected_period_ids, role_can_edit=role_can_edit
    )

    # Полные, не суженные сообщения: график динамики остаётся по периодам
    # целиком (это отдельная, более крупная переделка — см. обсуждение).
    render_metrics_dynamics(
        project_id, messages, periods, selected_period_ids, benchmarks, settings
    )

    if role_can_edit:
        render_brand_map_settings(project_id, project_settings, messages, brand_map)
        # Формулы остались доступны там, где настраиваются веса: аналитику
        # проверить цифру нужно, заказчику — нет.
        render_metric_details(cards)
        render_metric_settings(project_id, project_settings, settings)
        # Запасной путь, а не основной: у большинства проектов конкуренты уже
        # размечены тегами в самой выгрузке. Развёрнутым этот блок занимал
        # экран под задачу, которая возникает редко.
        with st.expander("Выгрузка по категории отдельным файлом", expanded=False):
            render_category_upload(
                project_id, periods, selected_period_ids, role_can_edit
            )
