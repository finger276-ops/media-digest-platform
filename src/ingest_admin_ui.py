# -*- coding: utf-8 -*-
"""Раздел «Автозагрузка»: очередь файлов от n8n и настройка источников.

Страница доступна редактору и владельцу проекта. Она показывает, какие выгрузки
пришли автоматически, что уже загружено, что упало с ошибкой, и позволяет
обработать очередь вручную, не дожидаясь планового запуска воркера.
"""

from __future__ import annotations

import time
import traceback
from typing import Any

import pandas as pd
import streamlit as st

import platform_store as store
from services import ingest_queue as queue
from services.cached_store import clear_platform_caches
from services.ingest import IngestError, file_sha256, ingest_file_bytes

SOURCE_SYSTEM_LABELS = {
    "auto": "Автоопределение",
    "mediologia": "Медиалогия CSV",
    "mediologia_excel": "Медиалогия Excel",
    "brand_analytics": "Brand Analytics",
    "generic": "Универсальный CSV/Excel",
}

STATUS_ICONS = {
    queue.STATUS_PENDING: "🕐",
    queue.STATUS_PROCESSING: "⏳",
    queue.STATUS_DONE: "✅",
    queue.STATUS_ERROR: "⚠️",
    queue.STATUS_SKIPPED: "➖",
}


def _fmt_dt(value: Any) -> str:
    if not value:
        return ""
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return str(value)
    return parsed.tz_convert("Europe/Moscow").strftime("%d.%m.%Y %H:%M")


def _tasks_view(tasks: pd.DataFrame) -> pd.DataFrame:
    if tasks.empty:
        return pd.DataFrame()
    view = pd.DataFrame(
        {
            "Статус": tasks["status"].map(
                lambda s: f"{STATUS_ICONS.get(s, '')} {queue.STATUS_LABELS.get(s, s)}"
            ),
            "Файл": tasks.get("original_filename", ""),
            "Источник": tasks.get("source_key", ""),
            "Период": tasks.get("period_name", ""),
            "Получено": tasks.get("created_at", "").map(_fmt_dt),
            "Обработано": tasks.get("finished_at", "").map(_fmt_dt),
            "Попытки": tasks.get("attempts", 0),
            "Комментарий": tasks.get("error_message", "").astype(str).str.slice(0, 160),
            "task_id": tasks.get("task_id", ""),
        }
    )
    return view


def _process_task_in_ui(task: dict[str, Any], work_dir: str) -> dict[str, Any]:
    project_id, params, source_system = queue.resolve_task_target(task)
    storage_path = str(task.get("storage_path") or "")
    filename = str(task.get("original_filename") or "upload.xlsx")
    file_bytes = store.download_storage_file(storage_path)
    if not file_bytes:
        raise IngestError(f"Файл не найден в Storage: {storage_path}")
    return ingest_file_bytes(
        file_bytes,
        project_id=project_id,
        source_filename=filename,
        period_name=str(task.get("period_name") or ""),
        source_system=source_system,
        date_from=task.get("date_from"),
        date_to=task.get("date_to"),
        params=params,
        work_dir=work_dir,
        save_raw_file=False,
        replace=bool(params.get("replace", True)),
        extra_manifest={
            "storage_path": storage_path,
            "file_sha256": file_sha256(file_bytes),
            "ingest": {
                "mode": "auto_manual_run",
                "task_id": str(task.get("task_id")),
                "source_key": str(task.get("source_key") or ""),
                "context": task.get("context") or {},
                "processed_at": store.now_iso(),
            },
        },
    )


def _run_queue_now(project_id: str, work_dir: str, limit: int = 5) -> None:
    """Обработать задачи очереди прямо из интерфейса."""
    worker_id = f"streamlit:{project_id}"
    processed, failed = 0, 0
    progress = st.progress(0.0, text="Обрабатываю очередь...")
    for index in range(limit):
        task = queue.claim_next_task(worker_id)
        if not task:
            break
        task_id = str(task.get("task_id"))
        label = str(task.get("original_filename") or task_id)
        progress.progress(
            min(1.0, (index + 1) / max(1, limit)), text=f"Обрабатываю: {label}"
        )
        started = time.monotonic()
        try:
            result = _process_task_in_ui(task, work_dir)
        except IngestError as exc:
            queue.mark_error(task_id, str(exc), retry=False)
            st.error(f"{label}: {exc}")
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001
            queue.mark_error(task_id, f"{exc}\n{traceback.format_exc(limit=3)}", retry=True)
            st.error(f"{label}: техническая ошибка — {exc}")
            failed += 1
            continue
        result["elapsed_sec"] = round(time.monotonic() - started, 1)
        queue.mark_done(task_id, period_id=result["period_id"], result=result)
        clear_platform_caches(result["project_id"])
        st.success(
            f"«{result['period_name']}» — сообщений {result['messages']}, "
            f"инфоповодов {result['events']} (за {result['elapsed_sec']} c)"
        )
        processed += 1
    progress.empty()
    if not processed and not failed:
        st.info("Новых задач в очереди нет.")


