from __future__ import annotations

import threading
from typing import Any

import pandas as pd

try:  # Streamlit is optional for non-UI tests.
    import streamlit as st
except Exception:  # pragma: no cover
    st = None  # type: ignore

import platform_store as store
from services.perf import perf_block

# Pure re-exports that do not need Streamlit caching.
ManualEditConflict = store.ManualEditConflict
UNCHECKED_VERSION = store.UNCHECKED_VERSION
supabase_configured = store.supabase_configured
make_period_id = store.make_period_id
save_uploaded_file_to_storage = store.save_uploaded_file_to_storage
save_report_logo_to_storage = store.save_report_logo_to_storage
download_storage_file = store.download_storage_file
delete_storage_file = store.delete_storage_file
storage_public_url = store.storage_public_url


# Счётчики версий раньше жили в st.session_state — отдельно у каждой вкладки
# и у каждого пользователя. А сам кеш @st.cache_data общий на весь процесс:
# ключ записи — это (аргументы, номер версии), и версия у разных сессий росла
# независимо от 0. Из-за этого: 1) вкладка, открытая заново (или клиентская),
# после чужой правки до TTL читала старый снимок, потому что у неё самой
# версия ещё не выросла; 2) два аналитика, начав с одного и того же 0, после
# по одной правке каждый получали ОДИНАКОВЫЙ номер версии — и то, кто сохранил
# вторым, читал из кеша результат первого, а не свою же правку. Счётчики
# сделаны общими на процесс (как и сам кеш), поэтому правка в одной вкладке
# сразу видна во всех остальных вкладках этого же процесса.
_VERSIONS_LOCK = threading.Lock()
_PROCESS_VERSIONS: dict[str, int] = {}


def _version_key(project_id: str | None, namespace: str) -> str:
    project = str(project_id or "__global__")
    return f"{namespace}::{project}"


def cache_version(project_id: str | None = None, namespace: str = "data") -> int:
    with _VERSIONS_LOCK:
        return int(_PROCESS_VERSIONS.get(_version_key(project_id, namespace), 0) or 0)


def bump_cache(
    project_id: str | None = None,
    *,
    namespaces: tuple[str, ...] = ("data", "manual", "periods"),
) -> None:
    with _VERSIONS_LOCK:
        for namespace in namespaces:
            key = _version_key(project_id, namespace)
            _PROCESS_VERSIONS[key] = int(_PROCESS_VERSIONS.get(key, 0) or 0) + 1
        # Project list/access may depend on project metadata and codes.
        if project_id is None or "projects" in namespaces:
            key = _version_key("__global__", "projects")
            _PROCESS_VERSIONS[key] = int(_PROCESS_VERSIONS.get(key, 0) or 0) + 1


def clear_platform_caches(project_id: str | None = None) -> None:
    """Invalidate platform caches by version bumping instead of global cache clear.

    This avoids dropping cached data for unrelated projects. Old cache entries expire by TTL.
    """
    bump_cache(project_id, namespaces=("data", "manual", "periods", "projects"))


