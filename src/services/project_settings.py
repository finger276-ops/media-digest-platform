# -*- coding: utf-8 -*-
"""Чтение project.settings (JSON) в структурированные, провалидированные значения.

Чистые функции без Streamlit — превращают то, что могло прийти из старых
версий настроек или быть отсутствующим, в безопасные значения по умолчанию.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .dashboard_config import (
    ALGORITHM_PROFILE_OPTIONS,
    CHART_LABEL_POSITION_OPTIONS,
    COMPARISON_CHART_BLOCKS,
    DASHBOARD_SECTION_OPTIONS,
    DEFAULT_CHART_LABEL_SETTINGS,
    DEFAULT_DASHBOARD_VIEW_SETTINGS,
    DEFAULT_REPORT_BRANDING,
    DEFAULT_REPORT_SECTIONS,
    DEFAULT_STORY_BUILD_SETTINGS,
    LEGACY_PROFILE_ALIASES,
    REPORT_SECTION_OPTIONS,
)


def project_settings_from_row(row) -> dict[str, Any]:
    settings = {}
    try:
        settings = row.get("settings") or {}
    except Exception:
        settings = {}
    return settings if isinstance(settings, dict) else {}


# Демо-проект: витрина для показа платформы. Смотреть можно почти всё, менять
# нельзя ничего. Флаг ставит владелец платформы в карточке проекта.
DEMO_MESSAGE = "Тестовый доступ, редактура недоступна"
DEMO_AI_LIMIT = 10


def is_demo_project(project_settings: dict[str, Any] | None) -> bool:
    """Демо-режим проекта. Мусор и отсутствие ключа — обычный проект.

    Включать демо должно быть явным решением: случайно включённый режим тихо
    отнимет у команды правку, и искать причину будут долго.
    """
    return bool((project_settings or {}).get("demo_mode") is True)


def demo_ai_runs_used(project_settings: dict[str, Any] | None) -> int:
    """Сколько генераций ИИ уже израсходовано в демо-проекте.

    Счётчик не сбрасывается: демо выдаётся многим, и ежемесячное обнуление
    превратило бы лимит в бесконечный. Отрицательные значения и мусор считаем
    нулём, значения выше лимита не обрезаем — лимит проверяется отдельно.
    """
    raw = (project_settings or {}).get("demo_ai_runs")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def demo_ai_runs_left(project_settings: dict[str, Any] | None) -> int:
    return max(0, DEMO_AI_LIMIT - demo_ai_runs_used(project_settings))


def merged_dashboard_view_settings(
    current_settings: dict[str, Any] | None, updates: dict[str, Any]
) -> dict[str, Any]:
    """Дополнить настройки вида, а не заменить их целиком.

    В одном словаре живут две группы ключей с разными хозяевами: карточка
    проекта пишет вид по умолчанию, стартовый раздел, набор графиков сравнения
    и «скрывать техническое», а поповер «⚙️ Вид» — порог склейки заголовков и
    набор блоков в шапке. Карточка про вторую группу не знает и, записывая
    словарь целиком, молча её стирала: владелец заходил поправить название
    проекта и сбрасывал порог склейки и «Метрики периода в шапке». По зрителю
    било сильнее всего — у него набор блоков берётся только отсюда, и
    переопределить его в интерфейсе нечем.
    """
    merged = dict(
        (current_settings or {}).get("dashboard_view_settings") or {}
    )
    merged.update(updates)
    return merged


def valid_hex_color(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if re.match(r"^#[0-9a-fA-F]{6}$", text):
        return text
    return fallback


def dashboard_view_settings_from_project_settings(
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    raw = {}
    if isinstance(settings, dict):
        raw = settings.get("dashboard_view_settings") or {}
    if not isinstance(raw, dict):
        raw = {}
    result = dict(DEFAULT_DASHBOARD_VIEW_SETTINGS)
    mode = (
        str(raw.get("default_view_mode") or result["default_view_mode"]).strip().lower()
    )
    result["default_view_mode"] = (
        mode if mode in {"client", "analyst"} else result["default_view_mode"]
    )
    start_section = str(raw.get("start_section") or result["start_section"]).strip()
    result["start_section"] = (
        start_section
        if start_section in DASHBOARD_SECTION_OPTIONS
        else result["start_section"]
    )
    raw_charts = raw.get("comparison_visible_charts")
    if isinstance(raw_charts, list):
        charts = [str(x) for x in raw_charts if str(x) in COMPARISON_CHART_BLOCKS]
    else:
        charts = list(result["comparison_visible_charts"])
    result["comparison_visible_charts"] = charts or list(
        DEFAULT_DASHBOARD_VIEW_SETTINGS["comparison_visible_charts"]
    )
    result["client_hide_technical"] = bool(
        raw.get("client_hide_technical", result["client_hide_technical"])
    )
    raw_blocks = raw.get("main_visible_blocks")
    known_blocks = {"metrics", "comparison", "summary", "threshold"}
    if isinstance(raw_blocks, list):
        blocks = [str(x) for x in raw_blocks if str(x) in known_blocks]
        result["main_visible_blocks"] = blocks
    try:
        merge_value = float(raw.get("event_title_merge", result["event_title_merge"]))
    except (TypeError, ValueError):
        merge_value = float(result["event_title_merge"])
    # 0 — выключено; всё, что ниже 0.4, слишком рискованно и трактуется как 0.4.
    if merge_value <= 0:
        result["event_title_merge"] = 0.0
    else:
        result["event_title_merge"] = min(0.95, max(0.4, merge_value))
    return result


def story_build_settings_from_project_settings(
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Пороги сборки инфоповодов: похожесть текстов, минимум авторов/сообщений.

    Как и у всех конвертеров в этом модуле, мусор во входе не роняет функцию —
    он молча заменяется значением по умолчанию.
    """
    raw = {}
    if isinstance(settings, dict):
        raw = settings.get("story_build") or {}
    if not isinstance(raw, dict):
        raw = {}
    result = dict(DEFAULT_STORY_BUILD_SETTINGS)

    try:
        similarity = float(raw.get("similarity", result["similarity"]))
    except (TypeError, ValueError):
        similarity = float(result["similarity"])
    # Нижний зажим — не 0: при similarity=0 сравнение "похожесть >= порог" в
    # story_recovery верно для любой пары текстов, и весь корпус схлопывается
    # в один сюжет.
    result["similarity"] = min(0.95, max(0.2, similarity))

    for key in ("min_authors", "min_messages"):
        try:
            value = int(float(raw.get(key, result[key])))
        except (TypeError, ValueError):
            value = int(result[key])
        # Верхний потолок защищает от опечатки вроде "300": такое значение не
        # "ужесточает" планку, а отключает сборку сюжетов насовсем — min_messages
        # в recover_stories отсекает не только маленькие кластеры, но и весь
        # остаток целиком, если тот меньше порога.
        result[key] = max(1, min(50, value))

    return result


