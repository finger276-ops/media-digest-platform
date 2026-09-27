# -*- coding: utf-8 -*-
"""Блок «Резервные копии» на странице «Проекты» — только владельцу.

Показывает, что копии действительно делаются: когда была последняя, сколько
лежит в хранилище. Если последней больше двух суток — предупреждает: ночная
задача в GitHub Actions могла отключиться (GitHub выключает расписание в
репозитории без активности за 60 дней). Восстановление — из командной
строки (docs/BACKUPS.md): это действие владельца над всей базой, кнопке ему
не место.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from error_ui import show_error
import platform_store as store
from services.backups import list_backups, make_backup

STALE_AFTER = timedelta(days=2)


def backup_time(name: str) -> datetime | None:
    """Время копии из имени platform_ГГГГ-ММ-ДД_ЧЧММСС.zip (UTC)."""
    stem = str(name).removeprefix("platform_").removesuffix(".zip")
    try:
        return datetime.strptime(stem, "%Y-%m-%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _moscow(moment: datetime) -> str:
    return pd.Timestamp(moment).tz_convert("Europe/Moscow").strftime("%d.%m.%Y %H:%M")


def render_backups_block(*, now: datetime | None = None) -> None:
    st.subheader("Резервные копии")
    st.caption(
        "Каждую ночь делается копия всех таблиц платформы: проекты, периоды, "
        "обработанные данные, ручные правки. В хранилище лежат 14 последних, "
        "и ещё 7 дней копия хранится вне Supabase — в GitHub Actions. Исходные "
        "файлы выгрузок в копию не входят: они и так в хранилище. Восстановление "
        "— командой из docs/BACKUPS.md."
    )
    client = store.get_supabase_client()
    try:
        backups = list_backups(client)
    except Exception as exc:  # noqa: BLE001 — список копий не должен ронять страницу
        show_error("Список резервных копий сейчас недоступен.", exc, warning=True)
        backups = []

    now = now or datetime.now(timezone.utc)
    latest = backup_time(backups[0]["name"]) if backups else None
    if latest is None:
        st.warning(
            "Резервных копий пока нет. Сделайте первую кнопкой ниже и проверьте, "
            "что в GitHub включена задача «Резервная копия»."
        )
    elif now - latest > STALE_AFTER:
        st.warning(
            f"Последней копии больше двух суток ({_moscow(latest)}). Проверьте в "
            "GitHub Actions задачу «Резервная копия»: GitHub отключает расписание в "
            "репозиториях без изменений за 60 дней."
        )
    else:
        st.success(f"Последняя копия: {_moscow(latest)}, всего в хранилище: {len(backups)}.")

    if backups:
        st.dataframe(
            pd.DataFrame(
                {
                    "Копия": [_moscow(t) if (t := backup_time(item["name"])) else item["name"] for item in backups],
                    "Размер, МБ": [round(item["size"] / 1e6, 1) for item in backups],
                }
            ),
            hide_index=True,
            width="stretch",
        )

    left, right = st.columns(2)
    with left:
        if st.button("Сделать копию сейчас", key="backup_now"):
            try:
                with st.spinner("Делаю копию…"):
                    result = make_backup(client)
                st.success(
                    f"Копия сделана: {result['size'] / 1e6:.1f} МБ, строк: "
                    f"{sum(result['tables'].values())}."
                )
            except Exception as exc:  # noqa: BLE001 — сбой объясняется владельцу
                show_error("Не удалось сделать резервную копию.", exc)
    with right:
        if backups and st.button("Подготовить скачивание последней", key="backup_prepare_download"):
            try:
                st.session_state["backup_download"] = (
                    backups[0]["name"],
                    store.download_storage_file(backups[0]["path"]),
                )
            except Exception as exc:  # noqa: BLE001
                show_error("Не удалось скачать копию из хранилища.", exc)
        prepared = st.session_state.get("backup_download")
        if prepared:
            st.download_button(
                "Скачать копию",
                data=prepared[1],
                file_name=prepared[0],
                mime="application/zip",
                key="backup_download_button",
            )
