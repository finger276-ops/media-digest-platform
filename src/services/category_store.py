# -*- coding: utf-8 -*-
"""Хранение категорийных бенчмарков (данных по конкурентам).

Таблица `platform_category_benchmarks` описана в
`sql/platform_brand_metrics_schema.sql`: одна строка на период проекта,
разбивка по брендам целиком в jsonb.

Сами сообщения конкурентов не сохраняются — только агрегаты, которые нужны
для SOV и ReachScore. Так категорийная выгрузка на сотни тысяч строк
превращается в десяток чисел и не раздувает базу проекта.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from platform_store import get_supabase_client, now_iso
from services.metrics_compute import (
    audience_by_group,
    audience_place_key,
    numeric_series,
)
from services.brand_metrics import (
    AUDIENCE_COLUMNS,
    ENGAGEMENT_COLUMNS,
    REACH_COLUMNS,
)

TABLE = "platform_category_benchmarks"

BRAND_MODE_COLUMNS = "columns"  # каждый бренд — отдельная колонка выгрузки
BRAND_MODE_VALUES = "values"    # бренд записан значением в одной колонке


# ---------------------------------------------------------------------------
# Разбор категорийной выгрузки
# ---------------------------------------------------------------------------


def candidate_brand_columns(table: pd.DataFrame) -> list[str]:
    """Колонки, в которых может лежать бренд.

    Для Brand Analytics это в первую очередь теговые колонки после «Обработано»
    (в категорийном мониторинге каждый бренд обычно ведётся отдельной колонкой),
    затем — тематические колонки выгрузки.
    """
    if table is None or table.empty:
        return []

    columns: list[str] = []
    declared = str(table.get("source_tag_columns", pd.Series(dtype=str)).iloc[0]) if (
        "source_tag_columns" in table.columns and len(table) > 0
    ) else ""
    for name in declared.split("|"):
        name = name.strip()
        if name and name in table.columns and name not in columns:
            columns.append(name)

    for name in ["Сюжет", "Основная тема", "Категории", "Теги", "Все темы"]:
        if name in table.columns and name not in columns:
            values = table[name].fillna("").astype(str).str.strip()
            if values.ne("").any():
                columns.append(name)
    return columns


def aggregate_by_brand_columns(
    table: pd.DataFrame, brand_columns: list[str]
) -> pd.DataFrame:
    """Агрегаты по брендам, когда каждый бренд — отдельная колонка выгрузки.

    Сообщение относится к бренду, если ячейка соответствующей колонки заполнена.
    Одно сообщение может относиться сразу к нескольким брендам — так устроен
    категорийный мониторинг, и это нормально.
    """
    if table is None or table.empty or not brand_columns:
        return pd.DataFrame()

    audience = numeric_series(table, AUDIENCE_COLUMNS)
    reach = numeric_series(table, REACH_COLUMNS)
    engagement = numeric_series(table, ENGAGEMENT_COLUMNS)
    # Площадка должна попасть в бренд один раз, сколько бы сообщений о нём
    # ни опубликовала: у поста и комментариев под ним одна аудитория.
    place = (
        table["_audience_place"]
        if "_audience_place" in table.columns
        else audience_place_key(table)
    )

    rows = []
    for column in brand_columns:
        if column not in table.columns:
            continue
        mask = table[column].fillna("").astype(str).str.strip().ne("")
        if not bool(mask.any()):
            continue
        brand_audience = (
            pd.DataFrame({"_a": audience[mask].values, "_k": list(place[mask])})
            .groupby("_k")["_a"]
            .max()
            .sum()
        )
        rows.append(
            {
                "brand": str(column),
                "messages": int(mask.sum()),
                "audience": int(brand_audience),
                "reach": int(reach[mask].sum()),
                "engagement": int(engagement[mask].sum()),
                "is_own": False,
            }
        )
    return pd.DataFrame(rows).sort_values("messages", ascending=False) if rows else pd.DataFrame()


def aggregate_by_brand_values(
    table: pd.DataFrame, brand_column: str, *, top_n: int = 40
) -> pd.DataFrame:
    """Агрегаты по брендам, когда название бренда записано значением колонки."""
    if table is None or table.empty or brand_column not in table.columns:
        return pd.DataFrame()

    work = table.copy()
    work["_brand"] = work[brand_column].fillna("").astype(str).str.strip()
    work = work[work["_brand"].ne("")]
    if work.empty:
        return pd.DataFrame()

    work["_audience"] = numeric_series(work, AUDIENCE_COLUMNS).values
    work["_reach"] = numeric_series(work, REACH_COLUMNS).values
    work["_engagement"] = numeric_series(work, ENGAGEMENT_COLUMNS).values

    grouped = (
        work.groupby("_brand")
        .agg(
            messages=("_brand", "size"),
            reach=("_reach", "sum"),
            engagement=("_engagement", "sum"),
        )
        .reset_index()
        .rename(columns={"_brand": "brand"})
        .sort_values("messages", ascending=False)
        .head(int(top_n))
    )
    # Аудитория — единственная метрика, которую нельзя складывать по строкам.
    grouped["audience"] = (
        grouped["brand"].map(audience_by_group(work, work["_brand"])).fillna(0)
    )
    for col in ["messages", "audience", "reach", "engagement"]:
        grouped[col] = grouped[col].astype(int)
    grouped["is_own"] = False
    return grouped


def mark_own_brand(brands: pd.DataFrame, own_brand: str) -> pd.DataFrame:
    if brands is None or brands.empty:
        return brands
    work = brands.copy()
    work["is_own"] = work["brand"].astype(str) == str(own_brand)
    return work


def brand_counts_from_tags(messages: pd.DataFrame) -> pd.Series:
    """Сколько сообщений у каждого тега периода.

    Нужно, чтобы аналитик видел, из чего выбирает: в теговых колонках лежат и
    бренды, и аналитические разрезы («PR», «Монтаж», «Статья»), и отличить их
    по названию машина не может — это знание о рынке.
    """
    if messages is None or messages.empty or "tags" not in messages.columns:
        return pd.Series(dtype=int)
    exploded = (
        messages["tags"].fillna("").astype(str).str.split("|").explode().str.strip()
    )
    exploded = exploded[exploded != ""]
    if exploded.empty:
        return pd.Series(dtype=int)
    return exploded.value_counts()


def brands_from_messages(
    messages: pd.DataFrame,
    own_brands: list[str],
    competitor_brands: list[str],
) -> pd.DataFrame:
    """Собрать категорийные агрегаты прямо из выгрузки проекта.

    Отдельная выгрузка по категории нужна не всегда. В категорийном мониторинге
    конкуренты уже размечены тегами в той же выгрузке: у RUFLEX это Docke,
    Tegola, Roofshield, Faracs, у Кнауфа — свои. Требовать вторую загрузку тех
    же данных значит просить работу, которая уже сделана.

    Своих брендов может быть несколько: головной, дочерние, отдельные марки.
    SOV считает их вместе — доля голоса у группы компаний общая.
    """
    own = [str(x).strip() for x in (own_brands or []) if str(x).strip()]
    competitors = [str(x).strip() for x in (competitor_brands or []) if str(x).strip()]
    names = list(dict.fromkeys(own + competitors))
    if messages is None or messages.empty or not names:
        return pd.DataFrame()

    tags = messages["tags"].fillna("").astype(str) if "tags" in messages.columns else None
    if tags is None:
        return pd.DataFrame()
    tag_sets = tags.map(lambda value: {t.strip() for t in value.split("|") if t.strip()})

    audience = numeric_series(messages, AUDIENCE_COLUMNS)
    reach = numeric_series(messages, REACH_COLUMNS)
    engagement = numeric_series(messages, ENGAGEMENT_COLUMNS)
    place = (
        messages["_audience_place"]
        if "_audience_place" in messages.columns
        else audience_place_key(messages)
    )

    own_lower = {x.casefold() for x in own}
    rows = []
    for name in names:
        mask = tag_sets.map(lambda values, n=name: n in values)
        if not bool(mask.any()):
            continue
        # Площадка учитывается один раз на бренд: у поста и комментариев под
        # ним одна аудитория.
        brand_audience = (
            pd.DataFrame({"_a": audience[mask].values, "_k": list(place[mask])})
            .groupby("_k")["_a"]
            .max()
            .sum()
        )
        rows.append(
            {
                "brand": name,
                "messages": int(mask.sum()),
                "audience": int(brand_audience),
                "reach": int(reach[mask].sum()),
                "engagement": int(engagement[mask].sum()),
                "is_own": name.casefold() in own_lower,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("messages", ascending=False).reset_index(drop=True)


def benchmark_from_messages(
    messages: pd.DataFrame,
    own_brands: list[str],
    competitor_brands: list[str],
) -> dict[str, Any] | None:
    """Бенчмарк из выгрузки проекта — в том же виде, что из отдельной загрузки."""
    brands = brands_from_messages(messages, own_brands, competitor_brands)
    if brands is None or brands.empty:
        return None
    if not bool(brands["is_own"].any()):
        return None
    own = [str(x) for x in brands.loc[brands["is_own"], "brand"]]
    found = {str(x).casefold() for x in brands["brand"]}
    # Отмеченный конкурент, не встретившийся в периоде, выпадает из агрегатов.
    # Если выпали все, метрика должна сказать именно это, а не «конкуренты не
    # отмечены»: человек их отметил и будет искать ошибку не там.
    missing = [
        str(x).strip()
        for x in (competitor_brands or [])
        if str(x).strip() and str(x).strip().casefold() not in found
    ]
    return {
        "own_brand": own[0] if own else "",
        "own_brands": own,
        "brands": brands.to_dict("records"),
        "source": "project_tags",
        "configured_competitors": len(
            [x for x in (competitor_brands or []) if str(x).strip()]
        ),
        "missing_competitors": missing,
    }


# ---------------------------------------------------------------------------
# Чтение и запись
# ---------------------------------------------------------------------------


def save_benchmark(
    *,
    project_id: str,
    period_id: str,
    brands: pd.DataFrame,
    own_brand: str,
    brand_mode: str = BRAND_MODE_COLUMNS,
    brand_source: str = "",
    source_filename: str = "",
    messages_total: int = 0,
) -> None:
    if brands is None or brands.empty:
        raise ValueError("Нет данных по брендам для сохранения.")

    payload_brands = [
        {
            "brand": str(row.get("brand", "")),
            "messages": int(row.get("messages", 0) or 0),
            "audience": int(row.get("audience", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "engagement": int(row.get("engagement", 0) or 0),
            "is_own": bool(row.get("is_own", False)),
        }
        for _, row in brands.iterrows()
    ]

    client = get_supabase_client()
    client.table(TABLE).upsert(
        {
            "project_id": str(project_id),
            "period_id": str(period_id),
            "own_brand": str(own_brand or ""),
            "brands": payload_brands,
            "brand_mode": str(brand_mode),
            "brand_source": str(brand_source or ""),
            "source_filename": str(source_filename or ""),
            "messages_total": int(messages_total or 0),
            "updated_at": now_iso(),
        },
        on_conflict="project_id,period_id",
    ).execute()


def load_benchmark(project_id: str, period_id: str) -> dict[str, Any] | None:
    client = get_supabase_client()
    response = (
        client.table(TABLE)
        .select("*")
        .eq("project_id", str(project_id))
        .eq("period_id", str(period_id))
        .limit(1)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


def load_benchmarks(project_id: str, period_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Бенчмарки нескольких периодов одним запросом: {period_id: запись}."""
    period_ids = [str(pid) for pid in (period_ids or []) if str(pid).strip()]
    if not period_ids:
        return {}
    client = get_supabase_client()
    response = (
        client.table(TABLE)
        .select("*")
        .eq("project_id", str(project_id))
        .in_("period_id", period_ids)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    return {str(row.get("period_id")): row for row in rows}


def merged_benchmark(benchmarks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Сложить бенчмарки нескольких периодов в один — для суммарных метрик."""
    if not benchmarks:
        return None
    frames = []
    own_brand = ""
    for record in benchmarks.values():
        own_brand = own_brand or str(record.get("own_brand") or "")
        brands = record.get("brands") or []
        if brands:
            frames.append(pd.DataFrame(brands))
    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True)
    for col in ["messages", "audience", "reach", "engagement"]:
        if col not in combined.columns:
            combined[col] = 0
        combined[col] = pd.to_numeric(combined[col], errors="coerce").fillna(0).astype(int)
    grouped = (
        combined.groupby("brand", as_index=False)[
            ["messages", "audience", "reach", "engagement"]
        ]
        .sum()
    )
    grouped["is_own"] = grouped["brand"].astype(str) == own_brand
    return {
        "own_brand": own_brand,
        "brands": grouped.to_dict("records"),
        "periods": len(benchmarks),
    }


def delete_benchmark(project_id: str, period_id: str) -> None:
    client = get_supabase_client()
    client.table(TABLE).delete().eq("project_id", str(project_id)).eq(
        "period_id", str(period_id)
    ).execute()