# Кеши ограничены по числу записей: приложение живёт в контейнере примерно на
# гигабайт памяти, а одна выгрузка за период — это десятки мегабайт таблиц.
# Без ограничения старые наборы периодов копились в памяти до перезапуска.
if st is not None:

    @st.cache_data(ttl=120, show_spinner=False, max_entries=4)
    def _cached_list_projects(include_inactive: bool, version: int) -> pd.DataFrame:
        with perf_block("store.list_projects", include_inactive=include_inactive):
            return store.list_projects(include_inactive=include_inactive)

    @st.cache_data(ttl=120, show_spinner=False, max_entries=8)
    def _cached_list_periods(
        project_id: str, include_inactive: bool, version: int
    ) -> pd.DataFrame:
        with perf_block(
            "store.list_periods",
            project_id=project_id,
            include_inactive=include_inactive,
        ):
            return store.list_periods(project_id, include_inactive=include_inactive)

    @st.cache_data(ttl=600, show_spinner=False, max_entries=3)
    def _cached_load_generated_tables(
        project_id: str, period_ids_tuple: tuple[str, ...], version: int
    ):
        with perf_block(
            "store.load_generated_tables",
            project_id=project_id,
            periods=len(period_ids_tuple),
        ):
            return store.load_generated_tables(project_id, list(period_ids_tuple))

    @st.cache_data(ttl=900, show_spinner=False, max_entries=6)
    def _cached_load_table(
        project_id: str, period_ids_tuple: tuple[str, ...], table_name: str, version: int
    ) -> pd.DataFrame:
        with perf_block(
            "store.load_table", project_id=project_id, table=table_name
        ):
            return store.load_table(project_id, list(period_ids_tuple), table_name)

    @st.cache_data(ttl=120, show_spinner=False, max_entries=8)
    def _cached_list_manual(
        project_id: str, table_name: str | None, version: int
    ) -> pd.DataFrame:
        with perf_block(
            "store.list_manual", project_id=project_id, table_name=table_name or ""
        ):
            return store.list_manual(project_id, table_name=table_name)

    # Логотип проекта рисуется в шапке на каждой перерисовке страницы, а лежит
    # в Storage. Без кеша это сетевой запрос на каждое нажатие кнопки. Срок
    # длинный: логотип меняют раз в жизни проекта, а смена сбрасывает кеш
    # версией настроек.
    @st.cache_data(ttl=3600, show_spinner=False, max_entries=8)
    def _cached_storage_file(storage_path: str, version: int) -> bytes:
        with perf_block("store.download_storage_file"):
            return store.download_storage_file(storage_path)

    @st.cache_data(ttl=120, show_spinner=False, max_entries=16)
    def _cached_get_manual(
        project_id: str, row_key: str, version: int
    ) -> dict[str, Any] | None:
        with perf_block("store.get_manual", project_id=project_id):
            return store.get_manual(project_id, row_key)

    # Доступы по email сверяются на каждой перерисовке: снятый владельцем
    # доступ должен закрыть проект и в уже открытой вкладке. Срок короче,
    # чем у списка проектов, — это граница прав, а не справочник.
    @st.cache_data(ttl=60, show_spinner=False, max_entries=256)
    def _cached_list_memberships(email: str, version: int) -> list[dict[str, Any]]:
        with perf_block("store.list_memberships"):
            return store.list_memberships(email)

    @st.cache_data(ttl=60, show_spinner=False, max_entries=16)
    def _cached_list_project_members(project_id: str, version: int) -> pd.DataFrame:
        with perf_block("store.list_project_members", project_id=project_id):
            return store.list_project_members(project_id)

else:  # pragma: no cover

    def _cached_load_table(
        project_id: str, period_ids_tuple: tuple[str, ...], table_name: str, version: int
    ) -> pd.DataFrame:
        return store.load_table(project_id, list(period_ids_tuple), table_name)

    def _cached_list_projects(include_inactive: bool, version: int) -> pd.DataFrame:
        return store.list_projects(include_inactive=include_inactive)

    def _cached_list_periods(
        project_id: str, include_inactive: bool, version: int
    ) -> pd.DataFrame:
        return store.list_periods(project_id, include_inactive=include_inactive)

    def _cached_load_generated_tables(
        project_id: str, period_ids_tuple: tuple[str, ...], version: int
    ):
        return store.load_generated_tables(project_id, list(period_ids_tuple))

    def _cached_list_manual(
        project_id: str, table_name: str | None, version: int
    ) -> pd.DataFrame:
        return store.list_manual(project_id, table_name=table_name)

    def _cached_get_manual(
        project_id: str, row_key: str, version: int
    ) -> dict[str, Any] | None:
        return store.get_manual(project_id, row_key)

    def _cached_list_memberships(email: str, version: int) -> list[dict[str, Any]]:
        return store.list_memberships(email)

    def _cached_list_project_members(project_id: str, version: int) -> pd.DataFrame:
        return store.list_project_members(project_id)


def load_table(project_id: str, period_ids: list[str], table_name: str) -> pd.DataFrame:
    """Загрузить одну таблицу периода.

    Нужна там, где не требуется вся подготовка дашборда: например, чтобы
    посчитать метрики прошлого периода для дельт в шапке.
    """
    key = tuple(sorted(str(pid) for pid in (period_ids or []) if str(pid).strip()))
    if not key:
        return pd.DataFrame()
    return _cached_load_table(
        str(project_id), key, str(table_name), cache_version(project_id, "data")
    )


