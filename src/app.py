from __future__ import annotations

import argparse
import os
import tempfile
import uuid
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
    list_manual,
    save_manual,
    delete_manual,
)
from preprocess import run_preprocess_from_dataframe

APP_TITLE = "Платформа дайджестов"
APP_VERSION = "3.1-alpha: мультипроектная платформа с ручной модерацией инфоповодов"


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
            "description": pick_event_description(group),
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



def pick_event_description(group: pd.DataFrame) -> str:
    """Return manual description if present; otherwise build an automatic one."""
    for col in ["display_description", "manual_description", "event_description"]:
        if col in group.columns:
            vals = [str(x).strip() for x in group[col].fillna("").tolist() if str(x).strip()]
            if vals:
                return vals[0]
    return build_event_description(group)


def manual_payloads(manual_df: pd.DataFrame, table_name: str) -> list[dict[str, Any]]:
    if manual_df is None or manual_df.empty or "table_name" not in manual_df.columns:
        return []
    rows = manual_df[manual_df["table_name"].astype(str) == table_name]
    payloads: list[dict[str, Any]] = []
    for _, row in rows.iterrows():
        payload = row.get("payload") or {}
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("_row_key", str(row.get("row_key") or ""))
            payloads.append(payload)
    return payloads


def get_manual_state(project_id: str) -> dict[str, Any]:
    try:
        manual_df = list_manual(project_id)
    except Exception:
        manual_df = pd.DataFrame()

    hidden_messages: set[str] = set()
    hidden_message_keys: dict[str, str] = {}
    for payload in manual_payloads(manual_df, "message_hidden"):
        message_id = str(payload.get("message_id") or payload.get("_row_key", "").replace("message_hidden::", ""))
        if message_id:
            hidden_messages.add(message_id)
            hidden_message_keys[message_id] = str(payload.get("_row_key") or f"message_hidden::{message_id}")

    irrelevant_pairs: set[tuple[str, str]] = set()
    irrelevant_keys: dict[tuple[str, str], str] = {}
    for payload in manual_payloads(manual_df, "message_irrelevant"):
        event_id = str(payload.get("event_id") or "")
        message_id = str(payload.get("message_id") or "")
        if event_id and message_id:
            pair = (event_id, message_id)
            irrelevant_pairs.add(pair)
            irrelevant_keys[pair] = str(payload.get("_row_key") or f"message_irrelevant::{event_id}::{message_id}")

    move_map: dict[str, str] = {}
    for payload in manual_payloads(manual_df, "message_moves"):
        message_id = str(payload.get("message_id") or payload.get("_row_key", "").replace("message_move::", ""))
        target_event_id = str(payload.get("target_event_id") or "")
        if message_id and target_event_id:
            move_map[message_id] = target_event_id

    event_edits: dict[str, dict[str, Any]] = {}
    for payload in manual_payloads(manual_df, "event_edits"):
        event_id = str(payload.get("event_id") or payload.get("_row_key", "").replace("event_edit::", ""))
        if event_id:
            event_edits[event_id] = payload

    event_merges: dict[str, str] = {}
    for payload in manual_payloads(manual_df, "event_merges"):
        source_event_id = str(payload.get("source_event_id") or payload.get("_row_key", "").replace("event_merge::", ""))
        target_event_id = str(payload.get("target_event_id") or "")
        if source_event_id and target_event_id:
            event_merges[source_event_id] = target_event_id

    manual_events = manual_payloads(manual_df, "manual_events")

    return {
        "manual_df": manual_df,
        "hidden_messages": hidden_messages,
        "hidden_message_keys": hidden_message_keys,
        "irrelevant_pairs": irrelevant_pairs,
        "irrelevant_keys": irrelevant_keys,
        "move_map": move_map,
        "event_edits": event_edits,
        "event_merges": event_merges,
        "manual_events": manual_events,
    }


