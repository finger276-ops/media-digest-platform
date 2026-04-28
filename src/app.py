from __future__ import annotations

import argparse
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from import_adapters import read_source_table, get_excel_sheet_names
from io_utils import read_table
from platform_store import (
    supabase_configured,
    list_projects,
    create_project,
    update_project,
    resolve_project_access,
    list_periods,
    make_period_id,
    save_processed_tables,
    load_generated_tables,
    update_period_metadata,
    delete_period,
    save_uploaded_file_to_storage,
    get_manual,
    save_manual,
    delete_manual,
)
from preprocess import run_preprocess_from_dataframe

APP_TITLE = "Платформа дайджестов"
APP_VERSION = "3.0-alpha: отдельная мультипроектная платформа с изолированными данными"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--work-dir", default=os.getenv("PLATFORM_WORK_DIR", "data/work"))
    args, _ = parser.parse_known_args()
    return args


def get_secret_value(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
    except Exception:
        value = os.getenv(name, default)
    return str(value or "").strip()


def is_platform_admin() -> bool:
    admin_password = get_secret_value("PLATFORM_ADMIN_PASSWORD") or get_secret_value("ADMIN_PASSWORD")
    if "platform_is_admin" not in st.session_state:
        st.session_state["platform_is_admin"] = False
    if not admin_password:
        st.sidebar.warning("PLATFORM_ADMIN_PASSWORD не настроен: режим владельца временно доступен всем.")
        return True
    if st.session_state.get("platform_is_admin"):
        st.sidebar.success("Режим: владелец платформы")
        if st.sidebar.button("Выйти из режима владельца"):
            st.session_state["platform_is_admin"] = False
            st.rerun()
        return True
    with st.sidebar.expander("Вход владельца платформы", expanded=False):
        password = st.text_input("Пароль владельца", type="password", key="platform_admin_password")
        if st.button("Войти", key="platform_admin_login"):
            if password == admin_password:
                st.session_state["platform_is_admin"] = True
                st.rerun()
            else:
                st.error("Неверный пароль.")
    return False


def role_rank(role: str) -> int:
    return {"none": 0, "viewer": 1, "editor": 2, "owner": 3}.get(role, 0)


def fmt_date(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    try:
        ts = pd.to_datetime(value, errors="coerce", dayfirst=True)
        if pd.isna(ts):
            return ""
        return ts.strftime("%d.%m.%Y")
    except Exception:
        return ""


def fmt_period(row: pd.Series) -> str:
    start = fmt_date(row.get("date_from") or row.get("start_date"))
    end = fmt_date(row.get("date_to") or row.get("end_date"))
    if start and end and start != end:
        return f"{start}–{end}"
    return start or end


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def render_project_access(is_admin: bool) -> tuple[str | None, str, pd.DataFrame]:
    projects = list_projects(include_inactive=is_admin)
    if projects.empty:
        if is_admin:
            st.info("Пока нет проектов. Создайте первый проект в блоке управления ниже.")
        else:
            st.warning("Пока нет доступных проектов.")
        return None, "owner" if is_admin else "none", projects

    if is_admin:
        options = projects["project_id"].astype(str).tolist()
        labels = {str(r["project_id"]): str(r.get("project_name") or r["project_id"]) for _, r in projects.iterrows()}
        selected = st.sidebar.selectbox("Проект", options, format_func=lambda x: labels.get(x, x))
        return selected, "owner", projects

    if st.session_state.get("platform_project_id"):
        project_id = st.session_state["platform_project_id"]
        role = st.session_state.get("platform_project_role", "viewer")
        project_row = projects[projects["project_id"].astype(str) == str(project_id)]
        project_name = str(project_row.iloc[0].get("project_name") if not project_row.empty else project_id)
        st.sidebar.success(f"Доступ: {project_name} · {role}")
        if st.sidebar.button("Сменить проект / выйти"):
            st.session_state.pop("platform_project_id", None)
            st.session_state.pop("platform_project_role", None)
            st.rerun()
        return project_id, role, projects

    st.sidebar.info("Введите код доступа к проекту")
    access_code = st.sidebar.text_input("Код проекта", type="password", key="project_access_code")
    if st.sidebar.button("Открыть проект"):
        project_id, role = resolve_project_access(access_code)
        if project_id:
            st.session_state["platform_project_id"] = project_id
            st.session_state["platform_project_role"] = role
            st.rerun()
        else:
            st.sidebar.error("Код проекта не найден.")
    return None, "none", projects


def render_project_manager(projects: pd.DataFrame) -> None:
    st.header("Управление проектами")
    with st.expander("Создать проект", expanded=projects.empty):
        name = st.text_input("Название проекта", key="new_project_name")
        description = st.text_area("Описание проекта", key="new_project_description")
        viewer_code = st.text_input("Код просмотра", type="password", key="new_viewer_code")
        editor_code = st.text_input("Код редактора", type="password", key="new_editor_code")
        if st.button("Создать проект", type="primary"):
            if not name.strip():
                st.error("Укажите название проекта.")
            else:
                project_id = create_project(project_name=name, description=description, viewer_code=viewer_code, editor_code=editor_code)
                st.success(f"Проект создан: {project_id}")
                st.rerun()

    if projects.empty:
        return
    st.subheader("Существующие проекты")
    view = projects.copy()
    for col in ["created_at", "updated_at"]:
        if col in view.columns:
            view[col] = view[col].apply(fmt_date)
    show = view[[c for c in ["project_name", "description", "status", "created_at", "updated_at", "project_id"] if c in view.columns]].rename(columns={
        "project_name": "Проект",
        "description": "Описание",
        "status": "Статус",
        "created_at": "Создан",
        "updated_at": "Обновлен",
        "project_id": "ID",
    })
    event = st.dataframe(show, hide_index=True, use_container_width=True, selection_mode="single-row", on_select="rerun")
    rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
    if rows:
        row = projects.iloc[rows[0]]
        project_id = str(row["project_id"])
        with st.expander(f"Редактировать: {row.get('project_name')}", expanded=True):
            new_name = st.text_input("Название", value=str(row.get("project_name") or ""), key=f"edit_project_name_{project_id}")
            new_description = st.text_area("Описание", value=str(row.get("description") or ""), key=f"edit_project_description_{project_id}")
            new_status = st.selectbox("Статус", ["active", "hidden", "archived"], index=["active", "hidden", "archived"].index(str(row.get("status") or "active")) if str(row.get("status") or "active") in ["active", "hidden", "archived"] else 0, key=f"edit_project_status_{project_id}")
            st.caption("Коды доступа заполняйте только если хотите заменить текущие.")
            new_viewer_code = st.text_input("Новый код просмотра", type="password", key=f"edit_viewer_code_{project_id}")
            new_editor_code = st.text_input("Новый код редактора", type="password", key=f"edit_editor_code_{project_id}")
            if st.button("Сохранить проект", key=f"save_project_{project_id}"):
                update_project(project_id, project_name=new_name, description=new_description, status=new_status, viewer_code=new_viewer_code, editor_code=new_editor_code)
                st.success("Проект обновлен.")
                st.rerun()


def render_period_selector(project_id: str) -> tuple[list[str], pd.DataFrame]:
    periods = list_periods(project_id, include_inactive=False)
    if periods.empty:
        st.sidebar.warning("В проекте пока нет загруженных периодов.")
        return [], periods
    labels = {}
    for _, r in periods.iterrows():
        period_id = str(r["period_id"])
        labels[period_id] = f"{r.get('period_name') or period_id} · {fmt_period(r)}"
    default = periods["period_id"].astype(str).head(3).tolist()
    selected = st.sidebar.multiselect("Периоды", periods["period_id"].astype(str).tolist(), default=default, format_func=lambda x: labels.get(x, x))
    return selected, periods


def read_uploaded_to_canonical(uploaded_file, source_system: str) -> pd.DataFrame:
    suffix = Path(uploaded_file.name).suffix.lower() or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name
    try:
        return read_source_table(tmp_path, source_system=source_system)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def read_generated_tables_from_dir(output_dir: Path) -> dict[str, pd.DataFrame]:
    return {name: read_table(str(output_dir), name) for name in ["events", "discussions", "messages", "discussion_messages", "event_discussions"]}


def render_upload_page(project_id: str, role: str, work_dir: str) -> None:
    st.header("Загрузка файла")
    if role_rank(role) < role_rank("editor"):
        st.info("Для загрузки файлов нужен доступ редактора или владельца.")
        return

    with st.form("upload_form"):
        period_name = st.text_input("Название периода", placeholder="Например: 24.04.2026–30.04.2026")
        date_col1, date_col2 = st.columns(2)
        with date_col1:
            date_from = st.date_input("Дата начала", value=None, format="DD.MM.YYYY")
        with date_col2:
            date_to = st.date_input("Дата окончания", value=None, format="DD.MM.YYYY")
        source_system = st.selectbox("Источник", ["auto", "mediologia", "mediologia_excel", "brand_analytics", "generic"], format_func=lambda x: {
            "auto": "Автоопределение",
            "mediologia": "Медиалогия CSV",
            "mediologia_excel": "Медиалогия Excel",
            "brand_analytics": "Brand Analytics",
            "generic": "Универсальный CSV/Excel",
        }.get(x, x))
        uploaded = st.file_uploader("CSV или Excel", type=["csv", "xlsx", "xls", "xlsm"])
        st.caption("Алгоритм")
        c1, c2, c3 = st.columns(3)
        with c1:
            threshold = st.slider("Похожесть", 0.10, 0.60, 0.30, 0.01)
        with c2:
            event_gap_hours = st.slider("Разрыв между волнами, часов", 1.0, 24.0, 3.0, 1.0)
        with c3:
            event_window_hours = st.slider("Макс. окно инфоповода, часов", 4.0, 72.0, 16.0, 4.0)
        submitted = st.form_submit_button("Обработать и сохранить", type="primary")

    if not submitted:
        return
    if uploaded is None:
        st.error("Загрузите файл.")
        return
    if not period_name.strip():
        st.error("Укажите название периода.")
        return
    if date_from and date_to and date_from > date_to:
        st.error("Дата начала не может быть позже даты окончания.")
        return

    with st.spinner("Читаю файл и привожу к единому формату..."):
        canonical = read_uploaded_to_canonical(uploaded, source_system)
    st.success(f"Файл прочитан: {len(canonical):,} строк".replace(",", " "))
    with st.expander("Предпросмотр распознанных колонок", expanded=False):
        st.dataframe(canonical.head(20), use_container_width=True)

    period_id = make_period_id(project_id, period_name, uploaded.name)
    output_dir = Path(work_dir) / project_id / period_id
    output_dir.mkdir(parents=True, exist_ok=True)
    with st.spinner("Собираю сообщения, обсуждения и инфоповоды..."):
        manifest = run_preprocess_from_dataframe(
            canonical,
            output=output_dir,
            source_file=uploaded.name,
            similarity_threshold=float(threshold),
            event_gap_hours=float(event_gap_hours),
            event_window_hours=float(event_window_hours),
        )
        tables = read_generated_tables_from_dir(output_dir)

    storage_path = ""
    try:
        storage_path = save_uploaded_file_to_storage(project_id, period_id, uploaded.name, uploaded.getvalue())
    except Exception as exc:
        st.warning(f"Обработанные данные сохраню в БД, но сырой файл не удалось сохранить в Storage: {exc}")

    with st.spinner("Сохраняю данные проекта в Supabase..."):
        manifest = dict(manifest or {})
        manifest.update({"storage_path": storage_path, "source_system": source_system})
        save_processed_tables(
            project_id=project_id,
            period_id=period_id,
            period_name=period_name,
            source_filename=uploaded.name,
            tables=tables,
            manifest=manifest,
            date_from=date_from,
            date_to=date_to,
            replace=True,
        )
    st.success("Период сохранен в платформенной базе.")
    st.cache_data.clear()


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
    show = view[[c for c in ["period_name", "Период", "source_filename", "status", "period_id"] if c in view.columns]].rename(columns={
        "period_name": "Название",
        "source_filename": "Файл",
        "status": "Статус",
        "period_id": "ID",
    })
    event = st.dataframe(show, hide_index=True, use_container_width=True, selection_mode="single-row", on_select="rerun")
    rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
    if not rows:
        return
    row = periods.iloc[rows[0]]
    period_id = str(row["period_id"])
    with st.expander("Редактировать период", expanded=True):
        name = st.text_input("Название периода", value=str(row.get("period_name") or ""), key=f"period_name_{period_id}")
        def to_date(v):
            ts = pd.to_datetime(v, errors="coerce")
            return None if pd.isna(ts) else ts.date()
        date_from = st.date_input("Дата начала", value=to_date(row.get("date_from")), format="DD.MM.YYYY", key=f"date_from_{period_id}")
        date_to = st.date_input("Дата окончания", value=to_date(row.get("date_to")), format="DD.MM.YYYY", key=f"date_to_{period_id}")
        filename = st.text_input("Файл", value=str(row.get("source_filename") or ""), key=f"filename_{period_id}")
        status = st.selectbox("Статус", ["active", "hidden", "archived"], index=["active", "hidden", "archived"].index(str(row.get("status") or "active")) if str(row.get("status") or "active") in ["active", "hidden", "archived"] else 0, key=f"status_{period_id}")
        comment = st.text_area("Комментарий", value=str((row.get("manifest") or {}).get("comment", "")) if isinstance(row.get("manifest"), dict) else "", key=f"comment_{period_id}")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Сохранить изменения", key=f"save_period_{period_id}"):
                if date_from and date_to and date_from > date_to:
                    st.error("Дата начала не может быть позже даты окончания.")
                else:
                    update_period_metadata(project_id, period_id, period_name=name, date_from=date_from, date_to=date_to, source_filename=filename, status=status, manifest_updates={"comment": comment})
                    st.success("Период обновлен.")
                    st.rerun()
        with c2:
            if st.button("Скрыть период", key=f"hide_period_{period_id}"):
                delete_period(project_id, period_id, hard=False)
                st.success("Период скрыт.")
                st.rerun()


def enrich_messages(messages: pd.DataFrame, event_discussions: pd.DataFrame, discussion_messages: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    if messages.empty:
        return messages
    out = messages.copy()
    if not event_discussions.empty and not discussion_messages.empty:
        link = discussion_messages.merge(event_discussions, on="discussion_id", how="left")
        if "message_id" in link.columns and "event_id" in link.columns:
            msg_event = link[["message_id", "event_id"]].drop_duplicates("message_id")
            out = out.merge(msg_event, on="message_id", how="left")
    if not events.empty and "event_id" in events.columns:
        titles = events[["event_id", "event_title"]].drop_duplicates("event_id")
        out = out.merge(titles, on="event_id", how="left")
    return out


def aggregate_events(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    df = events.copy()
    df["title"] = df.get("event_title", "").fillna("Без названия").astype(str).replace("", "Без названия")
    df["group_key"] = df["title"].str.lower().str.strip()
    rows = []
    for key, group in df.groupby("group_key", dropna=False):
        row = {
            "group_key": key,
            "title": group["title"].iloc[0],
            "description": build_event_description(group),
            "tags": " | ".join(sorted(set("|".join(group.get("main_tags", pd.Series(dtype=str)).fillna("").astype(str)).split("|")) - {""})),
            "start_date": pd.to_datetime(group.get("start_date"), errors="coerce").min(),
            "end_date": pd.to_datetime(group.get("end_date"), errors="coerce").max(),
            "message_count": int(pd.to_numeric(group.get("message_count", 0), errors="coerce").fillna(0).sum()),
            "chat_count": int(pd.to_numeric(group.get("chat_count", 0), errors="coerce").fillna(0).sum()),
            "negative_count": int(pd.to_numeric(group.get("negative_count", 0), errors="coerce").fillna(0).sum()),
            "importance_score": float(pd.to_numeric(group.get("importance_score", 0), errors="coerce").fillna(0).max()),
            "event_ids": list(group["event_id"].astype(str)) if "event_id" in group.columns else [],
        }
        row["negative_share"] = row["negative_count"] / row["message_count"] if row["message_count"] else 0
        rows.append(row)
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["importance_score", "message_count"], ascending=False)
    return out


def build_event_description(group: pd.DataFrame) -> str:
    text = " ".join(group.get("event_summary", pd.Series(dtype=str)).fillna("").astype(str).tolist())
    signals = []
    patterns = [
        ("законопроекты и регулирование", ["закон", "регулир", "штраф", "налог", "патент"]),
        ("коэффициенты, тарифы и приоритет", ["коэфф", "тариф", "приоритет", "цена"]),
        ("сбои приложения и обновления", ["сбой", "ошиб", "прилож", "обнов", "загруз"]),
        ("выплаты и оплата", ["выплат", "оплат", "деньг"]),
        ("блокировки и доступ к аккаунту", ["блок", "доступ", "аккаунт"]),
        ("забастовки и бойкоты", ["забаст", "бойкот"]),
        ("карты, адреса и навигация", ["карта", "адрес", "навиг", "гео"]),
    ]
    low = text.lower()
    for label, keys in patterns:
        if any(k in low for k in keys):
            signals.append(label)
    if not signals:
        tags = " | ".join(sorted(set("|".join(group.get("main_tags", pd.Series(dtype=str)).fillna("").astype(str)).split("|")) - {""}))
        return f"В теме обсуждались: {tags}." if tags else "В теме обсуждались связанные сообщения выбранного периода."
    return "В теме обсуждались: " + "; ".join(signals[:5]) + "."


def build_auto_summary(messages: pd.DataFrame, events_agg: pd.DataFrame, periods: pd.DataFrame, selected_period_ids: list[str]) -> str:
    total = len(messages)
    chats = messages["chat_title"].nunique() if "chat_title" in messages.columns else 0
    authors = messages["author"].nunique() if "author" in messages.columns else 0
    neg = 0
    if "sentiment" in messages.columns:
        neg = int(messages["sentiment"].fillna("").astype(str).str.lower().str.contains("нег").sum())
    neg_share = (neg / total * 100) if total else 0
    period_names = []
    if not periods.empty:
        subset = periods[periods["period_id"].astype(str).isin([str(x) for x in selected_period_ids])]
        period_names = [str(x) for x in subset.get("period_name", pd.Series(dtype=str)).tolist()]
    top_events = events_agg.head(5)["title"].tolist() if not events_agg.empty else []
    top_chats = []
    if "chat_title" in messages.columns:
        top_chats = messages["chat_title"].fillna("").astype(str).replace("", pd.NA).dropna().value_counts().head(5).index.tolist()

    lines = []
    lines.append(f"За выбранный период обработано {total:,} сообщений из {chats:,} чатов; уникальных авторов — {authors:,}.".replace(",", " "))
    lines.append(f"Негативных сообщений: {neg:,} ({neg_share:.1f}%).".replace(",", " "))
    if period_names:
        lines.append("Периоды: " + "; ".join(period_names[:6]) + ("…" if len(period_names) > 6 else ""))
    if top_events:
        lines.append("Основные инфоповоды: " + "; ".join(top_events) + ".")
    if top_chats:
        lines.append("Наиболее активные чаты: " + "; ".join(top_chats) + ".")
    return "\n\n".join(lines)


def render_summary(project_id: str, period_ids: list[str], messages: pd.DataFrame, events_agg: pd.DataFrame, periods: pd.DataFrame, role: str) -> None:
    st.subheader("Саммари")
    key = "summary::" + "__".join(sorted(period_ids))
    manual = get_manual(project_id, key)
    auto_summary = build_auto_summary(messages, events_agg, periods, period_ids)
    summary_text = (manual or {}).get("summary") or auto_summary
    st.markdown(summary_text.replace("\n", "  \n"))
    if role_rank(role) >= role_rank("editor"):
        with st.expander("Редактировать саммари"):
            edited = st.text_area("Текст саммари", value=summary_text, height=220, key=f"summary_{key}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Сохранить саммари", key=f"save_{key}"):
                    save_manual(project_id, "summaries", key, {"summary": edited})
                    st.success("Саммари сохранено.")
                    st.rerun()
            with c2:
                if st.button("Вернуть автоматическое", key=f"auto_{key}"):
                    delete_manual(project_id, key)
                    st.success("Вернули автоматическое саммари.")
                    st.rerun()


def render_period_dynamics(messages: pd.DataFrame, periods: pd.DataFrame, period_ids: list[str]) -> None:
    if len(period_ids) < 2 or messages.empty or "period_id" not in messages.columns:
        return
    rows = []
    for period_id, group in messages.groupby("period_id"):
        meta = periods[periods["period_id"].astype(str) == str(period_id)]
        name = str(meta.iloc[0].get("period_name") if not meta.empty else period_id)
        sort_date = pd.to_datetime(meta.iloc[0].get("date_from") if not meta.empty else None, errors="coerce")
        neg = int(group.get("sentiment", pd.Series(dtype=str)).fillna("").astype(str).str.lower().str.contains("нег").sum()) if "sentiment" in group.columns else 0
        total = len(group)
        rows.append({"period_name": name, "sort_date": sort_date, "Сообщения": total, "Негатив": neg, "Доля негатива, %": round(neg / total * 100, 1) if total else 0})
    summary = pd.DataFrame(rows)
    if summary.empty:
        return
    summary = summary.sort_values(["sort_date", "period_name"], na_position="last")
    st.subheader("Динамика по периодам")
    c1, c2 = st.columns(2)
    chart_df = summary.set_index("period_name")
    with c1:
        st.line_chart(chart_df[["Сообщения"]])
    with c2:
        st.line_chart(chart_df[["Доля негатива, %"]])
    st.dataframe(summary.drop(columns=["sort_date"]), hide_index=True, use_container_width=True)


def render_events(project_id: str, events_agg: pd.DataFrame, messages: pd.DataFrame, event_discussions: pd.DataFrame, discussion_messages: pd.DataFrame) -> None:
    st.subheader("Инфоповоды")
    if events_agg.empty:
        st.info("Инфоповоды не найдены.")
        return
    word = st.text_input("Фильтр по слову в сообщениях", placeholder="Например: коэффициент")
    filtered_events = events_agg.copy()
    filtered_messages = messages.copy()
    if word.strip() and "text" in filtered_messages.columns:
        mask = filtered_messages["text"].fillna("").astype(str).str.contains(word.strip(), case=False, regex=False)
        filtered_messages = filtered_messages[mask]
        if "event_id" in filtered_messages.columns:
            allowed = set(filtered_messages["event_id"].dropna().astype(str))
            filtered_events = filtered_events[filtered_events["event_ids"].apply(lambda ids: bool(set(map(str, ids)) & allowed))]
        st.caption(f"Найдено сообщений: {len(filtered_messages):,}".replace(",", " "))
        msg_view = filtered_messages.copy()
        if not msg_view.empty:
            msg_view["Дата"] = msg_view.get("datetime", "").apply(fmt_date)
            columns = [c for c in ["Дата", "chat_title", "author", "event_title", "text", "url"] if c in msg_view.columns]
            st.dataframe(msg_view[columns].rename(columns={"chat_title": "Чат", "author": "Автор", "event_title": "Инфоповод", "text": "Текст", "url": "Ссылка"}).head(300), hide_index=True, use_container_width=True)

    table = filtered_events.copy()
    table["Период"] = table.apply(lambda r: f"{fmt_date(r.get('start_date'))}–{fmt_date(r.get('end_date'))}" if fmt_date(r.get('start_date')) != fmt_date(r.get('end_date')) else fmt_date(r.get('start_date')), axis=1)
    table["Негатив"] = (table["negative_share"] * 100).round(1).astype(str) + "%"
    show = table[["title", "description", "tags", "Период", "message_count", "chat_count", "Негатив", "importance_score"]].rename(columns={
        "title": "Название",
        "description": "Описание",
        "tags": "Теги",
        "message_count": "Сообщений",
        "chat_count": "Чатов",
        "importance_score": "Важность",
    })
    event = st.dataframe(show, hide_index=True, use_container_width=True, selection_mode="single-row", on_select="rerun")
    rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
    if not rows:
        return
    selected = filtered_events.iloc[rows[0]]
    selected_ids = set(map(str, selected.get("event_ids", [])))
    st.markdown(f"### {selected['title']}")
    st.write(selected.get("description", ""))
    if not event_discussions.empty and not discussion_messages.empty:
        disc_ids = event_discussions[event_discussions["event_id"].astype(str).isin(selected_ids)]["discussion_id"].astype(str).unique().tolist()
        msg_ids = discussion_messages[discussion_messages["discussion_id"].astype(str).isin(disc_ids)]["message_id"].astype(str).unique().tolist()
        topic_messages = messages[messages["message_id"].astype(str).isin(msg_ids)].copy()
    else:
        topic_messages = messages[messages.get("event_id", pd.Series(dtype=str)).astype(str).isin(selected_ids)].copy()
    if topic_messages.empty:
        st.info("Сообщения по выбранному инфоповоду не найдены.")
        return
    topic_messages = topic_messages.sort_values("datetime") if "datetime" in topic_messages.columns else topic_messages
    topic_messages["Дата"] = topic_messages.get("datetime", "").apply(fmt_date)
    cols = [c for c in ["Дата", "chat_title", "author", "text", "url"] if c in topic_messages.columns]
    st.dataframe(topic_messages[cols].rename(columns={"chat_title": "Чат", "author": "Автор", "text": "Текст", "url": "Ссылка"}), hide_index=True, use_container_width=True)


def main() -> None:
    args = parse_args()
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(APP_VERSION)

    if not supabase_configured():
        st.error("Supabase не настроен. Добавьте SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY в Secrets.")
        st.stop()

    is_admin = is_platform_admin()
    project_id, role, projects = render_project_access(is_admin)

    if is_admin:
        with st.sidebar.expander("Управление платформой", expanded=False):
            if st.button("Открыть управление проектами"):
                st.session_state["platform_page"] = "projects"
    if project_id:
        page_options = ["Дашборд", "Загрузка файла", "История периодов"]
    else:
        page_options = []
    if is_admin:
        page_options.append("Проекты")
    if not page_options:
        st.info("Выберите проект или войдите как владелец платформы.")
        if is_admin:
            render_project_manager(projects)
        return
    page = st.sidebar.radio("Раздел", page_options, index=page_options.index("Проекты") if st.session_state.get("platform_page") == "projects" and "Проекты" in page_options else 0)

    if page == "Проекты":
        render_project_manager(projects)
        return

    if not project_id:
        st.info("Введите код доступа к проекту или войдите как владелец платформы.")
        return

    project_row = projects[projects["project_id"].astype(str) == str(project_id)]
    project_name = str(project_row.iloc[0].get("project_name") if not project_row.empty else project_id)
    st.sidebar.markdown(f"**Текущий проект:**  \n{project_name}")

    if page == "Загрузка файла":
        render_upload_page(project_id, role, args.work_dir)
        return
    if page == "История периодов":
        render_period_history(project_id, role)
        return

    selected_period_ids, periods = render_period_selector(project_id)
    if not selected_period_ids:
        st.info("Выберите период или загрузите первый файл.")
        return

    with st.spinner("Загружаю данные проекта..."):
        events, discussions, messages, discussion_messages, event_discussions = load_generated_tables(project_id, selected_period_ids)
    enriched_messages = enrich_messages(messages, event_discussions, discussion_messages, events)
    events_agg = aggregate_events(events)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Сообщений", f"{len(messages):,}".replace(",", " "))
    col2.metric("Инфоповодов", f"{len(events_agg):,}".replace(",", " "))
    col3.metric("Чатов", f"{messages['chat_title'].nunique() if 'chat_title' in messages.columns else 0:,}".replace(",", " "))
    neg = int(messages.get("sentiment", pd.Series(dtype=str)).fillna("").astype(str).str.lower().str.contains("нег").sum()) if not messages.empty and "sentiment" in messages.columns else 0
    col4.metric("Негатив", f"{neg:,}".replace(",", " "))

    render_summary(project_id, selected_period_ids, messages, events_agg, periods, role)
    render_period_dynamics(messages, periods, selected_period_ids)
    render_events(project_id, events_agg, enriched_messages, event_discussions, discussion_messages)


if __name__ == "__main__":
    main()