def load_storage_file(storage_path: str, project_id: str | None = None) -> bytes:
    """Файл из Storage с кешем — для логотипа в шапке проекта.

    Без кеша шапка ходила бы в сеть при каждой перерисовке страницы, то есть
    на каждое нажатие кнопки в любом разделе.
    """
    path = str(storage_path or "").strip()
    if not path:
        return b""
    if st is None:
        return store.download_storage_file(path)
    try:
        return _cached_storage_file(path, cache_version(project_id, "settings"))
    except Exception:  # noqa: BLE001 — картинка не стоит падения страницы
        return b""


def list_projects(include_inactive: bool = False) -> pd.DataFrame:
    return _cached_list_projects(
        include_inactive, cache_version("__global__", "projects")
    )


def list_periods(project_id: str, include_inactive: bool = False) -> pd.DataFrame:
    return _cached_list_periods(
        project_id, include_inactive, cache_version(project_id, "periods")
    )


def load_generated_tables(project_id: str, period_ids: list[str]):
    period_ids_tuple = tuple(str(x) for x in (period_ids or []) if str(x).strip())
    return _cached_load_generated_tables(
        project_id, period_ids_tuple, cache_version(project_id, "data")
    )


def list_manual(project_id: str, table_name: str | None = None) -> pd.DataFrame:
    return _cached_list_manual(
        project_id, table_name, cache_version(project_id, "manual")
    )


def get_manual(project_id: str, row_key: str) -> dict[str, Any] | None:
    return _cached_get_manual(project_id, row_key, cache_version(project_id, "manual"))


def _bump_members() -> None:
    bump_cache("__global__", namespaces=("members",))


def list_memberships(email: str) -> list[dict[str, Any]]:
    email = store.normalize_email(email)
    if not email:
        return []
    return _cached_list_memberships(email, cache_version("__global__", "members"))


def list_project_members(project_id: str) -> pd.DataFrame:
    return _cached_list_project_members(
        str(project_id), cache_version("__global__", "members")
    )


def save_project_member(project_id: str, email: str, role: str) -> str:
    with perf_block("store.save_project_member", project_id=project_id):
        saved = store.save_project_member(project_id, email, role)
    _bump_members()
    return saved


def remove_project_member(project_id: str, email: str) -> None:
    with perf_block("store.remove_project_member", project_id=project_id):
        store.remove_project_member(project_id, email)
    _bump_members()


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
    with perf_block(
        "store.update_period_metadata", project_id=project_id, period_id=period_id
    ):
        store.update_period_metadata(project_id, period_id, **kwargs)
    clear_platform_caches(project_id)


def delete_period(project_id: str, period_id: str, **kwargs):
    with perf_block("store.delete_period", project_id=project_id, period_id=period_id):
        result = store.delete_period(project_id, period_id, **kwargs)
    clear_platform_caches(project_id)
    return result


def delete_project(project_id: str, **kwargs):
    with perf_block("store.delete_project", project_id=project_id):
        result = store.delete_project(project_id, **kwargs)
    clear_platform_caches(project_id)
    clear_platform_caches(None)
    # Вместе с проектом удаляются и его доступы по email.
    _bump_members()
    return result


def save_manual(
    project_id: str,
    table_name: str,
    row_key: str,
    payload: dict[str, Any],
    *,
    expected_updated_at: Any = store.UNCHECKED_VERSION,
) -> str:
    # При конфликте версий store бросает ManualEditConflict до записи: база
    # не менялась, поэтому и кеш не сбрасывается — bump не выполняется.
    with perf_block("store.save_manual", project_id=project_id, table_name=table_name):
        written = store.save_manual(
            project_id,
            table_name,
            row_key,
            payload,
            expected_updated_at=expected_updated_at,
        )
    bump_cache(project_id, namespaces=("manual", "data"))
    return written


def get_manual_version(
    project_id: str, row_key: str, table_name: str | None = None
):
    """updated_at строки правки — версия для условного сохранения.

    Читает через кешированный list_manual, то есть отдаёт версию из того же
    снимка, который видит страница. None — записи нет.
    """
    df = list_manual(project_id, table_name)
    if df is None or df.empty or "row_key" not in df.columns:
        return None
    rows = df[df["row_key"].astype(str) == str(row_key)]
    if rows.empty or "updated_at" not in rows.columns:
        return None
    value = rows.iloc[-1]["updated_at"]
    return None if pd.isna(value) else value


def delete_manual(project_id: str, row_key: str) -> None:
    with perf_block("store.delete_manual", project_id=project_id):
        store.delete_manual(project_id, row_key)
    bump_cache(project_id, namespaces=("manual", "data"))
