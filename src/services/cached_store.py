from __future__ import annotations

from typing import Any

import pandas as pd

try:  # Streamlit is optional for non-UI tests.
    import streamlit as st
except Exception:  # pragma: no cover
    st = None  # type: ignore

import platform_store as store
from services.perf import perf_block

# Pure re-exports that do not need Streamlit caching.
supabase_configured = store.supabase_configured
make_period_id = store.make_period_id
save_uploaded_file_to_storage = store.save_uploaded_file_to_storage


def _session_versions() -> dict[str, int]:
    if st is None:
        if not hasattr(_session_versions, "_fallback"):
            setattr(_session_versions, "_fallback", {})
        return getattr(_session_versions, "_fallback")
    try:
        if "_platform_cache_versions" not in st.session_state:
            st.session_state["_platform_cache_versions"] = {}
        return st.session_state["_platform_cache_versions"]
    except Exception:
        if not hasattr(_session_versions, "_fallback"):
            setattr(_session_versions, "_fallback", {})
        return getattr(_session_versions, "_fallback")


def _version_key(project_id: str | None, namespace: str) -> str:
    project = str(project_id or "__global__")
    return f"{namespace}::{project}"


def cache_version(project_id: str | None = None, namespace: str = "data") -> int:
    versions = _session_versions()
    return int(versions.get(_version_key(project_id, namespace), 0) or 0)


def bump_cache(project_id: str | None = None, *, namespaces: tuple[str, ...] = ("data", "manual", "periods")) -> None:
    versions = _session_versions()
    for namespace in namespaces:
        key = _version_key(project_id, namespace)
        versions[key] = int(versions.get(key, 0) or 0) + 1
    # Project list/access may depend on project metadata and codes.
    if project_id is None or "projects" in namespaces:
        key = _version_key("__global__", "projects")
        versions[key] = int(versions.get(key, 0) or 0) + 1


def clear_platform_caches(project_id: str | None = None) -> None:
    """Invalidate platform caches by version bumping instead of global cache clear.

    This avoids dropping cached data for unrelated projects. Old cache entries expire by TTL.
    """
    bump_cache(project_id, namespaces=("data", "manual", "periods", "projects"))


if st is not None:
    @st.cache_data(ttl=120, show_spinner=False)
    def _cached_list_projects(include_inactive: bool, version: int) -> pd.DataFrame:
        with perf_block("store.list_projects", include_inactive=include_inactive):
            return store.list_projects(include_inactive=include_inactive)

    @st.cache_data(ttl=120, show_spinner=False)
    def _cached_list_periods(project_id: str, include_inactive: bool, version: int) -> pd.DataFrame:
        with perf_block("store.list_periods", project_id=project_id, include_inactive=include_inactive):
            return store.list_periods(project_id, include_inactive=include_inactive)

    @st.cache_data(ttl=300, show_spinner=False)
    def _cached_load_generated_tables(project_id: str, period_ids_tuple: tuple[str, ...], version: int):
        with perf_block("store.load_generated_tables", project_id=project_id, periods=len(period_ids_tuple)):
            return store.load_generated_tables(project_id, list(period_ids_tuple))

    @st.cache_data(ttl=120, show_spinner=False)
    def _cached_list_manual(project_id: str, table_name: str | None, version: int) -> pd.DataFrame:
        with perf_block("store.list_manual", project_id=project_id, table_name=table_name or ""):
            return store.list_manual(project_id, table_name=table_name)

    @st.cache_data(ttl=120, show_spinner=False)
    def _cached_get_manual(project_id: str, row_key: str, version: int) -> dict[str, Any] | None:
        with perf_block("store.get_manual", project_id=project_id):
            return store.get_manual(project_id, row_key)
else:  # pragma: no cover
    def _cached_list_projects(include_inactive: bool, version: int) -> pd.DataFrame:
        return store.list_projects(include_inactive=include_inactive)

    def _cached_list_periods(project_id: str, include_inactive: bool, version: int) -> pd.DataFrame:
        return store.list_periods(project_id, include_inactive=include_inactive)

    def _cached_load_generated_tables(project_id: str, period_ids_tuple: tuple[str, ...], version: int):
        return store.load_generated_tables(project_id, list(period_ids_tuple))

    def _cached_list_manual(project_id: str, table_name: str | None, version: int) -> pd.DataFrame:
        return store.list_manual(project_id, table_name=table_name)

    def _cached_get_manual(project_id: str, row_key: str, version: int) -> dict[str, Any] | None:
        return store.get_manual(project_id, row_key)


def list_projects(include_inactive: bool = False) -> pd.DataFrame:
    return _cached_list_projects(include_inactive, cache_version("__global__", "projects"))


def list_periods(project_id: str, include_inactive: bool = False) -> pd.DataFrame:
    return _cached_list_periods(project_id, include_inactive, cache_version(project_id, "periods"))


def load_generated_tables(project_id: str, period_ids: list[str]):
    period_ids_tuple = tuple(str(x) for x in (period_ids or []) if str(x).strip())
    return _cached_load_generated_tables(project_id, period_ids_tuple, cache_version(project_id, "data"))


def list_manual(project_id: str, table_name: str | None = None) -> pd.DataFrame:
    return _cached_list_manual(project_id, table_name, cache_version(project_id, "manual"))


def get_manual(project_id: str, row_key: str) -> dict[str, Any] | None:
    return _cached_get_manual(project_id, row_key, cache_version(project_id, "manual"))


def resolve_project_access(access_code: str) -> tuple[str | None, str]:
    # Reuse cached project list indirectly through store's logic is hard, so keep raw call.
    # Access checks are small and only happen at entry.
    with perf_block("store.resolve_project_access"):
        return store.resolve_project_access(access_code)


def create_project(*args, **kwargs):
    with perf_block("store.create_project"):
        project_id = store.create_project(*args, **kwargs)
    clear_platform_caches(project_id)
    clear_platform_caches(None)
    return project_id


def update_project(project_id: str, **kwargs) -> None:
    with perf_block("store.update_project", project_id=project_id):
        store.update_project(project_id, **kwargs)
    clear_platform_caches(project_id)
    clear_platform_caches(None)


def save_processed_tables(*, project_id: str, **kwargs) -> None:
    with perf_block("store.save_processed_tables", project_id=project_id):
        store.save_processed_tables(project_id=project_id, **kwargs)
    clear_platform_caches(project_id)


def update_period_metadata(project_id: str, period_id: str, **kwargs) -> None:
    with perf_block("store.update_period_metadata", project_id=project_id, period_id=period_id):
        store.update_period_metadata(project_id, period_id, **kwargs)
    clear_platform_caches(project_id)


def delete_period(project_id: str, period_id: str, **kwargs):
    with perf_block("store.delete_period", project_id=project_id, period_id=period_id):
        result = store.delete_period(project_id, period_id, **kwargs)
    clear_platform_caches(project_id)
    return result


def save_manual(project_id: str, table_name: str, row_key: str, payload: dict[str, Any]) -> None:
    with perf_block("store.save_manual", project_id=project_id, table_name=table_name):
        store.save_manual(project_id, table_name, row_key, payload)
    bump_cache(project_id, namespaces=("manual", "data"))


def delete_manual(project_id: str, row_key: str) -> None:
    with perf_block("store.delete_manual", project_id=project_id):
        store.delete_manual(project_id, row_key)
    bump_cache(project_id, namespaces=("manual", "data"))
