"""Supabase persistence layer for the multi-project digest platform.

This module intentionally uses a separate `platform_*` table namespace so the
existing single-project dashboard data (`dashboard_*`) remains untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

try:
    import streamlit as st
except Exception:  # pragma: no cover
    st = None

try:
    from supabase import Client, create_client
except Exception:  # pragma: no cover
    Client = Any  # type: ignore
    create_client = None  # type: ignore

TABLES = ["events", "discussions", "messages", "discussion_messages", "event_discussions"]
ROW_KEY_COLUMNS = {
    "events": ["event_id"],
    "discussions": ["discussion_id"],
    "messages": ["message_id"],
    "discussion_messages": ["discussion_id", "message_id"],
    "event_discussions": ["event_id", "discussion_id"],
}
PAGE_SIZE = 1000
CHUNK_SIZE = 400


def _secret_value(*names: str) -> str:
    for name in names:
        if st is not None:
            try:
                if name in st.secrets:
                    return str(st.secrets[name])
            except Exception:
                pass
            try:
                # Support [supabase] url/key too.
                section_key = name.lower().replace("supabase_", "")
                if "supabase" in st.secrets and section_key in st.secrets["supabase"]:
                    return str(st.secrets["supabase"][section_key])
            except Exception:
                pass
        value = os.getenv(name)
        if value:
            return str(value)
    return ""


def normalize_supabase_url(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    for suffix in ["/rest/v1", "/auth/v1", "/storage/v1"]:
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def supabase_configured() -> bool:
    return bool(_secret_value("SUPABASE_URL") and _secret_value("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY"))


def get_supabase_client() -> Client:
    if create_client is None:
        raise RuntimeError("Пакет supabase не установлен. Добавьте supabase>=2 в requirements.txt")
    url = normalize_supabase_url(_secret_value("SUPABASE_URL"))
    key = _secret_value("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("Не заданы SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY в secrets.")
    return create_client(url, key)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_slug(value: str, fallback: str = "item") -> str:
    value = str(value or "").strip().lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or fallback


def ascii_storage_component(value: str, fallback: str = "file") -> str:
    value = str(value or "").strip()
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    return value or fallback


def hash_code(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_code(value: str, expected_hash: str) -> bool:
    return bool(value and expected_hash and hash_code(value) == str(expected_hash))


def make_project_id(project_name: str) -> str:
    base = safe_slug(project_name, "project")[:70]
    digest = hashlib.md5(str(project_name).encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{base}_{digest}"


def make_period_id(project_id: str, period_name: str, source_filename: str = "") -> str:
    base = safe_slug(period_name, "period")[:70]
    digest = hashlib.md5(f"{project_id}|{period_name}|{source_filename}".encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{base}_{digest}"


def normalize_json_value(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def chunked(items: list[Any], size: int = CHUNK_SIZE) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fetch_all(
    client: Client,
    table: str,
    *,
    filters: dict[str, Any] | None = None,
    order: str | None = None,
    select: str = "*",
) -> list[dict[str, Any]]:
    """Paginated select with optional column projection.

    `select` is important for large payload tables: for dashboard rendering we
    usually only need `period_id` and `payload`, not every metadata column.
    """
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        query = client.table(table).select(select)
        if filters:
            for key, value in filters.items():
                if isinstance(value, (list, tuple, set)):
                    value_list = [str(v) for v in value if str(v).strip()]
                    if not value_list:
                        return rows
                    query = query.in_(key, value_list)
                else:
                    query = query.eq(key, value)
        if order:
            query = query.order(order)
        response = query.range(start, start + PAGE_SIZE - 1).execute()
        data = response.data or []
        rows.extend(data)
        if len(data) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def list_projects(include_inactive: bool = False) -> pd.DataFrame:
    client = get_supabase_client()
    rows = _fetch_all(client, "platform_projects", order="created_at")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if "status" not in df.columns:
        df["status"] = "active"
    df["status"] = df["status"].fillna("active").astype(str)
    if not include_inactive:
        df = df[df["status"].str.lower().isin(["active", ""])]
    for col in ["created_at", "updated_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df.sort_values("project_name")


def create_project(
    *,
    project_name: str,
    description: str = "",
    viewer_code: str = "",
    editor_code: str = "",
    settings: dict[str, Any] | None = None,
) -> str:
    client = get_supabase_client()
    project_id = make_project_id(project_name)
    payload = {
        "project_id": project_id,
        "project_name": project_name.strip() or project_id,
        "description": description.strip(),
        "status": "active",
        "viewer_code_hash": hash_code(viewer_code),
        "editor_code_hash": hash_code(editor_code),
        "settings": settings or {},
        "updated_at": now_iso(),
    }
    client.table("platform_projects").upsert(payload, on_conflict="project_id").execute()
    return project_id


def update_project(project_id: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"updated_at": now_iso()}
    for key in ["project_name", "description", "status"]:
        if key in fields and fields[key] is not None:
            payload[key] = str(fields[key]).strip()
    if "viewer_code" in fields and fields["viewer_code"]:
        payload["viewer_code_hash"] = hash_code(fields["viewer_code"])
    if "editor_code" in fields and fields["editor_code"]:
        payload["editor_code_hash"] = hash_code(fields["editor_code"])
    if "settings" in fields and fields["settings"] is not None:
        payload["settings"] = fields["settings"]
    get_supabase_client().table("platform_projects").update(payload).eq("project_id", project_id).execute()


def resolve_project_access(access_code: str) -> tuple[str | None, str]:
    """Return (project_id, role) for a project code. Role is viewer/editor."""
    if not access_code:
        return None, "none"
    projects = list_projects(include_inactive=False)
    if projects.empty:
        return None, "none"
    for _, row in projects.iterrows():
        if check_code(access_code, str(row.get("editor_code_hash") or "")):
            return str(row["project_id"]), "editor"
        if check_code(access_code, str(row.get("viewer_code_hash") or "")):
            return str(row["project_id"]), "viewer"
    return None, "none"


def _normalize_date_for_db(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text or text.lower() in {"nat", "nan", "none"}:
        return None
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None
    ru = re.match(r"^(\d{1,2})[.](\d{1,2})[.](\d{2,4})$", text)
    if ru:
        day, month, year = [int(x) for x in ru.groups()]
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    dt = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(dt):
        return None
    return dt.date().isoformat()


def list_periods(project_id: str, include_inactive: bool = False) -> pd.DataFrame:
    rows = _fetch_all(get_supabase_client(), "platform_periods", filters={"project_id": project_id}, order="uploaded_at")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if "status" not in df.columns:
        df["status"] = "active"
    df["status"] = df["status"].fillna("active").astype(str)
    if not include_inactive:
        df = df[df["status"].str.lower().isin(["active", ""])]
    else:
        df = df[~df["status"].str.lower().isin(["deleted", "удален", "удалён"])]
    for col in ["date_from", "date_to", "uploaded_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df.sort_values("uploaded_at", ascending=False)


def detect_period_dates(messages: pd.DataFrame) -> tuple[str | None, str | None]:
    if messages is None or messages.empty or "datetime" not in messages.columns:
        return None, None
    dt = pd.to_datetime(messages["datetime"], errors="coerce").dropna()
    if dt.empty:
        return None, None
    return dt.min().date().isoformat(), dt.max().date().isoformat()


def dataframe_to_payload_records(df: pd.DataFrame, table_name: str, project_id: str, period_id: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    key_cols = ROW_KEY_COLUMNS.get(table_name, [])
    work = df.copy()
    for col in work.columns:
        if pd.api.types.is_datetime64_any_dtype(work[col]):
            work[col] = work[col].dt.strftime("%Y-%m-%dT%H:%M:%S")
    records: list[dict[str, Any]] = []
    for idx, row in work.iterrows():
        payload = {str(k): normalize_json_value(v) for k, v in row.to_dict().items()}
        if key_cols and all(str(payload.get(col) or "").strip() for col in key_cols):
            row_id = "::".join(str(payload[col]) for col in key_cols)
        else:
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            row_id = f"row_{idx}_{hashlib.md5(raw.encode('utf-8')).hexdigest()[:10]}"
        records.append({
            "project_id": project_id,
            "period_id": period_id,
            "table_name": table_name,
            "row_id": str(row_id),
            "payload": payload,
            "updated_at": now_iso(),
        })
    return records


def save_processed_tables(
    *,
    project_id: str,
    period_id: str,
    period_name: str,
    source_filename: str,
    tables: dict[str, pd.DataFrame],
    manifest: dict[str, Any] | None = None,
    date_from: Any = None,
    date_to: Any = None,
    replace: bool = True,
) -> None:
    client = get_supabase_client()
    messages = tables.get("messages", pd.DataFrame())
    auto_from, auto_to = detect_period_dates(messages)
    period_payload = {
        "project_id": project_id,
        "period_id": period_id,
        "period_name": period_name,
        "date_from": _normalize_date_for_db(date_from) or auto_from,
        "date_to": _normalize_date_for_db(date_to) or auto_to,
        "source_filename": source_filename,
        "status": "active",
        "manifest": manifest or {},
        "uploaded_at": now_iso(),
    }
    client.table("platform_periods").upsert(period_payload, on_conflict="period_id").execute()
    if replace:
        client.table("platform_table_rows").delete().eq("project_id", project_id).eq("period_id", period_id).execute()
    for table_name in TABLES:
        records = dataframe_to_payload_records(tables.get(table_name, pd.DataFrame()), table_name, project_id, period_id)
        for batch in chunked(records):
            client.table("platform_table_rows").upsert(batch, on_conflict="project_id,period_id,table_name,row_id").execute()


def load_table(project_id: str, period_ids: list[str], table_name: str) -> pd.DataFrame:
    """Load a generated table for multiple periods in batched Supabase queries."""
    period_ids = [str(pid) for pid in (period_ids or []) if str(pid).strip()]
    if not period_ids:
        return pd.DataFrame()

    all_payloads: list[dict[str, Any]] = []
    client = get_supabase_client()

    # Keep batches modest to avoid overly long PostgREST URLs for large selections.
    for period_batch in chunked(period_ids, 50):
        rows = _fetch_all(
            client,
            "platform_table_rows",
            filters={"project_id": project_id, "period_id": list(period_batch), "table_name": table_name},
            select="period_id,payload",
        )
        for row in rows:
            payload = row.get("payload") or {}
            if isinstance(payload, dict):
                payload.setdefault("project_id", project_id)
                payload.setdefault("period_id", row.get("period_id") or payload.get("period_id"))
                all_payloads.append(payload)
    return pd.DataFrame(all_payloads)


def _prefix_ids(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Prefix generated ids by period without a row-wise DataFrame.apply."""
    if df is None or df.empty or "period_id" not in df.columns:
        return df
    df = df.copy()
    period = df["period_id"].fillna("").astype(str)
    for col in columns:
        if col not in df.columns:
            continue
        values = df[col].fillna("").astype(str)
        non_empty = values.str.strip().ne("")
        already_prefixed = pd.Series(
            [v.startswith(p + "__") if p else False for v, p in zip(values.tolist(), period.tolist())],
            index=df.index,
        )
        mask = non_empty & period.str.strip().ne("") & (~already_prefixed)
        if mask.any():
            df.loc[mask, col] = period.loc[mask] + "__" + values.loc[mask]
    return df


