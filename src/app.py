from __future__ import annotations

import argparse
import os
import tempfile
import uuid
import re
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
APP_VERSION = "4.0: мультиплатформа + дашборд водительских чатов"

ALGORITHM_PROFILE_OPTIONS = {
    "universal": "Универсальный",
    "brand_monitoring": "Бренд-мониторинг",
    "construction_materials": "Строительство / материалы",
    "driver_chats": "Дайджест водительских чатов",
    "taxi_legacy": "Такси / водительские чаты (legacy)",
}

TAXI_PROJECT_PROFILES = {"driver_chats", "taxi_legacy"}


def project_topic_profile(project_row: pd.Series | None) -> str:
    settings = project_settings_from_row(project_row) if project_row is not None else {}
    profile = str(settings.get("topic_profile") or "universal")
    return profile if profile in ALGORITHM_PROFILE_OPTIONS else "universal"


def is_taxi_project_profile(profile: str) -> bool:
    return str(profile or "").strip() in TAXI_PROJECT_PROFILES


def is_taxi_project_row(project_row: pd.Series | None) -> bool:
    return is_taxi_project_profile(project_topic_profile(project_row))


def project_settings_from_row(row) -> dict[str, Any]:
    settings = {}
    try:
        settings = row.get("settings") or {}
    except Exception:
        settings = {}
    return settings if isinstance(settings, dict) else {}



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


def split_pipe_values(value: Any) -> list[str]:
    """Split platform pipe-separated tags into clean unique labels."""
    raw = str(value or "").replace(";", "|").replace(",", "|")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw.split("|"):
        label = " ".join(str(item or "").split()).strip()
        if not label:
            continue
        key = label.lower().replace("ё", "е")
        if key not in seen:
            seen.add(key)
            result.append(label)
    return result