def render_ingest_queue_block(project_id: str, work_dir: str) -> None:
    st.subheader("Очередь автозагрузки")

    try:
        tasks = queue.list_tasks(project_id=project_id, limit=100)
        # Задачи без project_id резолвятся по источнику — показываем и их.
        pending_by_source = queue.list_tasks(limit=100)
        if not pending_by_source.empty and "project_id" in pending_by_source.columns:
            orphan = pending_by_source[pending_by_source["project_id"].isna()]
            if not orphan.empty:
                tasks = pd.concat([tasks, orphan], ignore_index=True)
    except Exception as exc:  # noqa: BLE001
        st.warning(
            "Не удалось прочитать очередь автозагрузки. Проверьте, что выполнен "
            "скрипт sql/platform_ingest_schema.sql в Supabase."
        )
        st.caption(f"Техническая ошибка: {exc}")
        return

    if tasks.empty:
        st.info(
            "Очередь пуста. Как только n8n положит файл в Supabase Storage и создаст "
            "задачу, она появится здесь."
        )
    else:
        counts = tasks["status"].value_counts().to_dict()
        cols = st.columns(len(queue.STATUS_LABELS))
        for col, (status, label) in zip(cols, queue.STATUS_LABELS.items()):
            col.metric(f"{STATUS_ICONS.get(status, '')} {label}", int(counts.get(status, 0)))

        st.dataframe(
            _tasks_view(tasks).drop(columns=["task_id"]),
            use_container_width=True,
            hide_index=True,
        )

    action_cols = st.columns([1, 1, 2])
    with action_cols[0]:
        if st.button("Обработать очередь сейчас", type="primary"):
            _run_queue_now(project_id, work_dir)
            st.rerun()
    with action_cols[1]:
        if st.button("Обновить"):
            st.rerun()

    if tasks.empty:
        return

    problem = tasks[tasks["status"].isin([queue.STATUS_ERROR, queue.STATUS_SKIPPED])]
    if problem.empty:
        return

    with st.expander(f"Задачи с ошибками ({len(problem)})", expanded=False):
        options = {
            f"{row.get('original_filename')} · {_fmt_dt(row.get('created_at'))}": str(
                row.get("task_id")
            )
            for _, row in problem.iterrows()
        }
        choice = st.selectbox("Задача", list(options.keys()))
        task_id = options[choice]
        selected = problem[problem["task_id"] == task_id]
        if not selected.empty:
            st.text_area(
                "Текст ошибки",
                value=str(selected.iloc[0].get("error_message") or ""),
                height=140,
                disabled=True,
            )
        retry_col, delete_col = st.columns(2)
        with retry_col:
            if st.button("Повторить обработку"):
                queue.retry_task(task_id)
                st.success("Задача возвращена в очередь.")
                st.rerun()
        with delete_col:
            if st.button("Удалить задачу"):
                queue.delete_task(task_id)
                st.success("Задача удалена из очереди.")
                st.rerun()