def with_story_build(
    settings: dict[str, Any] | None, values: dict[str, Any]
) -> dict[str, Any]:
    """Настройки проекта с заменённым ключом story_build — остальное на месте.

    settings — свободный JSON-столбец без валидации на уровне базы: запись
    settings={'story_build': {...}} вместо dict(current)+ключ стёрла бы
    брендирование, соперников бренда и всё остальное разом.
    """
    updated = dict(settings or {})
    updated["story_build"] = dict(values or {})
    return updated


def report_branding_from_project_settings(
    settings: dict[str, Any] | None, *, project_name: str = ""
) -> dict[str, Any]:
    raw = {}
    if isinstance(settings, dict):
        raw = settings.get("report_branding") or {}
    if not isinstance(raw, dict):
        raw = {}
    result = dict(DEFAULT_REPORT_BRANDING)
    result.update(
        {
            k: str(raw.get(k) or result.get(k) or "").strip()
            for k in [
                "client_name",
                "report_title",
                "footer_text",
                "logo_url",
                "logo_storage_path",
                "logo_filename",
                "logo_mime_type",
            ]
        }
    )
    result["accent_color"] = valid_hex_color(
        raw.get("accent_color"), result["accent_color"]
    )
    result["background_color"] = valid_hex_color(
        raw.get("background_color"), result["background_color"]
    )
    if not result.get("client_name"):
        result["client_name"] = str(project_name or "").strip()
    if not result.get("report_title"):
        result["report_title"] = "Дайджест упоминаний"
    return result


def report_sections_from_project_settings(settings: dict[str, Any] | None) -> list[str]:
    """Какие блоки включать в PNG/DOCX/PDF по умолчанию для проекта.

    Сохранено — как и брендирование — на уровне проекта: аналитик выбирает
    один раз, дальше выгрузка собирается так же, с возможностью поменять
    набор перед конкретной выгрузкой (см. report_export_ui.py).
    """
    raw = {}
    if isinstance(settings, dict):
        raw = settings.get("report_sections")
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_REPORT_SECTIONS)
    result = [s for s in raw if s in REPORT_SECTION_OPTIONS]
    return result or list(DEFAULT_REPORT_SECTIONS)


