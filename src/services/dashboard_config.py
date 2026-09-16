# -*- coding: utf-8 -*-
"""Константы конфигурации дашборда: профили алгоритма, разделы навигации,
настройки подписей графиков, брендирование отчётов, клиентский вид.

Общие для app.py и нескольких *_ui.py модулей, поэтому вынесены сюда, а не
дублируются.
"""

from __future__ import annotations

from .event_titles import DEFAULT_SIMILARITY

ALGORITHM_PROFILE_OPTIONS = {
    "universal": "Универсальный",
    "brand_monitoring": "Бренд-мониторинг",
    "construction_materials": "Строительство / материалы",
}

# Профили водительских чатов удалены в версии 4.10.0: проекты со старым
# значением открываются как универсальные.
LEGACY_PROFILE_ALIASES = {
    "driver_chats": "universal",
    "taxi_legacy": "universal",
}

CHART_LABEL_POSITION_OPTIONS = {
    "top": "У верхнего края",
    "center": "В центре",
    "bottom": "У нижнего края",
}
CHART_LABEL_FONT_OPTIONS = [
    "Arial",
    "Inter",
    "Roboto",
    "Verdana",
    "Tahoma",
    "Times New Roman",
]
DEFAULT_CHART_LABEL_SETTINGS = {
    "font": "Arial",
    "font_size": 11,
    "position": "top",
    "show_donut_legend": False,
}

REPORT_TEMPLATE_OPTIONS = {
    "summary": "Краткое саммари",
    "client_overview": "Клиентский обзор",
    "comparison": "Сравнительный отчет",
    "full": "Полный отчет",
}

DEFAULT_REPORT_BRANDING = {
    "client_name": "",
    "report_title": "Дайджест упоминаний",
    "accent_color": "#2563eb",
    "background_color": "#ffffff",
    "footer_text": "",
    "logo_url": "",
    "logo_storage_path": "",
    "logo_filename": "",
    "logo_mime_type": "",
}

COMPARISON_CHART_BLOCKS = [
    "Динамика основных метрик",
    "Динамика тональности",
    "Сравнение выбранной метрики",
    "Круговые диаграммы тональности",
]

DEFAULT_DASHBOARD_VIEW_SETTINGS = {
    "default_view_mode": "client",  # client / analyst
    "start_section": "Обзор",
    "comparison_visible_charts": ["Динамика основных метрик", "Динамика тональности"],
    "client_hide_technical": True,
    "main_visible_blocks": ["metrics", "comparison", "summary", "threshold"],
    # Порог склейки инфоповодов с близкими заголовками. 0 — склейка выключена,
    # остаётся только точное совпадение нормализованного заголовка.
    "event_title_merge": DEFAULT_SIMILARITY,
}

# Разделы аналитики. Они же — пункты бокового меню: до содержимого
# раздела теперь один экран, а не пять.
DASHBOARD_SECTION_OPTIONS = [
    "Обзор",
    "Индексы бренда",
    "Теги",
    "Инфоповоды",
    # Отзывы стоят рядом с инфоповодами, потому что это их изнанка: почти весь
    # негатив периода приходит претензиями покупателей, а не новостями рынка,
    # и в ленте инфоповодов такому сообщению места нет.
    "Отзывы",
    "Сообщения",
    "Динамика",
    "Отчёт",
]

# Старые названия разделов из сохранённых настроек проектов.
SECTION_ALIASES = {
    "Клиентский обзор": "Обзор",
    "Ключевые сообщения": "Сообщения",
    "Саммари": "Отчёт",
}