def render_ingest_sources_block(project_id: str, project_name: str) -> None:
    st.subheader("Источники автозагрузки")
    st.caption(
        "Ключ источника — то, что n8n присылает вместе с файлом: название отчета "
        "Brand Analytics, адрес отправителя или имя папки. По ключу платформа "
        "понимает, в какой проект класть выгрузку."
    )

    try:
        sources = queue.list_sources(project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        st.warning("Таблица источников недоступна. Выполните sql/platform_ingest_schema.sql.")
        st.caption(f"Техническая ошибка: {exc}")
        return

    if not sources.empty:
        view = pd.DataFrame(
            {
                "Ключ": sources["source_key"],
                "Название": sources.get("title", ""),
                "Формат": sources.get("source_system", "auto").map(
                    lambda s: SOURCE_SYSTEM_LABELS.get(s, s)
                ),
                "Активен": sources.get("is_active", True).map(
                    lambda v: "да" if v else "нет"
                ),
            }
        )
        st.dataframe(view, use_container_width=True, hide_index=True)

    with st.form("ingest_source_form"):
        st.markdown("**Добавить или изменить источник**")
        source_key = st.text_input(
            "Ключ источника",
            placeholder="brand-analytics-weekly",
            help="Латиницей, без пробелов. Именно это значение n8n передает в поле source_key.",
        )
        title = st.text_input("Описание", placeholder="Еженедельный отчет Brand Analytics")
        source_system = st.selectbox(
            "Формат выгрузки",
            list(SOURCE_SYSTEM_LABELS.keys()),
            format_func=lambda s: SOURCE_SYSTEM_LABELS.get(s, s),
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            threshold = st.slider("Похожесть", 0.10, 0.60, 0.30, 0.01)
        with c2:
            gap_hours = st.slider("Разрыв между волнами, часов", 1.0, 24.0, 3.0, 1.0)
        with c3:
            window_hours = st.slider("Макс. окно инфоповода, часов", 4.0, 72.0, 16.0, 4.0)
        is_active = st.checkbox("Источник активен", value=True)
        submitted = st.form_submit_button("Сохранить источник", type="primary")

    if submitted:
        if not source_key.strip():
            st.error("Укажите ключ источника.")
        else:
            try:
                queue.upsert_source(
                    source_key=source_key.strip(),
                    project_id=project_id,
                    title=title.strip() or project_name,
                    source_system=source_system,
                    params={
                        "similarity_threshold": float(threshold),
                        "event_gap_hours": float(gap_hours),
                        "event_window_hours": float(window_hours),
                        "replace": True,
                    },
                    is_active=bool(is_active),
                )
                st.success(f"Источник «{source_key.strip()}» сохранен.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Не удалось сохранить источник: {exc}")

    if not sources.empty:
        with st.expander("Удалить источник", expanded=False):
            key_to_delete = st.selectbox(
                "Ключ", sources["source_key"].astype(str).tolist(), key="ingest_source_delete"
            )
            if st.button("Удалить"):
                queue.delete_source(key_to_delete)
                st.success("Источник удален.")
                st.rerun()


def render_n8n_hint_block(project_id: str) -> None:
    with st.expander("Что настроить в n8n", expanded=False):
        bucket = store.storage_bucket_name()
        st.markdown(
            "n8n делает два HTTP-запроса к Supabase: кладет файл в Storage и "
            "создает задачу в очереди. Ниже — значения именно для этого проекта."
        )
        st.code(
            f"""1) Загрузка файла (POST)
{{SUPABASE_URL}}/storage/v1/object/{bucket}/inbox/{project_id}/{{{{ $now.format('yyyy-MM-dd') }}}}_{{{{ $binary.attachment_0.fileName }}}}
Headers: Authorization: Bearer {{SERVICE_ROLE_KEY}}, x-upsert: true

2) Постановка задачи (POST)
{{SUPABASE_URL}}/rest/v1/platform_ingest_queue?on_conflict=project_id,file_sha256
Headers: apikey и Authorization: Bearer {{SERVICE_ROLE_KEY}},
         Prefer: resolution=ignore-duplicates
Body: {{
  "task_id": "ing_{{{{ $execution.id }}}}",
  "project_id": "{project_id}",
  "source_key": "brand-analytics-weekly",
  "storage_path": "inbox/{project_id}/файл.xlsx",
  "original_filename": "файл.xlsx",
  "file_sha256": "...",
  "source_system": "brand_analytics"
}}""",
            language="text",
        )
        st.caption(
            "Готовый workflow лежит в репозитории: n8n/brand-analytics-email-ingest.json — "
            "его можно импортировать в n8n и подставить свои учетные данные."
        )


def render_ingest_admin_page(project_id: str, project_name: str, work_dir: str) -> None:
    st.header("Автозагрузка")
    st.caption(
        "Выгрузки Brand Analytics приходят сюда автоматически: n8n забирает файл "
        "из почты, кладет его в хранилище и ставит задачу, платформа обрабатывает "
        "ее тем же алгоритмом, что и ручную загрузку."
    )
    render_ingest_queue_block(project_id, work_dir)
    st.divider()
    render_ingest_sources_block(project_id, project_name)
    render_n8n_hint_block(project_id)