def category_brands_from_project_settings(
    settings: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """Какие теги проекта считать брендами и какие из них свои.

    Отличить бренд от аналитического разреза машина не может: в одних и тех же
    теговых колонках лежат и «Docke», и «Монтаж», и «PR». Это знание о рынке, и
    его задаёт аналитик один раз на проект.

    Своих брендов бывает несколько — головной, дочерние, отдельные марки. Доля
    голоса считается для группы целиком, поэтому здесь список, а не строка.
    """
    raw = {}
    if isinstance(settings, dict):
        raw = settings.get("category_brands") or {}
    if not isinstance(raw, dict):
        raw = {}

    def _names(key: str) -> list[str]:
        values = raw.get(key) or []
        if isinstance(values, str):
            values = [values]
        result: list[str] = []
        for value in values:
            name = str(value or "").strip()
            if name and name not in result:
                result.append(name)
        return result

    own = _names("own")
    # Бренд не может быть одновременно своим и конкурентом: своя разметка
    # главнее, иначе он попал бы в знаменатель дважды.
    own_lower = {x.casefold() for x in own}
    competitors = [x for x in _names("competitors") if x.casefold() not in own_lower]
    return {"own": own, "competitors": competitors}


def project_topic_profile(project_row: pd.Series | None) -> str:
    settings = project_settings_from_row(project_row) if project_row is not None else {}
    profile = str(settings.get("topic_profile") or "universal")
    profile = LEGACY_PROFILE_ALIASES.get(profile, profile)
    return profile if profile in ALGORITHM_PROFILE_OPTIONS else "universal"


def is_brand_analytics_event_set(events: pd.DataFrame) -> bool:
    """Return True when events were built from Brand Analytics `Сюжет` values.

    Brand Analytics projects can legitimately have small one-off сюжеты, so the
    default small-event filter should not hide them. Algorithmic projects
    (especially driver chats) are much noisier and need a higher threshold.
    """
    if events is None or events.empty or "event_source" not in events.columns:
        return False
    values = events["event_source"].fillna("").astype(str).str.lower()
    return bool(values.str.contains("brand_analytics_story", regex=False).any())


def default_min_event_messages(profile: str, events: pd.DataFrame | None = None) -> int:
    """Default threshold for showing information events in dashboards.

    - Brand Analytics: keep every `Сюжет`, because the source system already
      provides editorial/story grouping.
    - Driver chats and other algorithmic projects: hide tiny clusters by default
      so one-message noise does not become an information event.
    """
    if is_brand_analytics_event_set(events if events is not None else pd.DataFrame()):
        return 1
    return 4


def chart_label_settings_from_project_settings(
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    raw = {}
    if isinstance(settings, dict):
        raw = settings.get("chart_label_settings") or {}
    if not isinstance(raw, dict):
        raw = {}
    result = dict(DEFAULT_CHART_LABEL_SETTINGS)
    font = str(raw.get("font") or result["font"]).strip()
    result["font"] = font or result["font"]
    try:
        size = int(raw.get("font_size", result["font_size"]))
    except Exception:
        size = int(result["font_size"])
    result["font_size"] = max(8, min(28, size))
    position = str(raw.get("position") or result["position"]).strip()
    result["position"] = (
        position if position in CHART_LABEL_POSITION_OPTIONS else result["position"]
    )
    result["show_donut_legend"] = bool(
        raw.get("show_donut_legend", result.get("show_donut_legend", False))
    )
    return result


def chart_label_text_kwargs(
    settings: dict[str, Any] | None, *, chart_type: str = "line"
) -> dict[str, Any]:
    cfg = chart_label_settings_from_project_settings(
        {"chart_label_settings": settings or {}}
    )
    position = cfg.get("position", "top")
    kwargs: dict[str, Any] = {
        "align": "center",
        "font": cfg.get("font", "Arial"),
        "size": int(cfg.get("font_size", 11)),
    }
    if position == "center":
        kwargs.update({"baseline": "middle", "dy": 0})
    elif position == "bottom":
        kwargs.update({"baseline": "top", "dy": 10})
    else:
        kwargs.update({"baseline": "bottom", "dy": -8})
    if chart_type == "bar" and position == "bottom":
        kwargs.update({"baseline": "bottom", "dy": -4})
    return kwargs


def chart_label_radius(settings: dict[str, Any] | None) -> int:
    cfg = chart_label_settings_from_project_settings(
        {"chart_label_settings": settings or {}}
    )
    position = cfg.get("position", "top")
    if position == "center":
        return 72
    if position == "bottom":
        return 54
    return 104
