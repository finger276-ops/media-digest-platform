# -*- coding: utf-8 -*-
"""Разделы дашборда, которые перерисовываются фрагментом: «Теги»,
«Сообщения», «Источники», «Инфоповоды», «Индексы бренда».

Вынесено из app.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from brand_metrics_ui import render_brand_metrics_page
from events_ui import render_events
from messages_ui import render_messages_block
from section_boundary_ui import _as_fragment
from services.dashboard_data import cached_period_messages
from sidebar_ui import render_small_events_notice
from sources_ui import render_sources_page
from tag_slice_ui import sliced_loader
from tag_tier_analytics_ui import render_tier_analytics_block
from tags_ui import render_tag_statistics


@_as_fragment
def _section_tags(
    messages: pd.DataFrame, project_id: str, analyst_view: bool = False
) -> None:
    render_tag_statistics(messages, project_id=project_id, analyst_view=analyst_view)
    render_tier_analytics_block(messages, project_id, analyst_view=analyst_view)


@_as_fragment
def _section_messages(
    messages: pd.DataFrame,
    project_id: str,
    project_name: str = "",
    period_label: str = "",
    tag_slice: list[str] | None = None,
) -> None:
    render_messages_block(
        messages,
        project_id=project_id,
        project_name=project_name,
        period_label=period_label,
        slice_tags=tag_slice,
    )


@_as_fragment
def _section_sources(
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    project_id: str,
    tag_slice: list[str] | None = None,
) -> None:
    render_sources_page(
        messages,
        periods,
        period_ids,
        load_period_messages=sliced_loader(
            lambda period_id: cached_period_messages(project_id, [period_id]),
            list(tag_slice or []),
        ),
        project_id=project_id,
    )


@_as_fragment
def _section_events(
    project_id: str,
    role: str,
    events_agg: pd.DataFrame,
    messages: pd.DataFrame,
    manual_state: dict[str, Any],
    hidden_events: int,
    hidden_messages: int,
    min_event_messages: int,
    read_only: bool = False,
) -> None:
    render_small_events_notice(hidden_events, hidden_messages, min_event_messages)
    render_events(
        project_id, role, events_agg, messages, manual_state, read_only=read_only
    )


@_as_fragment
def _section_brand_metrics(
    project_id: str,
    project_settings: dict[str, Any],
    messages: pd.DataFrame,
    periods: pd.DataFrame,
    period_ids: list[str],
    role_can_edit: bool,
    read_only: bool = False,
    partial_period: bool = False,
) -> None:
    render_brand_metrics_page(
        project_id,
        project_settings,
        messages,
        periods,
        period_ids,
        role_can_edit=role_can_edit,
        read_only=read_only,
        partial_period=partial_period,
    )