def numeric_series(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    """Return a parsed numeric series from the first useful existing column.

    If a processed metric column exists but contains only zeros while a raw
    alias is also present, try the raw alias before giving up. This helps with
    older uploaded periods and mixed Brand Analytics exports.
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)

    fallback = pd.Series([0] * len(df), index=df.index, dtype=float)

    for col in columns:
        if col not in df.columns:
            continue
        series = (
            df[col]
            .fillna("")
            .astype(str)
            .str.replace("\ufeff", "", regex=False)
            .str.replace("\u00a0", "", regex=False)
            .str.replace("\u202f", "", regex=False)
            .str.replace(" ", "", regex=False)
            .str.replace("\t", "", regex=False)
            .str.replace(r"[^0-9\-]", "", regex=True)
            .pipe(pd.to_numeric, errors="coerce")
            .fillna(0)
        )
        # Use the first column with a non-zero value. Keep a zero fallback in
        # case all aliases are empty or genuinely zero.
        if float(series.sum()) != 0:
            return series
        fallback = series

    return fallback


AUTO_GENERATED_TAGS_TO_HIDE = {
    "коэффициент", "законы и налоги", "яндекс", "wb такси", "фастен",
    "приложение и сбои", "яндекс про", "забастовка", "аэропорты",
    "детские кресла", "карты и навигация",
    "проблемы, жалобы и негативный опыт", "цены, стоимость и условия",
    "качество продукта или услуги", "наличие, поставки и логистика",
    "монтаж, применение и эксплуатация", "документы, сертификаты и требования",
    "безопасность и пожарные свойства", "безопасность и риски",
    "экология и энергоэффективность", "конкуренты и сравнение на рынке",
    "поддержка и клиентский сервис", "общие обсуждения", "прочие обсуждения", "без тега",
}


def normalize_tag_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def declared_ba_tag_set(messages: pd.DataFrame) -> set[str]:
    """Return Brand Analytics tag names declared in source_tag_columns."""
    if messages is None or messages.empty or "source_tag_columns" not in messages.columns:
        return set()
    tags: set[str] = set()
    for raw in messages["source_tag_columns"].dropna().astype(str).unique().tolist():
        for item in str(raw or "").replace(";", "|").replace(",", "|").split("|"):
            label = " ".join(str(item or "").split()).strip()
            if label:
                tags.add(normalize_tag_key(label))
    return tags


def is_brand_analytics_messages(messages: pd.DataFrame) -> bool:
    if messages is None or messages.empty:
        return False
    if "source_system" in messages.columns:
        values = messages["source_system"].fillna("").astype(str).str.lower()
        if values.eq("brand_analytics").any():
            return True
    return bool(declared_ba_tag_set(messages))


def clean_brand_analytics_tags(messages: pd.DataFrame) -> pd.DataFrame:
    """Keep only real Brand Analytics tags from columns after `Обработано`."""
    if messages is None or messages.empty or "tags" not in messages.columns:
        return messages
    if not is_brand_analytics_messages(messages):
        return messages

    allowed = declared_ba_tag_set(messages)
    out = messages.copy()

    def filter_tags(value: Any) -> str:
        cleaned: list[str] = []
        seen: set[str] = set()
        for label in split_pipe_values(value):
            key = normalize_tag_key(label)
            if not key or key in seen:
                continue
            if key in AUTO_GENERATED_TAGS_TO_HIDE:
                continue
            if allowed and key not in allowed:
                continue
            seen.add(key)
            cleaned.append(label)
        return "|".join(cleaned)

    out["tags"] = out["tags"].apply(filter_tags)
    out["tag_count"] = out["tags"].apply(lambda x: len(split_pipe_values(x)))
    return out


def message_text_column(df: pd.DataFrame) -> str | None:
    for col in ["text", "text_clean", "message_text", "message_raw", "Сообщение"]:
        if col in df.columns:
            return col
    return None


def message_link_column(df: pd.DataFrame) -> str | None:
    for col in ["url", "message_link", "Ссылка"]:
        if col in df.columns:
            return col
    return None


def render_overview_statistics(messages: pd.DataFrame) -> None:
    """Render top-level numbers for the start page."""
    st.subheader("Статистика")
    total_messages = int(len(messages)) if isinstance(messages, pd.DataFrame) else 0
    audience = int(numeric_series(messages, ["audience", "Аудитория"]).sum()) if total_messages else 0
    reach = int(numeric_series(messages, ["views", "Просмотры", "Просмотров", "reach", "Охват"]).sum()) if total_messages else 0
    engagement = int(numeric_series(messages, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]).sum()) if total_messages else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Сообщений", format_int(total_messages))
    c2.metric("Суммарная аудитория", format_int(audience))
    c3.metric("Суммарный охват", format_int(reach))
    c4.metric("Суммарная вовлеченность", format_int(engagement))


def build_tag_statistics(messages: pd.DataFrame) -> pd.DataFrame:
    """Build tag-level analytics: messages, total views/reach and engagement."""
    if messages is None or messages.empty or "tags" not in messages.columns:
        return pd.DataFrame(columns=["Тег", "Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"])

    work = messages.copy()
    work["_tag"] = work["tags"].fillna("").astype(str).apply(split_pipe_values)
    work = work.explode("_tag")
    work["_tag"] = work["_tag"].fillna("").astype(str).str.strip()
    work = work[work["_tag"] != ""]
    if work.empty:
        return pd.DataFrame(columns=["Тег", "Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"])

    work["_audience"] = numeric_series(work, ["audience", "Аудитория"])
    work["_reach"] = numeric_series(work, ["views", "Просмотры", "Просмотров", "reach", "Охват"])
    work["_engagement"] = numeric_series(work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"])
    if "sentiment" in work.columns:
        work["_negative"] = work["sentiment"].fillna("").astype(str).str.lower().str.contains("нег", regex=True).astype(int)
    else:
        work["_negative"] = 0

    stats = (
        work.groupby("_tag", as_index=False)
        .agg(
            Сообщений=("message_id", "nunique") if "message_id" in work.columns else ("_tag", "size"),
            Аудитория=("_audience", "sum"),
            Охват=("_reach", "sum"),
            Вовлеченность=("_engagement", "sum"),
            Негатив=("_negative", "sum"),
        )
        .rename(columns={"_tag": "Тег"})
    )
    for col in ["Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"]:
        if col in stats.columns:
            stats[col] = pd.to_numeric(stats[col], errors="coerce").fillna(0).astype(int)
    stats["Доля негатива"] = (stats["Негатив"] / stats["Сообщений"].replace(0, pd.NA) * 100).fillna(0).round(1)
    return stats.sort_values(["Сообщений", "Аудитория", "Охват", "Вовлеченность"], ascending=False).reset_index(drop=True)


def format_int(value: Any) -> str:
    try:
        return f"{int(float(value)):,}".replace(",", " ")
    except Exception:
        return "0"


def render_tag_statistics(messages: pd.DataFrame) -> None:
    stats = build_tag_statistics(messages)
    if stats.empty:
        return

    st.subheader("Статистика тегов")
    top = stats.head(30).copy()
    display = top.copy()
    for col in ["Сообщений", "Аудитория", "Охват", "Вовлеченность", "Негатив"]:
        if col in display.columns:
            display[col] = display[col].apply(format_int)
    display["Доля негатива"] = display["Доля негатива"].astype(str) + "%"
    st.caption("Теги берутся из системных колонок Brand Analytics после «Обработано». Аудитория, охват и вовлеченность суммируются по сообщениям с выбранным тегом.")
    st.dataframe(display, hide_index=True, use_container_width=True)


def messages_with_tag(messages: pd.DataFrame, tag: str) -> pd.DataFrame:
    if messages is None or messages.empty or "tags" not in messages.columns or not str(tag).strip():
        return pd.DataFrame()
    key = str(tag).strip().lower().replace("ё", "е")
    mask = messages["tags"].fillna("").astype(str).apply(
        lambda value: key in {item.lower().replace("ё", "е") for item in split_pipe_values(value)}
    )
    return messages[mask].copy()


def render_tag_explorer(messages: pd.DataFrame, *, key_prefix: str = "tag_explorer") -> None:
    stats = build_tag_statistics(messages)
    if stats.empty:
        return

    st.markdown("#### Теги")
    st.caption("Выберите тег, чтобы посмотреть все сообщения с этим тегом.")
    options = stats["Тег"].astype(str).tolist()
    labels = {
        str(row["Тег"]): f"{row['Тег']} · {format_int(row['Сообщений'])} сообщ. · {format_int(row['Аудитория'])} аудитория · {format_int(row['Охват'])} охват · {format_int(row['Вовлеченность'])} вовлеч."
        for _, row in stats.iterrows()
    }
    selected_tag = st.selectbox(
        "Тег",
        options,
        format_func=lambda x: labels.get(str(x), str(x)),
        key=f"{key_prefix}_select",
    )
    tag_messages = messages_with_tag(messages, selected_tag)
    st.caption(f"Сообщений с тегом «{selected_tag}»: {format_int(len(tag_messages))}")
    if tag_messages.empty:
        return
    tag_messages = tag_messages.copy()
    tag_messages["Дата"] = tag_messages.get("datetime", "").apply(fmt_date)
    tag_messages["Аудитория"] = numeric_series(tag_messages, ["audience", "Аудитория"]).astype(int)
    tag_messages["Охват"] = numeric_series(tag_messages, ["views", "Просмотры", "Просмотров", "reach", "Охват"]).astype(int)
    tag_messages["Вовлеченность"] = numeric_series(tag_messages, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]).astype(int)
    columns = [c for c in ["Дата", "chat_title", "author", "event_title", "text", "url", "Аудитория", "Охват", "Вовлеченность"] if c in tag_messages.columns]
    st.dataframe(
        tag_messages[columns].rename(columns={
            "chat_title": "Источник/площадка",
            "author": "Автор",
            "event_title": "Инфоповод",
            "text": "Текст",
            "url": "Ссылка",
        }),
        hide_index=True,
        use_container_width=True,
        height=420,
        column_config={"Ссылка": st.column_config.LinkColumn("Ссылка")} if "url" in tag_messages.columns else None,
    )


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
        topic_profile = st.selectbox(
            "Профиль алгоритма",
            list(ALGORITHM_PROFILE_OPTIONS.keys()),
            format_func=lambda x: ALGORITHM_PROFILE_OPTIONS.get(x, x),
            key="new_topic_profile",
            help="Профиль не привязывает платформу к одной отрасли: по умолчанию темы берутся из колонок выгрузки и универсальных правил.",
        )
        viewer_code = st.text_input("Код просмотра", type="password", key="new_viewer_code")
        editor_code = st.text_input("Код редактора", type="password", key="new_editor_code")
        if st.button("Создать проект", type="primary"):
            if not name.strip():
                st.error("Укажите название проекта.")
            else:
                project_id = create_project(
                    project_name=name,
                    description=description,
                    viewer_code=viewer_code,
                    editor_code=editor_code,
                    settings={"topic_profile": topic_profile},
                )
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
            current_settings = project_settings_from_row(row)
            current_profile = str(current_settings.get("topic_profile") or "universal")
            if current_profile not in ALGORITHM_PROFILE_OPTIONS:
                current_profile = "universal"
            new_topic_profile = st.selectbox(
                "Профиль алгоритма",
                list(ALGORITHM_PROFILE_OPTIONS.keys()),
                index=list(ALGORITHM_PROFILE_OPTIONS.keys()).index(current_profile),
                format_func=lambda x: ALGORITHM_PROFILE_OPTIONS.get(x, x),
                key=f"edit_topic_profile_{project_id}",
            )
            st.caption("Коды доступа заполняйте только если хотите заменить текущие.")
            new_viewer_code = st.text_input("Новый код просмотра", type="password", key=f"edit_viewer_code_{project_id}")
            new_editor_code = st.text_input("Новый код редактора", type="password", key=f"edit_editor_code_{project_id}")
            if st.button("Сохранить проект", key=f"save_project_{project_id}"):
                updated_settings = dict(current_settings)
                updated_settings["topic_profile"] = new_topic_profile
                update_project(
                    project_id,
                    project_name=new_name,
                    description=new_description,
                    status=new_status,
                    viewer_code=new_viewer_code,
                    editor_code=new_editor_code,
                    settings=updated_settings,
                )
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
        try:
            canonical = read_uploaded_to_canonical(uploaded, source_system)
        except Exception as exc:
            st.error("Не удалось прочитать файл.")
            st.info(
                "Проверьте, что файл содержит лист/таблицу с сообщениями: дата, текст/сообщение, url/ссылка, источник или автор. "
                "Если в Excel несколько листов, платформа автоматически ищет лист «Сообщения» и пропускает пустые листы."
            )
            st.exception(exc)
            return
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

        with st.expander("Удалить выгрузку без восстановления", expanded=False):
            st.warning(
                "Удаление выгрузки удалит период и все обработанные таблицы этого периода из platform_table_rows. "
                "Также будут удалены ручные правки, которые явно ссылаются на этот период. "
                "Другие проекты и другие периоды не затрагиваются."
            )
            manifest = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
            storage_path = str((manifest or {}).get("storage_path") or "").strip()
            if storage_path:
                st.caption(f"Исходный файл в Storage: {storage_path}")
            delete_storage = st.checkbox(
                "Удалить исходный файл из Supabase Storage, если он был сохранен",
                value=True,
                key=f"delete_storage_{period_id}",
            )
            st.caption("Удаление запускается одной кнопкой. Действие необратимо.")
            if st.button("Удалить выгрузку", key=f"hard_delete_period_{period_id}", type="primary"):
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

                manual_count = int(result.get("manual_rows_deleted") or 0) if isinstance(result, dict) else 0
                table_count = int(result.get("table_rows_deleted") or 0) if isinstance(result, dict) else 0
                storage_deleted = bool(result.get("storage_deleted")) if isinstance(result, dict) else False
                mode = str(result.get("mode") or "") if isinstance(result, dict) else ""
                for warning in (result.get("warnings") or []) if isinstance(result, dict) else []:
                    st.warning(str(warning))
                if delete_storage and storage_path and not storage_deleted and mode != "soft_fallback":
                    st.warning("Выгрузка удалена из базы, но исходный файл в Storage удалить не удалось или он уже отсутствовал.")
                if mode == "soft_fallback":
                    st.warning("Физическое удаление не завершилось, поэтому период скрыт из интерфейса. Для полной очистки можно повторить удаление позже или выполнить очистку в Supabase.")
                else:
                    st.success(f"Выгрузка удалена. Удалено строк данных: {table_count}. Удалено связанных ручных правок: {manual_count}.")
                st.cache_data.clear()
                st.rerun()

def enrich_messages(messages: pd.DataFrame, event_discussions: pd.DataFrame, discussion_messages: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Attach event ids/titles to messages safely.

    Some project profiles, especially Brand Analytics story-based imports, may
    have generated event rows but no discussion/event link table in older
    periods. The previous version assumed that the merge through
    discussion_messages/event_discussions always created an `event_id` column and
    crashed with KeyError when it did not.
    """
    if messages is None or messages.empty:
        return messages if isinstance(messages, pd.DataFrame) else pd.DataFrame()

    out = messages.copy()
    if "message_id" not in out.columns:
        out["message_id"] = out.index.astype(str)
    out["message_id"] = out["message_id"].fillna("").astype(str)

    # 1) Preferred path: message -> discussion -> event links.
    if (
        isinstance(event_discussions, pd.DataFrame)
        and isinstance(discussion_messages, pd.DataFrame)
        and not event_discussions.empty
        and not discussion_messages.empty
        and "discussion_id" in event_discussions.columns
        and "discussion_id" in discussion_messages.columns
    ):
        link = discussion_messages.merge(event_discussions, on="discussion_id", how="left")
        if "message_id" in link.columns and "event_id" in link.columns:
            msg_event = (
                link[["message_id", "event_id"]]
                .dropna(subset=["message_id"])
                .drop_duplicates("message_id")
            )
            msg_event["message_id"] = msg_event["message_id"].fillna("").astype(str)
            out = out.merge(msg_event, on="message_id", how="left", suffixes=("", "_linked"))
            if "event_id_linked" in out.columns:
                if "event_id" not in out.columns:
                    out["event_id"] = out["event_id_linked"]
                else:
                    out["event_id"] = out["event_id"].fillna(out["event_id_linked"])
                out = out.drop(columns=["event_id_linked"])

    # 2) Fallback for story-based BA periods: map source topic/story to event.
    if "event_id" not in out.columns:
        out["event_id"] = ""
    out["event_id"] = out["event_id"].fillna("").astype(str)

    if isinstance(events, pd.DataFrame) and not events.empty and "event_id" in events.columns:
        events_work = events.copy()
        events_work["event_id"] = events_work["event_id"].fillna("").astype(str)

        needs_fallback = out["event_id"].str.strip().eq("").all()
        if needs_fallback:
            topic_map: dict[str, str] = {}
            if "source_main_topic" in events_work.columns:
                for _, row in events_work.dropna(subset=["event_id"]).iterrows():
                    key = str(row.get("source_main_topic") or "").strip().lower()
                    if key and key not in topic_map:
                        topic_map[key] = str(row.get("event_id"))
            if "event_title" in events_work.columns:
                for _, row in events_work.dropna(subset=["event_id"]).iterrows():
                    key = str(row.get("event_title") or "").strip().lower()
                    if key and key not in topic_map:
                        topic_map[key] = str(row.get("event_id"))

            if topic_map:
                source_col = None
                for candidate in ["source_main_topic", "source_topics", "event_title"]:
                    if candidate in out.columns:
                        source_col = candidate
                        break
                if source_col:
                    keys = out[source_col].fillna("").astype(str).str.split(";").str[0].str.strip().str.lower()
                    out["event_id"] = keys.map(topic_map).fillna(out["event_id"])

        # Add/fill event title without requiring a merge key to exist.
        if "event_title" in events_work.columns:
            title_map = (
                events_work.drop_duplicates("event_id")
                .set_index("event_id")["event_title"]
                .fillna("")
                .astype(str)
                .to_dict()
            )
            mapped_titles = out["event_id"].fillna("").astype(str).map(title_map).fillna("")
            if "event_title" not in out.columns:
                out["event_title"] = mapped_titles
            else:
                current = out["event_title"].fillna("").astype(str)
                out["event_title"] = current.where(current.str.strip().ne(""), mapped_titles)

    if "event_title" not in out.columns:
        out["event_title"] = ""
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
    """Build a neutral, source-agnostic event description."""
    text = " ".join(group.get("event_summary", pd.Series(dtype=str)).fillna("").astype(str).tolist())
    tags = " | ".join(sorted(set("|".join(group.get("main_tags", pd.Series(dtype=str)).fillna("").astype(str)).split("|")) - {""}))
    low = f"{text} {tags}".lower().replace("ё", "е")
    patterns = [
        ("проблемы, жалобы и негативный опыт", ["жалоб", "проблем", "негатив", "ошиб", "не работает", "плохо", "брак", "дефект"]),
        ("цены, стоимость и условия", ["цен", "стоим", "скид", "акци", "тариф", "услов", "дорого", "дешев"]),
        ("качество продукта или услуги", ["качеств", "материал", "характерист", "свойств", "надежн", "эффектив"]),
        ("наличие, поставки и логистика", ["достав", "налич", "склад", "постав", "логист", "срок", "отгруз"]),
        ("монтаж, применение и эксплуатация", ["монтаж", "установ", "примен", "использ", "эксплуатац", "строител", "утепл", "изоляц"]),
        ("документы, сертификаты и требования", ["сертифик", "документ", "декларац", "гост", "снип", "требован", "стандарт"]),
        ("безопасность и риски", ["безопас", "пожар", "огне", "горюч", "опасн", "токсич"]),
        ("экология и энергоэффективность", ["эколог", "энергоэфф", "энергосбереж", "устойчив", "переработ"]),
        ("конкуренты и сравнение", ["конкур", "аналог", "сравнен", "рынок", "бренд"]),
        ("клиентский сервис и поддержка", ["поддерж", "сервис", "менеджер", "дилер", "магазин", "клиент"]),
    ]
    signals = []
    for label, keys in patterns:
        if any(k in low for k in keys):
            signals.append(label)
    if signals:
        return "В теме обсуждались: " + "; ".join(signals[:5]) + "."
    return f"В теме обсуждались: {tags}." if tags else "В теме обсуждались связанные сообщения выбранного периода."


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

    word = st.text_input("Фильтр по слову в сообщениях", placeholder="Например: доставка, качество, сертификат")
    filtered_events = events_agg.copy()
    filtered_messages = messages.copy()

    text_col = message_text_column(filtered_messages)
    if word.strip() and text_col:
        mask = filtered_messages[text_col].fillna("").astype(str).str.contains(word.strip(), case=False, regex=False)
        filtered_messages = filtered_messages[mask]
        if "event_id" in filtered_messages.columns:
            allowed = set(filtered_messages["event_id"].dropna().astype(str))
            filtered_events = filtered_events[filtered_events["event_ids"].apply(lambda ids: bool(set(map(str, ids)) & allowed))]
        st.caption(f"Найдено сообщений: {len(filtered_messages):,}".replace(",", " "))
        msg_view = filtered_messages.copy()
        if not msg_view.empty:
            msg_view["Дата"] = msg_view.get("datetime", "").apply(fmt_date)
            if text_col:
                msg_view["Текст"] = msg_view[text_col].fillna("").astype(str).str.slice(0, 700)
            link_col = message_link_column(msg_view)
            msg_view["Ссылка"] = msg_view[link_col].fillna("").astype(str) if link_col else ""
            columns = [c for c in ["Дата", "chat_title", "author", "event_title", "Текст", "Ссылка"] if c in msg_view.columns]
            st.dataframe(
                msg_view[columns].rename(columns={
                    "chat_title": "Источник/площадка",
                    "author": "Автор",
                    "event_title": "Инфоповод",
                }).head(500),
                hide_index=True,
                use_container_width=True,
                column_config={"Ссылка": st.column_config.LinkColumn("Ссылка")},
            )

    table = filtered_events.copy()
    table["Период"] = table.apply(
        lambda r: f"{fmt_date(r.get('start_date'))}–{fmt_date(r.get('end_date'))}"
        if fmt_date(r.get("start_date")) != fmt_date(r.get("end_date"))
        else fmt_date(r.get("start_date")),
        axis=1,
    )
    table["Негатив"] = (table["negative_share"] * 100).round(1).astype(str) + "%"
    show = table[["title", "description", "Период", "message_count", "chat_count", "Негатив", "importance_score"]].rename(columns={
        "title": "Сюжет / инфоповод",
        "description": "Описание",
        "message_count": "Сообщений",
        "chat_count": "Источников",
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

    st.caption("Сообщения доступны ниже в блоке «Ключевые сообщения / Вся лента».")



def _value_from_row(row: pd.Series, *columns: str) -> str:
    """Return the first non-empty value from a message row."""
    for col in columns:
        if col in row.index:
            value = str(row.get(col) or "").strip()
            if value and value.lower() not in {"nan", "none", "nat", "null"}:
                return value
    return ""


def _render_message_list(view: pd.DataFrame, *, text_col: str | None, link_col: str | None) -> None:
    """Render messages as readable cards instead of a dataframe."""
    if view is None or view.empty:
        st.info("Сообщений для показа нет.")
        return

    for _, row in view.iterrows():
        date_text = fmt_date(row.get("datetime")) if "datetime" in row.index else ""
        source = _value_from_row(row, "chat_title", "platform", "source", "Источник", "Место публикации")
        author = _value_from_row(row, "author", "Автор")
        sentiment = _value_from_row(row, "sentiment", "Тональность")
        event_title = _value_from_row(row, "event_title", "source_main_topic", "Сюжет")
        tags = _value_from_row(row, "tags", "Теги").replace("|", ", ")
        audience = int(row.get("_audience", 0) or 0)
        reach = int(row.get("_reach", 0) or 0)
        engagement = int(row.get("_engagement", 0) or 0)
        text = str(row.get(text_col, "") or "").strip() if text_col else ""
        link = str(row.get(link_col, "") or "").strip() if link_col else ""

        meta_parts = [part for part in [date_text, source, author, sentiment] if part]
        metrics_parts = [f"аудитория: {format_int(audience)}", f"охват: {format_int(reach)}", f"вовлеченность: {format_int(engagement)}"]

        st.markdown("---")
        if meta_parts:
            st.caption(" · ".join(meta_parts))
        if event_title:
            st.markdown(f"**Инфоповод:** {event_title}")
        if tags:
            st.caption(f"Теги: {tags}")
        st.markdown(f"*{' · '.join(metrics_parts)}*")
        st.write(text[:1800] if text else "—")
        if link.startswith("http"):
            st.markdown(f"[Открыть сообщение]({link})")


def render_messages_block(messages: pd.DataFrame) -> None:
    """Render global key messages and full feed as a readable list."""
    st.subheader("Ключевые сообщения")
    if messages is None or messages.empty:
        st.info("Сообщения не найдены.")
        return

    mode = st.radio(
        "Режим просмотра сообщений",
        ["Ключевые сообщения", "Вся лента"],
        horizontal=True,
        key="messages_block_mode",
    )

    work = messages.copy()
    text_col = message_text_column(work)
    link_col = message_link_column(work)
    work["_audience"] = numeric_series(work, ["audience", "Аудитория"]).astype(int)
    work["_reach"] = numeric_series(work, ["views", "Просмотры", "Просмотров", "reach", "Охват"]).astype(int)
    work["_engagement"] = numeric_series(work, ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]).astype(int)

    if mode == "Ключевые сообщения":
        st.caption("Показаны 15 сообщений с максимальной вовлеченностью. Если вовлеченность равна 0, дополнительными критериями выступают охват и аудитория.")
        view = work.sort_values(["_engagement", "_reach", "_audience"], ascending=False).head(15).copy()
    else:
        search = st.text_input("Поиск по всей ленте", placeholder="Введите слово или фразу", key="full_feed_search")
        view = work.copy()
        if search.strip() and text_col:
            view = view[view[text_col].fillna("").astype(str).str.contains(search.strip(), case=False, regex=False)]
        feed_limit = int(st.number_input("Сколько сообщений показать", min_value=25, max_value=1000, value=100, step=25, key="full_feed_limit"))
        view = view.sort_values("datetime", ascending=False) if "datetime" in view.columns else view
        st.caption(f"Найдено сообщений: {format_int(len(view))}. Показано: {format_int(min(len(view), feed_limit))}.")
        view = view.head(feed_limit).copy()

    _render_message_list(view, text_col=text_col, link_col=link_col)



# -----------------------------------------------------------------------------
# Driver chats dashboard profile
# -----------------------------------------------------------------------------


def taxi_bool_negative(messages: pd.DataFrame) -> pd.Series:
    if messages is None or messages.empty:
        return pd.Series(dtype=bool)
    if "is_negative" in messages.columns:
        return messages["is_negative"].astype(str).str.lower().isin(["true", "1", "yes", "да", "негатив", "negative"])
    if "sentiment" in messages.columns:
        return messages["sentiment"].fillna("").astype(str).str.lower().str.contains("нег|negative|отриц", regex=True, na=False)
    return pd.Series([False] * len(messages), index=messages.index)


def taxi_bool_positive(messages: pd.DataFrame) -> pd.Series:
    if messages is None or messages.empty:
        return pd.Series(dtype=bool)
    if "sentiment" in messages.columns:
        return messages["sentiment"].fillna("").astype(str).str.lower().str.contains("позит|positive|полож", regex=True, na=False)
    return pd.Series([False] * len(messages), index=messages.index)


def render_taxi_overview_statistics(events_agg: pd.DataFrame, messages: pd.DataFrame) -> None:
    """Top-level metrics for the driver-chat profile."""
    st.subheader("Статистика")
    total_messages = int(len(messages)) if isinstance(messages, pd.DataFrame) else 0
    chat_col = "chat_title" if isinstance(messages, pd.DataFrame) and "chat_title" in messages.columns else "chat_id" if isinstance(messages, pd.DataFrame) and "chat_id" in messages.columns else None
    author_col = "author" if isinstance(messages, pd.DataFrame) and "author" in messages.columns else "author_id" if isinstance(messages, pd.DataFrame) and "author_id" in messages.columns else None
    chat_count = int(messages[chat_col].fillna("").astype(str).replace("", pd.NA).dropna().nunique()) if chat_col and total_messages else 0
    author_count = int(messages[author_col].fillna("").astype(str).replace("", pd.NA).dropna().nunique()) if author_col and total_messages else 0
    neg_count = int(taxi_bool_negative(messages).sum()) if total_messages else 0
    high_count = 0
    if isinstance(events_agg, pd.DataFrame) and not events_agg.empty and "importance_score" in events_agg.columns:
        importance = pd.to_numeric(events_agg["importance_score"], errors="coerce").fillna(0)
        threshold = float(importance.quantile(0.75)) if len(importance) else 0
        high_count = int((importance >= threshold).sum()) if threshold else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Инфоповодов", format_int(len(events_agg) if isinstance(events_agg, pd.DataFrame) else 0))
    c2.metric("Сообщений", format_int(total_messages))
    c3.metric("Чатов", format_int(chat_count))
    c4.metric("Негатив", f"{(neg_count / total_messages * 100):.0f}%" if total_messages else "0%")
    c5.metric("Высокая важность", format_int(high_count))
    if author_count:
        st.caption(f"Уникальных авторов: {format_int(author_count)}")


def normalize_taxi_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def taxi_macro_title(row: pd.Series) -> str:
    """Map detailed taxi events to report-level topics."""
    text = normalize_taxi_text(" ".join([
        str(row.get("event_title") or ""),
        str(row.get("event_summary") or ""),
        str(row.get("display_description") or ""),
        str(row.get("main_tags") or ""),
        str(row.get("microtopic") or ""),
    ]))
    rules = [
        ("Забастовка, бойкот и коллективные действия", ["strike", "забаст", "бойкот", "стачк", "митинг", "коллективн"]),
        ("Законы, налоги и регулирование такси", ["tax_law", "налог", "патент", "самозан", "минтранс", "реестр", "закон", "разрешен", "лиценз"]),
        ("Коэффициенты, приоритет и тарифы", ["coeff_priority", "коэфф", "коэф", "кэф", "приоритет", "тариф", "подач"]),
        ("Сбои и ошибки в приложении", ["app_bug", "сбой", "ошиб", "завис", "не работает", "яндекс про", "обновлен", "приложен"]),
        ("Проблемы с заказами в приложении", ["app_orders", "заказ", "назнач", "раздач", "цепоч", "не приход"]),
        ("Оплата, выплаты и удержания", ["payments", "оплат", "выплат", "деньг", "баланс", "удерж", "комисс"]),
        ("Блокировки и доступ к аккаунту", ["account_block", "блок", "аккаунт", "доступ", "вериф", "фотоконтроль"]),
        ("Детские кресла и требования к заказам", ["child_seat", "кресл", "детск", "ребен", "ребён"]),
        ("Карты, адреса и навигация", ["gps_map", "карт", "адрес", "навиг", "геолока", "gps", "маршрут"]),
        ("Заказы и правила в аэропортах", ["airport", "аэропорт", "шереметьево", "домодедово", "внуково", "пулково"]),
        ("Поддержка, парк и диспетчерские вопросы", ["support", "поддерж", "таксопарк", "диспетчер", "парк"]),
        ("Запуск и обсуждение WB Такси", ["wb_launch", "wb такси", "wildberries", "вайлдбер", "вб такси"]),
        ("Обсуждение сервиса Фастен", ["fasten", "фастен", "fasten_service"]),
        ("Общее обсуждение Яндекса", ["general_yandex", "яндекс", "yandex", "яша"]),
    ]
    for title, keys in rules:
        if any(key in text for key in keys):
            return title
    raw_title = str(row.get("event_title") or "").strip()
    return raw_title or "Прочие обсуждения"


def aggregate_taxi_events(events: pd.DataFrame, level: str = "balanced") -> pd.DataFrame:
    """Aggregate taxi events for three levels of detail."""
    if events is None or events.empty:
        return pd.DataFrame()
    df = events.copy()
    if "event_id" not in df.columns:
        df["event_id"] = df.index.astype(str)
    if "event_title" not in df.columns:
        df["event_title"] = "Без названия"
    if level == "detailed":
        df["__group_title"] = df["event_title"].fillna("Без названия").astype(str).replace("", "Без названия")
        df["__group_key"] = df["event_id"].astype(str)
    elif level == "macro":
        df["__group_title"] = df.apply(taxi_macro_title, axis=1)
        df["__group_key"] = df["__group_title"].map(normalize_taxi_text)
    else:
        df["__group_title"] = df["event_title"].fillna("Без названия").astype(str).replace("", "Без названия")
        df["__group_key"] = df["__group_title"].map(normalize_taxi_text)

    rows: list[dict[str, Any]] = []
    for key, group in df.groupby("__group_key", dropna=False):
        title = str(group["__group_title"].iloc[0] or "Без названия")
        tags = " | ".join(sorted(set("|".join(group.get("main_tags", pd.Series(dtype=str)).fillna("").astype(str)).split("|")) - {""}))
        msg_count = int(pd.to_numeric(group.get("message_count", 0), errors="coerce").fillna(0).sum())
        neg_count = int(pd.to_numeric(group.get("negative_count", 0), errors="coerce").fillna(0).sum())
        rows.append({
            "group_key": str(key),
            "title": title,
            "description": pick_event_description(group),
            "tags": tags,
            "start_date": pd.to_datetime(group.get("start_date"), errors="coerce").min(),
            "end_date": pd.to_datetime(group.get("end_date"), errors="coerce").max(),
            "message_count": msg_count,
            "chat_count": int(pd.to_numeric(group.get("chat_count", 0), errors="coerce").fillna(0).sum()),
            "negative_count": neg_count,
            "importance_score": float(pd.to_numeric(group.get("importance_score", 0), errors="coerce").fillna(0).max()),
            "event_ids": list(group["event_id"].astype(str)),
            "source_event_count": int(group["event_id"].astype(str).nunique()),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["negative_share"] = out.apply(lambda r: float(r["negative_count"]) / float(r["message_count"]) if float(r.get("message_count") or 0) else 0.0, axis=1)
    return out.sort_values(["importance_score", "message_count"], ascending=False).reset_index(drop=True)


def build_taxi_auto_summary(messages: pd.DataFrame, events_agg: pd.DataFrame, periods: pd.DataFrame, selected_period_ids: list[str]) -> str:
    """Readable summary for driver-chat projects."""
    total = int(len(messages)) if isinstance(messages, pd.DataFrame) else 0
    if not total:
        return "По выбранному периоду пока нет сообщений для саммари."
    neg_count = int(taxi_bool_negative(messages).sum())
    pos_count = int(taxi_bool_positive(messages).sum())
    neutral_count = max(0, total - neg_count - pos_count)
    top_events = "нет выраженных тем"
    if isinstance(events_agg, pd.DataFrame) and not events_agg.empty:
        top = events_agg.sort_values("message_count", ascending=False).head(6)
        top_events = "; ".join(f"{r['title']} — {format_int(r['message_count'])}" for _, r in top.iterrows())
    chat_col = "chat_title" if "chat_title" in messages.columns else "chat_id" if "chat_id" in messages.columns else None
    top_chats = "нет данных"
    if chat_col:
        vc = messages[chat_col].fillna("").astype(str).replace("", pd.NA).dropna().value_counts().head(5)
        if not vc.empty:
            top_chats = "; ".join(f"{name} — {format_int(count)}" for name, count in vc.items())
    lines = [
        f"За выбранный период собрано {format_int(total)} сообщений. Тональность: {neutral_count / total * 100:.0f}% нейтрал, {neg_count / total * 100:.0f}% негатив, {pos_count / total * 100:.0f}% позитив.",
        f"Основные обсуждения: {top_events}.",
        f"Наиболее активные чаты: {top_chats}.",
    ]
    return "\n".join("• " + line for line in lines)


def render_taxi_summary(project_id: str, period_ids: list[str], messages: pd.DataFrame, events_agg: pd.DataFrame, periods: pd.DataFrame, role: str) -> None:
    key = "summary::taxi::" + "|".join(sorted(map(str, period_ids)))
    saved = get_manual(project_id, "summaries", key)
    auto = build_taxi_auto_summary(messages, events_agg, periods, period_ids)
    text = str((saved or {}).get("summary") or "").strip() or auto
    st.subheader("Саммари")
    st.markdown(text.replace("\n", "  \n"))
    if role_rank(role) >= role_rank("editor"):
        with st.expander("Редактировать саммари", expanded=False):
            edited = st.text_area("Саммари", value=text, height=220, key=f"taxi_summary_{key}")
            if st.button("Сохранить саммари", key=f"save_taxi_summary_{key}"):
                save_manual(project_id, "summaries", key, {"summary": edited, "period_ids": period_ids, "profile": "driver_chats"})
                st.success("Саммари сохранено.")
                st.rerun()


def render_taxi_dashboard(
    project_id: str,
    project_name: str,
    role: str,
    selected_period_ids: list[str],
    periods: pd.DataFrame,
    events: pd.DataFrame,
    messages: pd.DataFrame,
    manual_state: dict[str, Any],
) -> None:
    """Dedicated UI for driver-chat digest projects inside the platform namespace."""
    st.header(project_name)
    st.caption("Профиль проекта: дайджест водительских чатов. Данные хранятся отдельно внутри текущего проекта платформы.")
    level = st.sidebar.selectbox(
        "Уровень сборки инфоповодов",
        ["balanced", "macro", "detailed"],
        index=0,
        format_func=lambda x: {
            "balanced": "Сбалансировано — рекомендовано",
            "macro": "Крупные темы — для отчета",
            "detailed": "Подробно — первичные инфоповоды",
        }.get(x, x),
        key="taxi_event_detail_level",
    )
    events_agg = aggregate_taxi_events(events, level=level)
    render_taxi_overview_statistics(events_agg, messages)
    render_taxi_summary(project_id, selected_period_ids, messages, events_agg, periods, role)
    st.markdown("---")
    render_events(project_id, role, events_agg, messages, manual_state)
    st.markdown("---")
    render_messages_block(messages)
    if len(selected_period_ids) >= 2:
        with st.expander("Динамика по периодам", expanded=False):
            render_period_dynamics(messages, periods, selected_period_ids)


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
    current_project_row = project_row.iloc[0] if not project_row.empty else None
    project_name = str(current_project_row.get("project_name") if current_project_row is not None else project_id)
    project_profile = project_topic_profile(current_project_row)
    st.sidebar.markdown(f"**Текущий проект:**  \n{project_name}")
    st.sidebar.caption(f"Профиль: {ALGORITHM_PROFILE_OPTIONS.get(project_profile, project_profile)}")

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

    if is_taxi_project_profile(project_profile):
        render_taxi_dashboard(
            project_id,
            project_name,
            role,
            selected_period_ids,
            periods,
            events,
            enriched_messages,
            manual_state,
        )
        return

    events_agg = aggregate_events(events)

    # Brand Analytics projects must show only system tags from columns after
    # `Обработано`. This prevents legacy taxi/generic labels from appearing
    # in the tag block after algorithm updates.
    enriched_messages = clean_brand_analytics_tags(enriched_messages)

    render_overview_statistics(enriched_messages)
    render_summary(project_id, selected_period_ids, enriched_messages, events_agg, periods, role)
    render_tag_statistics(enriched_messages)
    render_events(project_id, role, events_agg, enriched_messages, manual_state)
    render_messages_block(enriched_messages)

    if len(selected_period_ids) >= 2:
        with st.expander("Динамика по периодам", expanded=False):
            render_period_dynamics(enriched_messages, periods, selected_period_ids)


if __name__ == "__main__":
    main()