def load_generated_tables(project_id: str, period_ids: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    loaded = [load_table(project_id, period_ids, table_name) for table_name in TABLES]
    events, discussions, messages, discussion_messages, event_discussions = loaded
    events = _prefix_ids(events, ["event_id"])
    discussions = _prefix_ids(discussions, ["discussion_id"])
    discussion_messages = _prefix_ids(discussion_messages, ["discussion_id"])
    event_discussions = _prefix_ids(event_discussions, ["event_id", "discussion_id"])
    for df, cols in [(events, ["start_date", "end_date"]), (discussions, ["start_date", "end_date"]), (messages, ["datetime"])]:
        for col in cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
    return events, discussions, messages, discussion_messages, event_discussions


def update_period_metadata(project_id: str, period_id: str, **fields: Any) -> None:
    payload: dict[str, Any] = {}
    for key in ["period_name", "source_filename", "status"]:
        if key in fields and fields[key] is not None:
            payload[key] = str(fields[key]).strip()
    if "date_from" in fields and fields["date_from"] is not None:
        payload["date_from"] = _normalize_date_for_db(fields["date_from"])
    if "date_to" in fields and fields["date_to"] is not None:
        payload["date_to"] = _normalize_date_for_db(fields["date_to"])
    if "manifest_updates" in fields and fields["manifest_updates"]:
        current = get_period(project_id, period_id) or {}
        manifest = current.get("manifest") if isinstance(current.get("manifest"), dict) else {}
        manifest.update(fields["manifest_updates"])
        payload["manifest"] = manifest
    if payload:
        get_supabase_client().table("platform_periods").update(payload).eq("project_id", project_id).eq("period_id", period_id).execute()


def get_period(project_id: str, period_id: str) -> dict[str, Any] | None:
    data = get_supabase_client().table("platform_periods").select("*").eq("project_id", project_id).eq("period_id", period_id).limit(1).execute().data or []
    return data[0] if data else None


def _manual_row_references_period(row: dict[str, Any], period_id: str) -> bool:
    """Return True when a manual row clearly belongs to the deleted upload."""
    period_id = str(period_id or "").strip()
    if not period_id:
        return False
    prefix = f"{period_id}__"
    row_key = str(row.get("row_key") or "")
    table_name = str(row.get("table_name") or "")
    if period_id in row_key or prefix in row_key:
        return True
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        return False
    if str(payload.get("period_id") or "") == period_id:
        return True
    if table_name == "summaries":
        raw_period_ids = payload.get("period_ids") or payload.get("selected_period_ids") or ""
        if isinstance(raw_period_ids, (list, tuple, set)) and period_id in {str(x) for x in raw_period_ids}:
            return True
        if period_id in str(raw_period_ids):
            return True
    for key in ["event_id", "source_event_id", "target_event_id", "group_key", "summary_key"]:
        value = str(payload.get(key) or "")
        if period_id in value or prefix in value:
            return True
    return False


def delete_manual_rows_for_period(project_id: str, period_id: str) -> int:
    """Delete manual rows that are clearly tied to a period/upload."""
    client = get_supabase_client()
    rows = _fetch_all(client, "platform_manual_rows", filters={"project_id": project_id})
    row_keys = [
        str(row.get("row_key") or "")
        for row in rows
        if _manual_row_references_period(row, period_id) and str(row.get("row_key") or "")
    ]
    for batch in chunked(row_keys, 200):
        client.table("platform_manual_rows").delete().eq("project_id", project_id).in_("row_key", batch).execute()
    return len(row_keys)


def _api_error_message(exc: Exception) -> str:
    """Return a compact readable message from a Supabase/PostgREST exception."""
    try:
        raw = getattr(exc, "args", None)
        if raw:
            return str(raw[0])[:1200]
    except Exception:
        pass
    return str(exc)[:1200]


def _fetch_table_row_keys_for_period(client: Client, project_id: str, period_id: str) -> list[dict[str, str]]:
    """Fetch primary-key parts for generated rows of one upload/period."""
    rows: list[dict[str, str]] = []
    start = 0
    while True:
        response = (
            client.table("platform_table_rows")
            .select("table_name,row_id")
            .eq("project_id", project_id)
            .eq("period_id", period_id)
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        data = response.data or []
        for row in data:
            table_name = str(row.get("table_name") or "").strip()
            row_id = str(row.get("row_id") or "").strip()
            if table_name and row_id:
                rows.append({"table_name": table_name, "row_id": row_id})
        if len(data) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def delete_table_rows_for_period(project_id: str, period_id: str, *, batch_size: int = 150) -> int:
    """Delete generated table rows for a period in small primary-key batches."""
    client = get_supabase_client()
    keys = _fetch_table_row_keys_for_period(client, project_id, period_id)
    if not keys:
        return 0

    deleted = 0
    by_table: dict[str, list[str]] = {}
    for row in keys:
        by_table.setdefault(row["table_name"], []).append(row["row_id"])

    for table_name, row_ids in by_table.items():
        unique_row_ids = list(dict.fromkeys(row_ids))
        for batch in chunked(unique_row_ids, batch_size):
            (
                client.table("platform_table_rows")
                .delete()
                .eq("project_id", project_id)
                .eq("period_id", period_id)
                .eq("table_name", table_name)
                .in_("row_id", batch)
                .execute()
            )
            deleted += len(batch)
    return deleted


def delete_period(
    project_id: str,
    period_id: str,
    *,
    hard: bool = False,
    delete_storage: bool = True,
    cleanup_manual: bool = True,
) -> dict[str, Any]:
    """Hide or permanently delete a project upload/period.

    If Supabase rejects a physical delete, the period is hidden instead of
    crashing the whole app.
    """
    client = get_supabase_client()
    if not hard:
        update_period_metadata(project_id, period_id, status="hidden")
        return {"mode": "soft", "manual_rows_deleted": 0, "table_rows_deleted": 0, "storage_deleted": False}

    period = get_period(project_id, period_id) or {}
    manifest = period.get("manifest") if isinstance(period.get("manifest"), dict) else {}
    storage_path = str((manifest or {}).get("storage_path") or "").strip()

    manual_deleted = 0
    table_rows_deleted = 0
    warnings: list[str] = []

    try:
        manual_deleted = delete_manual_rows_for_period(project_id, period_id) if cleanup_manual else 0
    except Exception as exc:
        warnings.append(f"Ручные правки не удалось очистить автоматически: {_api_error_message(exc)}")

    try:
        table_rows_deleted = delete_table_rows_for_period(project_id, period_id)
        client.table("platform_periods").delete().eq("project_id", project_id).eq("period_id", period_id).execute()
        mode = "hard"
    except Exception as exc:
        try:
            update_period_metadata(project_id, period_id, status="hidden")
        except Exception:
            pass
        return {
            "mode": "soft_fallback",
            "manual_rows_deleted": manual_deleted,
            "table_rows_deleted": table_rows_deleted,
            "storage_deleted": False,
            "storage_path": storage_path,
            "warnings": warnings + [
                "Supabase не разрешил физически удалить строки выгрузки; период скрыт из интерфейса. "
                f"Детали: {_api_error_message(exc)}"
            ],
        }

    storage_deleted = delete_uploaded_file_from_storage(storage_path) if delete_storage and storage_path else False
    return {
        "mode": mode,
        "manual_rows_deleted": manual_deleted,
        "table_rows_deleted": table_rows_deleted,
        "storage_deleted": storage_deleted,
        "storage_path": storage_path,
        "warnings": warnings,
    }



def list_manual(project_id: str, table_name: str | None = None) -> pd.DataFrame:
    filters: dict[str, Any] = {"project_id": project_id}
    if table_name:
        filters["table_name"] = table_name
    rows = _fetch_all(get_supabase_client(), "platform_manual_rows", filters=filters, order="updated_at")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if "updated_at" in df.columns:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    return df


def save_manual(project_id: str, table_name: str, row_key: str, payload: dict[str, Any]) -> None:
    get_supabase_client().table("platform_manual_rows").upsert({
        "project_id": project_id,
        "table_name": table_name,
        "row_key": row_key,
        "payload": payload,
        "updated_at": now_iso(),
    }, on_conflict="project_id,row_key").execute()


def get_manual(project_id: str, row_key: str) -> dict[str, Any] | None:
    data = get_supabase_client().table("platform_manual_rows").select("payload").eq("project_id", project_id).eq("row_key", row_key).limit(1).execute().data or []
    if not data:
        return None
    payload = data[0].get("payload") or {}
    return payload if isinstance(payload, dict) else None


def delete_manual(project_id: str, row_key: str) -> None:
    get_supabase_client().table("platform_manual_rows").delete().eq("project_id", project_id).eq("row_key", row_key).execute()


def safe_storage_filename(filename: str) -> str:
    source = Path(filename or "upload.csv")
    stem = ascii_storage_component(source.stem, "upload")[:120]
    suffix = ascii_storage_component(source.suffix.lower().lstrip("."), "csv")
    if suffix not in {"csv", "txt", "xlsx", "xls", "xlsm"}:
        suffix = "csv"
    return f"{stem}.{suffix}"


def content_type_for_filename(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".xls":
        return "application/vnd.ms-excel"
    return "application/octet-stream"


def storage_bucket_name() -> str:
    """Return the Supabase Storage bucket used by the platform."""
    return _secret_value("SUPABASE_STORAGE_BUCKET") or _secret_value("PLATFORM_STORAGE_BUCKET") or "dashboard-csv"


def delete_uploaded_file_from_storage(storage_path: str) -> bool:
    """Best-effort removal of a raw uploaded file from Supabase Storage."""
    storage_path = str(storage_path or "").strip()
    if not storage_path:
        return False
    client = get_supabase_client()
    bucket = storage_bucket_name()
    try:
        client.storage.from_(bucket).remove([storage_path])
        return True
    except Exception:
        return False


def save_uploaded_file_to_storage(project_id: str, period_id: str, filename: str, file_bytes: bytes) -> str:
    client = get_supabase_client()
    bucket = storage_bucket_name()
    safe_project = ascii_storage_component(project_id, "project")[:80]
    safe_period = ascii_storage_component(period_id, "period")[:80]
    safe_name = safe_storage_filename(filename)
    digest = hashlib.md5(file_bytes or b"").hexdigest()[:8]
    path = f"{safe_project}/{safe_period}/{digest}_{safe_name}"
    try:
        client.storage.from_(bucket).upload(path, file_bytes, {"content-type": content_type_for_filename(filename), "upsert": "true"})
    except TypeError:
        client.storage.from_(bucket).upload(path, file_bytes)
    return path
