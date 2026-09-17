# -*- coding: utf-8 -*-
"""Раздел «Данные»: выбор периодов в сайдбаре, загрузка файла и история
периодов (редактирование, скрытие, необратимое удаление выгрузки).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from typing import Any

from services.cached_store import (
    clear_platform_caches,
    delete_period,
    list_periods,
    update_period_metadata,
    update_project,
)
from services.formatting import fmt_period, period_picker_label
from services.import_report import normalization_lines, summarize_import
from services.ingest import IngestError, process_canonical, read_canonical_bytes
from services.metrics_compute import format_int
from services.project_settings import (
    story_build_settings_from_project_settings,
    with_story_build,
)
from services.roles import role_rank
from noise_filter_ui import render_noise_filter_block
from tag_hierarchy_ui import render_tag_hierarchy_block


def _render_clustering_algorithm_settings() -> tuple[float, float, float]:
    """Пороги кластеризации обсуждений в инфоповоды текстом (без сюжетов).

    Работают только для выгрузок БЕЗ готовой разметки сюжетов Brand
    Analytics (обычный CSV, Медиалогия) - для самих BA-выгрузок платформа
    берёт сюжет прямо из данных, эти пороги там ни на что не влияют. Не
    путать с «Что платформа считает инфоповодом» выше: та настройка,
    наоборот, работает только для Brand Analytics — достраивает сюжеты,
    которых не хватает в выгрузке, и отсеивает одиночные публикации. Разные
    выгрузки, разные пороги, поэтому названия и похожи, а смысл — нет.

    Свёрнуто по умолчанию: для большинства загрузок (Brand Analytics) эти
    ползунки вообще не применяются, и держать их развёрнутыми на каждой
    выгрузке — лишний шум на странице.
    """
    with st.expander("Алгоритм: как собирать инфоповоды без сюжетов", expanded=False):
        st.caption(
            "Применяется только к выгрузкам без готовой разметки сюжетов "
            "(обычный CSV, Медиалогия). Для Brand Analytics эти пороги "
            "не используются — там сюжет уже есть в данных."
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            threshold = st.slider(
                "Похожесть текстов при сборке",
                0.10,
                0.60,
                0.30,
                0.01,
                help="Ниже — смелее объединяет разные публикации в один инфоповод.",
            )
        with c2:
            event_gap_hours = st.slider(
                "Разрыв между волнами, часов",
                1.0,
                24.0,
                3.0,
                1.0,
                help="Тишина дольше этого — начинается новая волна той же темы.",
            )
        with c3:
            event_window_hours = st.slider(
                "Макс. окно инфоповода, часов",
                4.0,
                72.0,
                16.0,
                4.0,
                help=(
                    "Потолок длины одной волны, даже без тишины — защита от "
                    "общего тега, который иначе склеит события за несколько "
                    "дней в один инфоповод."
                ),
            )
    return threshold, event_gap_hours, event_window_hours


def render_period_selector(project_id: str) -> tuple[list[str], pd.DataFrame]:
    periods = list_periods(project_id, include_inactive=False)
    if periods.empty:
        st.sidebar.warning("В проекте пока нет загруженных периодов.")
        return [], periods
    labels = {}
    for _, r in periods.iterrows():
        period_id = str(r["period_id"])
        labels[period_id] = period_picker_label(r, fallback=period_id)
    # Раньше по умолчанию открывались три периода — втрое больше данных при
    # каждом заходе. Достаточно последнего; остальные добавляются вручную.
    default = periods["period_id"].astype(str).head(1).tolist()
    selected = st.sidebar.multiselect(
        "Периоды",
        periods["period_id"].astype(str).tolist(),
        default=default,
        format_func=lambda x: labels.get(x, x),
        key=f"period_select_{project_id}",
        help="Добавьте второй период, чтобы появилось сравнение и раздел «Динамика».",
    )
    return selected, periods


def read_uploaded_to_canonical(
    uploaded_file, source_system: str, report: dict | None = None
) -> pd.DataFrame:
    """Чтение загруженного файла тем же кодом, что использует автозагрузка."""
    return read_canonical_bytes(
        uploaded_file.getvalue(), uploaded_file.name, source_system, report=report
    )


def render_import_report(report: dict) -> None:
    """Показать, что платформа поняла в выгрузке, а что нет.

    Синонимов имён колонок не хватит никогда: у одной системы они различаются
    между форматами, а у разных систем совпадают редко. Пока платформа молчит о
    непонятых колонках, потеря обнаруживается случайно и спустя месяцы —
    аудитория у проектов на Медиалогии была нулевой ровно поэтому.
    """
    if not report:
        return

    unknown = report.get("unrecognized") or []
    st.caption(summarize_import(report))

    fixes = normalization_lines(report)
    if fixes:
        with st.expander("Что выправлено при чтении", expanded=False):
            st.caption(
                "Системы мониторинга пишут одно и то же по-разному: HTML-мнемоники "
                "вместо знаков, пробелы в разрядах чисел, экранированные переводы "
                "строки. Это приведено к единому виду, данные не изменились."
            )
            for line in fixes:
                st.write(f"• {line}")

    if not unknown:
        return

    total = sum(int(item["filled"]) for item in unknown)
    st.warning(
        f"Не распознано колонок: {len(unknown)} "
        f"(значений в них: {format_int(total)}). Данные из них в отчёт не попадут."
    )
    with st.expander("Какие колонки не распознаны", expanded=True):
        st.caption(
            "Если среди них есть нужные — сообщите, какая колонка что означает: "
            "добавить её в разбор быстрее, чем искать причину расхождений потом."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Колонка": item["column"],
                        "Заполнено": item["filled"],
                        "Доля": f"{item['share'] * 100:.0f}%",
                        "Примеры значений": item["sample"],
                    }
                    for item in unknown
                ]
            ),
            hide_index=True,
            width="stretch",
        )


def render_story_build_settings(
    project_id: str, project_settings: dict[str, Any] | None
) -> None:
    """Пороги, по которым платформа собирает инфоповоды из сообщений.

    Похожесть текстов и минимум авторов/сообщений — то же самое, что раньше
    было зашито в коде константами. Настройка применяется к СЛЕДУЮЩИМ
    загрузкам: инфоповоды считаются один раз при импорте, а не пересчитываются
    на лету, поэтому смена порога не трогает уже сохранённые периоды.
    """
    current = story_build_settings_from_project_settings(project_settings)
    with st.expander("Что платформа считает инфоповодом", expanded=False):
        st.caption(
            "Настройка применяется к следующим загрузкам. Уже сохранённые "
            "периоды не пересчитываются: чтобы применить новые пороги к "
            "прошлому периоду, загрузите тот же файл повторно."
        )
        with st.form(f"story_build_{project_id}"):
            c1, c2, c3 = st.columns(3)
            with c1:
                min_authors = st.number_input(
                    "Минимум авторов в инфоповоде",
                    min_value=1,
                    max_value=20,
                    value=int(current["min_authors"]),
                    help=(
                        "Инфоповод — это когда об одном пишут разные люди. "
                        "При 3 рассылка одного магазина в список не попадёт."
                    ),
                )
            with c2:
                min_messages = st.number_input(
                    "Минимум сообщений в инфоповоде",
                    min_value=1,
                    max_value=20,
                    value=int(current["min_messages"]),
                    help=(
                        "Одиночная публикация, которую никто не подхватил, "
                        "инфоповодом не считается."
                    ),
                )
            with c3:
                similarity = st.slider(
                    "Насколько похожими должны быть тексты",
                    0.20,
                    0.95,
                    float(current["similarity"]),
                    0.05,
                    help=(
                        "Ниже — платформа смелее склеивает разные публикации в "
                        "один сюжет, выше — оставляет их раздельно. Работает на "
                        "выгрузках Brand Analytics, где часть сюжетов платформа "
                        "достраивает сама. Это не тот же порог, что «Похожесть "
                        "текстов при сборке» в раскрывашке «Алгоритм» ниже: тот "
                        "отвечает за кластеризацию выгрузок без готовых сюжетов "
                        "и для Brand Analytics не используется."
                    ),
                )
            if st.form_submit_button("Сохранить пороги"):
                updated = with_story_build(
                    project_settings,
                    {
                        "similarity": similarity,
                        "min_authors": int(min_authors),
                        "min_messages": int(min_messages),
                    },
                )
                try:
                    update_project(project_id, settings=updated)
                    clear_platform_caches(project_id)
                    st.success("Пороги сохранены.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001 — ошибка видна пользователю
                    st.warning(f"Не удалось сохранить: {exc}")


def render_upload_page(
    project_id: str,
    role: str,
    work_dir: str,
    project_settings: dict[str, Any] | None = None,
) -> None:
    st.header("Загрузка файла")
    if role_rank(role) < role_rank("editor"):
        st.info("Для загрузки файлов нужен доступ редактора или владельца.")
        return

    render_tag_hierarchy_block(project_id)
    render_story_build_settings(project_id, project_settings)
    threshold, event_gap_hours, event_window_hours = (
        _render_clustering_algorithm_settings()
    )

    with st.form("upload_form"):
        period_name = st.text_input(
            "Название периода (необязательно)",
            placeholder="Оставьте пустым — платформа назовёт период по датам ниже",
            help=(
                "Заполняйте только если хотите своё название (например, "
                "«Апрельская волна»). Без названия период подпишется "
                "диапазоном дат из полей «Дата начала»/«Дата окончания» — "
                "тем же способом, что уже используется на графиках."
            ),
        )
        date_col1, date_col2 = st.columns(2)
        with date_col1:
            date_from = st.date_input("Дата начала", value=None, format="DD.MM.YYYY")
        with date_col2:
            date_to = st.date_input("Дата окончания", value=None, format="DD.MM.YYYY")
        source_system = st.selectbox(
            "Источник",
            ["auto", "mediologia", "mediologia_excel", "brand_analytics", "generic"],
            format_func=lambda x: {
                "auto": "Автоопределение",
                "mediologia": "Медиалогия CSV",
                "mediologia_excel": "Медиалогия Excel",
                "brand_analytics": "Brand Analytics",
                "generic": "Универсальный CSV/Excel",
            }.get(x, x),
        )
        uploaded = st.file_uploader(
            "CSV или Excel", type=["csv", "xlsx", "xls", "xlsm"]
        )
        submitted = st.form_submit_button("Обработать и сохранить", type="primary")

    if not submitted:
        return
    if uploaded is None:
        st.error("Загрузите файл.")
        return
    if date_from and date_to and date_from > date_to:
        st.error("Дата начала не может быть позже даты окончания.")
        return

    import_report: dict = {}
    with st.spinner("Читаю файл и привожу к единому формату..."):
        try:
            canonical = read_uploaded_to_canonical(
                uploaded, source_system, report=import_report
            )
        except Exception as exc:
            st.error("Не удалось прочитать файл.")
            st.info(
                "Проверьте, что файл содержит лист/таблицу с сообщениями: дата, текст/сообщение, url/ссылка, источник или автор. "
                "Если в Excel несколько листов, платформа автоматически ищет лист «Сообщения» и пропускает пустые листы."
            )
            st.exception(exc)
            return
    st.success(f"Файл прочитан: {len(canonical):,} строк".replace(",", " "))
    render_import_report(import_report)
    with st.expander("Предпросмотр распознанных колонок", expanded=False):
        st.dataframe(canonical.head(20), width="stretch")
    render_noise_filter_block(canonical)

    with st.spinner("Собираю сообщения, обсуждения и инфоповоды..."):
        try:
            result = process_canonical(
                canonical,
                project_id=project_id,
                source_filename=uploaded.name,
                file_bytes=uploaded.getvalue(),
                period_name=period_name,
                source_system=source_system,
                date_from=date_from,
                date_to=date_to,
                params={
                    "similarity_threshold": float(threshold),
                    "event_gap_hours": float(event_gap_hours),
                    "event_window_hours": float(event_window_hours),
                },
                work_dir=work_dir,
                extra_manifest={"ingest": {"mode": "manual", "role": role}},
            )
        except IngestError as exc:
            st.error(str(exc))
            return

    if result.get("storage_error"):
        st.warning(
            "Обработанные данные сохранены в БД, но сырой файл не удалось положить "
            f"в Storage: {result['storage_error']}"
        )
    st.success(
        f"Период «{result['period_name']}» сохранен: сообщений {result['messages']}, "
        f"инфоповодов {result['events']}."
    )
    clear_platform_caches(project_id)


def render_period_history(project_id: str, role: str) -> None:
    st.header("История периодов")
    if role_rank(role) < role_rank("editor"):
        st.info("Для редактирования истории нужен доступ редактора или владельца.")
        return
    periods = list_periods(project_id, include_inactive=True)
    if periods.empty:
        st.info("Периодов пока нет.")
        return
    view = periods.copy()
    view["Период"] = view.apply(fmt_period, axis=1)
    show = view[
        [
            c
            for c in ["period_name", "Период", "source_filename", "status", "period_id"]
            if c in view.columns
        ]
    ].rename(
        columns={
            "period_name": "Название",
            "source_filename": "Файл",
            "status": "Статус",
            "period_id": "ID",
        }
    )
    event = st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        selection_mode="single-row",
        on_select="rerun",
    )
    rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
    if not rows:
        return
    row = periods.iloc[rows[0]]
    period_id = str(row["period_id"])
    with st.expander("Редактировать период", expanded=True):
        name = st.text_input(
            "Название периода",
            value=str(row.get("period_name") or ""),
            key=f"period_name_{period_id}",
        )

        def to_date(v):
            ts = pd.to_datetime(v, errors="coerce")
            return None if pd.isna(ts) else ts.date()

        date_from = st.date_input(
            "Дата начала",
            value=to_date(row.get("date_from")),
            format="DD.MM.YYYY",
            key=f"date_from_{period_id}",
        )
        date_to = st.date_input(
            "Дата окончания",
            value=to_date(row.get("date_to")),
            format="DD.MM.YYYY",
            key=f"date_to_{period_id}",
        )
        filename = st.text_input(
            "Файл",
            value=str(row.get("source_filename") or ""),
            key=f"filename_{period_id}",
        )
        status = st.selectbox(
            "Статус",
            ["active", "hidden", "archived"],
            index=(
                ["active", "hidden", "archived"].index(
                    str(row.get("status") or "active")
                )
                if str(row.get("status") or "active")
                in ["active", "hidden", "archived"]
                else 0
            ),
            key=f"status_{period_id}",
        )
        comment = st.text_area(
            "Комментарий",
            value=(
                str((row.get("manifest") or {}).get("comment", ""))
                if isinstance(row.get("manifest"), dict)
                else ""
            ),
            key=f"comment_{period_id}",
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Сохранить изменения", key=f"save_period_{period_id}"):
                if date_from and date_to and date_from > date_to:
                    st.error("Дата начала не может быть позже даты окончания.")
                else:
                    update_period_metadata(
                        project_id,
                        period_id,
                        period_name=name,
                        date_from=date_from,
                        date_to=date_to,
                        source_filename=filename,
                        status=status,
                        manifest_updates={"comment": comment},
                    )
                    st.success("Период обновлен.")
                    st.rerun()
        with c2:
            if st.button("Скрыть период", key=f"hide_period_{period_id}"):
                delete_period(project_id, period_id, hard=False)
                st.success("Период скрыт.")
                st.rerun()

        with st.expander("Удалить выгрузку без восстановления", expanded=False):
            st.warning(
                "Удаление выгрузки удалит период и все обработанные таблицы этого периода из platform_table_rows. "
                "Также будут удалены ручные правки, которые явно ссылаются на этот период. "
                "Другие проекты и другие периоды не затрагиваются."
            )
            manifest = (
                row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
            )
            storage_path = str((manifest or {}).get("storage_path") or "").strip()
            if storage_path:
                st.caption(f"Исходный файл в Storage: {storage_path}")
            delete_storage = st.checkbox(
                "Удалить исходный файл из Supabase Storage, если он был сохранен",
                value=True,
                key=f"delete_storage_{period_id}",
            )
            st.caption("Удаление запускается одной кнопкой. Действие необратимо.")
            if st.button(
                "Удалить выгрузку",
                key=f"hard_delete_period_{period_id}",
                type="primary",
            ):
                try:
                    result = delete_period(
                        project_id,
                        period_id,
                        hard=True,
                        delete_storage=delete_storage,
                        cleanup_manual=True,
                    )
                except Exception as exc:
                    st.error("Не удалось удалить выгрузку.")
                    st.exception(exc)
                    return

                manual_count = (
                    int(result.get("manual_rows_deleted") or 0)
                    if isinstance(result, dict)
                    else 0
                )
                table_count = (
                    int(result.get("table_rows_deleted") or 0)
                    if isinstance(result, dict)
                    else 0
                )
                storage_deleted = (
                    bool(result.get("storage_deleted"))
                    if isinstance(result, dict)
                    else False
                )
                mode = str(result.get("mode") or "") if isinstance(result, dict) else ""
                for warning in (
                    (result.get("warnings") or []) if isinstance(result, dict) else []
                ):
                    st.warning(str(warning))
                if (
                    delete_storage
                    and storage_path
                    and not storage_deleted
                    and mode != "soft_fallback"
                ):
                    st.warning(
                        "Выгрузка удалена из базы, но исходный файл в Storage удалить не удалось или он уже отсутствовал."
                    )
                if mode == "soft_fallback":
                    st.warning(
                        "Физическое удаление не завершилось, поэтому период скрыт из интерфейса. Для полной очистки можно повторить удаление позже или выполнить очистку в Supabase."
                    )
                elif mode == "failed":
                    st.error(
                        "Удалить не получилось: период остался без изменений. "
                        "Подробности — в предупреждениях выше; попробуйте позже."
                    )
                else:
                    st.success(
                        f"Выгрузка удалена. Удалено строк данных: {table_count}. Удалено связанных ручных правок: {manual_count}."
                    )
                clear_platform_caches(project_id)
                st.rerun()