def append_manual_events(events: pd.DataFrame, manual_events: list[dict[str, Any]]) -> pd.DataFrame:
    if not manual_events:
        return events
    rows = []
    for payload in manual_events:
        event_id = str(payload.get("event_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not event_id or not title:
            continue
        rows.append({
            "event_id": event_id,
            "event_title": title,
            "event_summary": str(payload.get("description") or ""),
            "display_description": str(payload.get("description") or ""),
            "main_tags": str(payload.get("tags") or "Ручной инфоповод"),
            "start_date": pd.NaT,
            "end_date": pd.NaT,
            "message_count": 0,
            "chat_count": 0,
            "negative_count": 0,
            "importance_score": 0,
            "status": str(payload.get("status") or "active"),
            "is_manual_event": True,
        })
    if not rows:
        return events
    extra = pd.DataFrame(rows)
    if events is None or events.empty:
        return extra
    return pd.concat([events, extra], ignore_index=True, sort=False)


def recompute_event_counts(events: pd.DataFrame, messages: pd.DataFrame) -> pd.DataFrame:
    if events is None or events.empty or messages is None or messages.empty or "event_id" not in messages.columns:
        return events
    out = events.copy()
    msg = messages.copy()
    msg["event_id"] = msg["event_id"].fillna("").astype(str)
    msg = msg[msg["event_id"].str.strip() != ""]
    if msg.empty:
        return out
    grouped = msg.groupby("event_id", dropna=False)
    for event_id, group in grouped:
        mask = out["event_id"].astype(str) == str(event_id)
        if not mask.any():
            continue
        out.loc[mask, "message_count"] = int(len(group))
        if "chat_title" in group.columns:
            out.loc[mask, "chat_count"] = int(group["chat_title"].fillna("").astype(str).replace("", pd.NA).dropna().nunique())
        elif "chat_id" in group.columns:
            out.loc[mask, "chat_count"] = int(group["chat_id"].fillna("").astype(str).replace("", pd.NA).dropna().nunique())
        if "sentiment" in group.columns:
            out.loc[mask, "negative_count"] = int(group["sentiment"].fillna("").astype(str).str.lower().str.contains("нег").sum())
        if "datetime" in group.columns:
            dt = pd.to_datetime(group["datetime"], errors="coerce").dropna()
            if not dt.empty:
                out.loc[mask, "start_date"] = dt.min()
                out.loc[mask, "end_date"] = dt.max()
    return out


def apply_manual_overrides(project_id: str, events: pd.DataFrame, messages: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    state = get_manual_state(project_id)
    events_out = events.copy() if events is not None else pd.DataFrame()
    messages_out = messages.copy() if messages is not None else pd.DataFrame()

    events_out = append_manual_events(events_out, state["manual_events"])
    if not events_out.empty:
        for col in ["event_id", "event_title", "event_summary", "main_tags", "status", "display_description"]:
            if col not in events_out.columns:
                events_out[col] = ""

    # Apply event-level manual edits.
    if not events_out.empty:
        for event_id, payload in state["event_edits"].items():
            mask = events_out["event_id"].astype(str) == str(event_id)
            if not mask.any():
                continue
            if str(payload.get("title") or "").strip():
                events_out.loc[mask, "event_title"] = str(payload.get("title")).strip()
            if "description" in payload:
                events_out.loc[mask, "display_description"] = str(payload.get("description") or "").strip()
                events_out.loc[mask, "event_summary"] = str(payload.get("description") or "").strip()
            if str(payload.get("tags") or "").strip():
                events_out.loc[mask, "main_tags"] = str(payload.get("tags")).strip()
            if str(payload.get("status") or "").strip():
                events_out.loc[mask, "status"] = str(payload.get("status")).strip()

    # Merge events by redirecting source title to target title.
    if not events_out.empty and state["event_merges"]:
        title_map = {
            str(row.get("event_id")): str(row.get("event_title") or "Без названия")
            for _, row in events_out.iterrows()
        }
        summary_map = {
            str(row.get("event_id")): str(row.get("display_description") or row.get("event_summary") or "")
            for _, row in events_out.iterrows()
        }
        for source_event_id, target_event_id in state["event_merges"].items():
            mask = events_out["event_id"].astype(str) == str(source_event_id)
            if mask.any():
                events_out.loc[mask, "event_title"] = title_map.get(str(target_event_id), str(target_event_id))
                events_out.loc[mask, "display_description"] = summary_map.get(str(target_event_id), "")
                events_out.loc[mask, "merged_into"] = str(target_event_id)

    if not messages_out.empty:
        if "message_id" not in messages_out.columns:
            messages_out["message_id"] = messages_out.index.astype(str)
        messages_out["message_id"] = messages_out["message_id"].fillna("").astype(str)

        if state["hidden_messages"]:
            messages_out = messages_out[~messages_out["message_id"].isin(state["hidden_messages"])].copy()

        if "event_id" in messages_out.columns:
            messages_out["event_id"] = messages_out["event_id"].fillna("").astype(str)
            if state["event_merges"]:
                messages_out["event_id"] = messages_out["event_id"].map(lambda x: state["event_merges"].get(str(x), str(x)))
            if state["move_map"]:
                messages_out["event_id"] = messages_out.apply(lambda r: state["move_map"].get(str(r.get("message_id")), str(r.get("event_id") or "")), axis=1)

    # Refresh titles in messages after moves/merges.
    if not events_out.empty and not messages_out.empty and "event_id" in messages_out.columns:
        title_map = {
            str(row.get("event_id")): str(row.get("event_title") or "Без названия")
            for _, row in events_out.iterrows()
        }
        messages_out["event_title"] = messages_out["event_id"].fillna("").astype(str).map(title_map).fillna(messages_out.get("event_title", ""))

    events_out = recompute_event_counts(events_out, messages_out)

    # Hide events after counts are recomputed.
    if not events_out.empty and "status" in events_out.columns:
        events_out = events_out[~events_out["status"].fillna("").astype(str).str.lower().isin(["hidden", "deleted", "archived"])].copy()

    return events_out, messages_out, state


def create_manual_event(project_id: str, title: str, description: str = "", tags: str = "", status: str = "active") -> str:
    event_id = "manual_" + uuid.uuid4().hex[:12]
    save_manual(project_id, "manual_events", f"manual_event::{event_id}", {
        "event_id": event_id,
        "title": title.strip(),
        "description": description.strip(),
        "tags": tags.strip() or "Ручной инфоповод",
        "status": status,
    })
    return event_id


def event_select_options(events_agg: pd.DataFrame, exclude_event_ids: set[str] | None = None) -> list[tuple[str, str]]:
    exclude_event_ids = exclude_event_ids or set()
    options: list[tuple[str, str]] = []
    if events_agg is None or events_agg.empty:
        return options
    for _, row in events_agg.iterrows():
        ids = [str(x) for x in row.get("event_ids", [])]
        if not ids:
            continue
        if set(ids) & exclude_event_ids:
            continue
        label = f"{row.get('title') or ids[0]} · {int(row.get('message_count') or 0)} сообщ."
        options.append((ids[0], label))
    return options


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



def render_events(
    project_id: str,
    role: str,
    events_agg: pd.DataFrame,
    messages: pd.DataFrame,
    manual_state: dict[str, Any],
) -> None:
    st.subheader("Инфоповоды")
    can_edit = role_rank(role) >= role_rank("editor")

    if can_edit:
        with st.expander("Создать инфоповод вручную", expanded=False):
            title = st.text_input("Название нового инфоповода", key="new_manual_event_title")
            description = st.text_area("Описание", key="new_manual_event_description")
            tags = st.text_input("Теги", key="new_manual_event_tags")
            if st.button("Создать инфоповод", type="primary", key="create_manual_event"):
                if not title.strip():
                    st.error("Укажите название инфоповода.")
                else:
                    create_manual_event(project_id, title, description, tags)
                    st.success("Инфоповод создан.")
                    st.rerun()

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
            st.dataframe(
                msg_view[columns].rename(columns={
                    "chat_title": "Чат",
                    "author": "Автор",
                    "event_title": "Инфоповод",
                    "text": "Текст",
                    "url": "Ссылка",
                }).head(500),
                hide_index=True,
                use_container_width=True,
            )

    table = filtered_events.copy()
    table["Период"] = table.apply(
        lambda r: f"{fmt_date(r.get('start_date'))}–{fmt_date(r.get('end_date'))}"
        if fmt_date(r.get("start_date")) != fmt_date(r.get("end_date"))
        else fmt_date(r.get("start_date")),
        axis=1,
    )
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

    if can_edit:
        with st.expander("Правка выбранного инфоповода", expanded=False):
            new_title = st.text_input("Название", value=str(selected.get("title") or ""), key=f"edit_title_{selected.get('group_key')}")
            new_desc = st.text_area("Описание", value=str(selected.get("description") or ""), height=160, key=f"edit_desc_{selected.get('group_key')}")
            new_tags = st.text_input("Теги", value=str(selected.get("tags") or ""), key=f"edit_tags_{selected.get('group_key')}")
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("Сохранить правки", key=f"save_event_edit_{selected.get('group_key')}"):
                    for event_id in selected_ids:
                        save_manual(project_id, "event_edits", f"event_edit::{event_id}", {
                            "event_id": event_id,
                            "title": new_title,
                            "description": new_desc,
                            "tags": new_tags,
                            "status": "active",
                        })
                    st.success("Правки сохранены.")
                    st.rerun()
            with c2:
                if st.button("Скрыть инфоповод", key=f"hide_event_{selected.get('group_key')}"):
                    for event_id in selected_ids:
                        save_manual(project_id, "event_edits", f"event_edit::{event_id}", {
                            "event_id": event_id,
                            "title": new_title,
                            "description": new_desc,
                            "tags": new_tags,
                            "status": "hidden",
                        })
                    st.success("Инфоповод скрыт.")
                    st.rerun()
            with c3:
                options = event_select_options(events_agg, exclude_event_ids=selected_ids)
                if options:
                    target = st.selectbox("Объединить с темой", options, format_func=lambda x: x[1], key=f"merge_target_{selected.get('group_key')}")
                    if st.button("Объединить", key=f"merge_event_{selected.get('group_key')}"):
                        target_event_id = target[0]
                        for source_event_id in selected_ids:
                            if source_event_id != target_event_id:
                                save_manual(project_id, "event_merges", f"event_merge::{source_event_id}", {
                                    "source_event_id": source_event_id,
                                    "target_event_id": target_event_id,
                                })
                        st.success("Инфоповоды объединены.")
                        st.rerun()

    if messages.empty or "event_id" not in messages.columns:
        st.info("Сообщения по выбранному инфоповоду не найдены.")
        return

    topic_messages = messages[messages["event_id"].fillna("").astype(str).isin(selected_ids)].copy()
    if topic_messages.empty:
        st.info("Сообщения по выбранному инфоповоду не найдены.")
        return

    # Exclude messages marked as irrelevant for this event/group.
    irrelevant_pairs: set[tuple[str, str]] = manual_state.get("irrelevant_pairs", set())
    if irrelevant_pairs and "message_id" in topic_messages.columns:
        topic_messages["__pair"] = topic_messages.apply(lambda r: (str(r.get("event_id") or ""), str(r.get("message_id") or "")), axis=1)
        topic_messages = topic_messages[~topic_messages["__pair"].isin(irrelevant_pairs)].drop(columns=["__pair"], errors="ignore")

    topic_messages = topic_messages.sort_values("datetime") if "datetime" in topic_messages.columns else topic_messages
    topic_messages["Дата"] = topic_messages.get("datetime", "").apply(fmt_date)
    cols = [c for c in ["Дата", "chat_title", "author", "text", "url"] if c in topic_messages.columns]
    msg_show = topic_messages[cols].rename(columns={
        "chat_title": "Чат",
        "author": "Автор",
        "text": "Текст",
        "url": "Ссылка",
    })
    st.markdown("#### Сообщения инфоповода")
    msg_event = st.dataframe(msg_show, hide_index=True, use_container_width=True, selection_mode="single-row", on_select="rerun")
    msg_rows = getattr(msg_event, "selection", {}).get("rows", []) if msg_event is not None else []

    if can_edit and msg_rows:
        selected_msg = topic_messages.iloc[msg_rows[0]]
        message_id = str(selected_msg.get("message_id") or "")
        current_event_id = str(selected_msg.get("event_id") or "")
        with st.expander("Действия с выбранным сообщением", expanded=True):
            st.caption(str(selected_msg.get("text") or "")[:1000])
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("Нерелевант к теме", key=f"irrelevant_{current_event_id}_{message_id}"):
                    save_manual(project_id, "message_irrelevant", f"message_irrelevant::{current_event_id}::{message_id}", {
                        "event_id": current_event_id,
                        "message_id": message_id,
                    })
                    st.success("Сообщение исключено из этой темы.")
                    st.rerun()
            with c2:
                if st.button("Скрыть сообщение", key=f"hide_msg_{message_id}"):
                    save_manual(project_id, "message_hidden", f"message_hidden::{message_id}", {
                        "message_id": message_id,
                    })
                    st.success("Сообщение скрыто.")
                    st.rerun()
            with c3:
                options = event_select_options(events_agg, exclude_event_ids={current_event_id})
                if options:
                    target = st.selectbox("Перенести в тему", options, format_func=lambda x: x[1], key=f"move_target_{message_id}")
                    if st.button("Перенести", key=f"move_msg_{message_id}"):
                        save_manual(project_id, "message_moves", f"message_move::{message_id}", {
                            "message_id": message_id,
                            "source_event_id": current_event_id,
                            "target_event_id": target[0],
                        })
                        st.success("Сообщение перенесено.")
                        st.rerun()

            st.markdown("**Создать новую тему из выбранного сообщения**")
            new_topic_title = st.text_input("Название новой темы", key=f"new_topic_title_{message_id}")
            new_topic_desc = st.text_area("Описание новой темы", key=f"new_topic_desc_{message_id}", height=120)
            new_topic_tags = st.text_input("Теги новой темы", key=f"new_topic_tags_{message_id}")
            if st.button("Создать и перенести сообщение", key=f"create_topic_from_msg_{message_id}"):
                if not new_topic_title.strip():
                    st.error("Укажите название новой темы.")
                else:
                    new_event_id = create_manual_event(project_id, new_topic_title, new_topic_desc, new_topic_tags)
                    save_manual(project_id, "message_moves", f"message_move::{message_id}", {
                        "message_id": message_id,
                        "source_event_id": current_event_id,
                        "target_event_id": new_event_id,
                    })
                    st.success("Новая тема создана, сообщение перенесено.")
                    st.rerun()

    if can_edit:
        with st.expander("Нерелевантные сообщения по выбранной теме", expanded=False):
            keys = manual_state.get("irrelevant_keys", {})
            pairs_for_topic = [(event_id, message_id) for (event_id, message_id) in keys if event_id in selected_ids]
            if not pairs_for_topic:
                st.caption("Нет сообщений, исключенных из этой темы.")
            else:
                excluded_ids = [message_id for _, message_id in pairs_for_topic]
                excluded = messages[messages["message_id"].astype(str).isin(excluded_ids)].copy() if "message_id" in messages.columns else pd.DataFrame()
                if not excluded.empty:
                    excluded["Дата"] = excluded.get("datetime", "").apply(fmt_date)
                    view_cols = [c for c in ["Дата", "chat_title", "author", "text"] if c in excluded.columns]
                    st.dataframe(excluded[view_cols].rename(columns={"chat_title": "Чат", "author": "Автор", "text": "Текст"}), hide_index=True, use_container_width=True)
                for pair in pairs_for_topic[:20]:
                    if st.button(f"Вернуть сообщение {pair[1]}", key=f"restore_irrel_{pair[0]}_{pair[1]}"):
                        delete_manual(project_id, keys[pair])
                        st.success("Сообщение возвращено в тему.")
                        st.rerun()


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
    events, enriched_messages, manual_state = apply_manual_overrides(project_id, events, enriched_messages)
    events_agg = aggregate_events(events)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Сообщений", f"{len(enriched_messages):,}".replace(",", " "))
    col2.metric("Инфоповодов", f"{len(events_agg):,}".replace(",", " "))
    col3.metric("Чатов", f"{enriched_messages['chat_title'].nunique() if 'chat_title' in enriched_messages.columns else 0:,}".replace(",", " "))
    neg = int(enriched_messages.get("sentiment", pd.Series(dtype=str)).fillna("").astype(str).str.lower().str.contains("нег").sum()) if not enriched_messages.empty and "sentiment" in enriched_messages.columns else 0
    col4.metric("Негатив", f"{neg:,}".replace(",", " "))

    render_summary(project_id, selected_period_ids, enriched_messages, events_agg, periods, role)
    render_period_dynamics(enriched_messages, periods, selected_period_ids)
    render_events(project_id, role, events_agg, enriched_messages, manual_state)


if __name__ == "__main__":
    main()
