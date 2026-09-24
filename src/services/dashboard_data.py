"""Подготовка сообщений периодов к расчёту метрик.

Дашборд показывает сообщения не такими, как они лежат в базе: к ним
привязываются инфоповоды, применяются ручные правки аналитика (скрытые
сообщения, переносы между темами, склейки тем), вычищаются служебные теги и
добавляются колонки для расчётов. Любой расчёт, который берёт периоды сверх
выбранных — динамика по всем периодам, изменение к прошлому периоду, — должен
проходить тот же путь. Иначе одинаковые по данным периоды дают разные числа:
NSS не видит инфоповодов, а скрытое аналитиком сообщение продолжает считаться.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from services.cached_store import load_generated_tables
from services.event_enrichment import enrich_messages
from services.manual_moderation import apply_manual_overrides
from services.metrics_compute import prepare_dashboard_messages
from services.tag_compute import clean_brand_analytics_tags


def prepare_period_data(
    project_id: str, period_ids: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Инфоповоды, подготовленные сообщения и состояние ручных правок."""
    events, _discussions, messages, discussion_messages, event_discussions = (
        load_generated_tables(project_id, period_ids)
    )
    enriched = enrich_messages(messages, event_discussions, discussion_messages, events)
    events, enriched, manual_state = apply_manual_overrides(project_id, events, enriched)
    # Brand Analytics: в блоке тегов остаются только системные колонки после
    # «Обработано», без legacy-меток старых алгоритмов.
    enriched = clean_brand_analytics_tags(enriched)
    enriched = prepare_dashboard_messages(enriched)
    return events, enriched, manual_state


def prepare_period_messages(project_id: str, period_ids: list[str]) -> pd.DataFrame:
    """Только сообщения — для расчётов по периодам вне выбранных."""
    _events, messages, _manual_state = prepare_period_data(project_id, period_ids)
    return messages
