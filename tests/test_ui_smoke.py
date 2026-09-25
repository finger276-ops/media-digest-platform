"""Дымовой тест интерфейса: боковая навигация, компактная шапка и разделы.

Supabase подменен поддельным клиентом, поэтому тест не ходит в сеть.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from datetime import datetime, timedelta, timezone

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

now = datetime.now(timezone.utc).isoformat()
# Однопиксельный PNG: содержимое не важно, важно что шапка его покажет.
LOGO_PATH = "branding/tn_project/logo.png"
CLIENT.files[LOGO_PATH] = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

CLIENT.db["platform_projects"] = [
    {
        "project_id": "tn_project",
        "project_name": "ТЕХНОНИКОЛЬ",
        "status": "active",
        # Логотип в шапке — тот же файл, что уходит в отчёт: один логотип на
        # проект, загружается в настройках.
        "settings": {
            "report_branding": {
                "logo_storage_path": LOGO_PATH,
                "logo_filename": "logo.png",
                "logo_mime_type": "image/png",
            }
        },
        "created_at": now,
        "updated_at": now,
    }
]
PERIOD_ID = "p_2026_04"
PERIOD_ID_2 = "p_2026_05"

CLIENT.db["platform_periods"] = [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "period_name": "24.04.2026–30.04.2026",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
        "source_filename": "week.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    },
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID_2,
        "period_name": "01.05.2026–07.05.2026",
        "date_from": "2026-05-01",
        "date_to": "2026-05-07",
        "source_filename": "week2.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    },
]


def _message_row(index, sentiment, views, audience, engagement, theme, period_id=PERIOD_ID):
    payload = {
        "message_id": f"{period_id}_m{index}",
        "period_id": period_id,
        "date": "24.04.2026",
        "datetime": "2026-04-24T10:00:00",
        "sentiment": sentiment,
        "views": views,
        "audience": audience,
        "engagement": engagement,
        "likes": engagement,
        "comments": 0,
        "reposts": 0,
        "text_clean": f"Сообщение {index} про {theme}",
        "message_link": f"https://example.com/{index}",
        "platform": "vk.com",
        "author": f"user{index}",
        "tags": theme,
        "event_title": theme,
    }
    return {
        "project_id": "tn_project",
        "period_id": period_id,
        "table_name": "messages",
        "row_id": payload["message_id"],
        "payload": payload,
    }


_THEMES = [("позитив", "Запуск завода"), ("негатив", "Жалобы на монтаж"), ("нейтрал", "Отраслевая статистика")]
CLIENT.db["platform_table_rows"] = [
    _message_row(i, _THEMES[i % 3][0], 10_000 * (i + 1), 5_000, 120, _THEMES[i % 3][1])
    for i in range(12)
] + [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "events",
        "row_id": f"e{i}",
        "payload": {
            "event_id": f"e{i}",
            "period_id": PERIOD_ID,
            "event_title": theme,
            "event_summary": f"Инфоповод про {theme}",
            "message_count": 4,
            "negative_count": 4 if sentiment == "негатив" else 0,
            "chat_count": 2,
            "importance_score": 10 - i,
            "start_date": "2026-04-24",
            "end_date": "2026-04-30",
            "main_tags": theme,
        },
    }
    for i, (sentiment, theme) in enumerate(_THEMES)
]

# Две формулировки одного сюжета: Brand Analytics переименовывает сюжеты между
# периодами, и платформа должна собирать их в один инфоповод.
_SAME_STORY = [
    "ТЕХНОНИКОЛЬ запустила линию по производству кровельных материалов в Рязани",
    "Запуск линии кровельных материалов ТЕХНОНИКОЛЬ в Рязани",
]
CLIENT.db["platform_table_rows"] += [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "events",
        "row_id": f"e_story{i}",
        "payload": {
            "event_id": f"e_story{i}",
            "period_id": PERIOD_ID,
            "event_title": title,
            "event_summary": title,
            "message_count": 6 - i,
            "negative_count": 0,
            "chat_count": 2,
            "importance_score": 20 - i,
            "start_date": "2026-04-24",
            "end_date": "2026-04-30",
            "main_tags": "Производство",
        },
    }
    for i, title in enumerate(_SAME_STORY)
]

# Второй период — чтобы упражнять последовательное сравнение периодов
# (графики, донаты, сравнительная таблица), которое раньше жило в app.py.
CLIENT.db["platform_table_rows"] += [
    _message_row(
        i,
        _THEMES[i % 3][0],
        16_000 * (i + 1),
        7_000,
        180,
        _THEMES[i % 3][1],
        period_id=PERIOD_ID_2,
    )
    for i in range(9)
] + [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID_2,
        "table_name": "events",
        "row_id": f"p2_e{i}",
        "payload": {
            "event_id": f"p2_e{i}",
            "period_id": PERIOD_ID_2,
            "event_title": theme,
            "event_summary": f"Инфоповод про {theme}",
            "message_count": 3,
            "negative_count": 3 if sentiment == "негатив" else 0,
            "chat_count": 2,
            "importance_score": 8 - i,
            "start_date": "2026-05-01",
            "end_date": "2026-05-07",
            "main_tags": theme,
        },
    }
    for i, (sentiment, theme) in enumerate(_THEMES)
]

# Остаток: сообщения, которые не сложились в инфоповод. Аналитик должен иметь
# возможность отнести такое сообщение к теме — автоматика видит одиночную
# публикацию, человек видит, что она про ту же историю.
CLIENT.db["platform_table_rows"] += [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "events",
        "row_id": "e_residual",
        "payload": {
            "event_id": "e_residual",
            "period_id": PERIOD_ID,
            "event_title": "Без сюжета",
            "event_summary": "Публикации, не сложившиеся в общий сюжет",
            "message_count": 2,
            "negative_count": 0,
            "chat_count": 2,
            "importance_score": 30,
            "start_date": "2026-04-24",
            "end_date": "2026-04-30",
            "main_tags": "",
            "is_residual": True,
        },
    }
] + [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "messages",
        "row_id": f"{PERIOD_ID}_res{i}",
        "payload": {
            "message_id": f"{PERIOD_ID}_res{i}",
            "period_id": PERIOD_ID,
            "date": "26.04.2026",
            "datetime": "2026-04-26T10:00:00",
            "sentiment": "нейтрал",
            "views": 100,
            "audience": 500,
            "engagement": 5,
            "text_clean": f"Одиночная публикация номер {i} про кровельные работы",
            "message_link": f"https://example.com/res{i}",
            "platform": "vk.com",
            "author": f"одиночка{i}",
            "tags": "Кровля",
            "event_title": "Без сюжета",
        },
    }
    for i in range(2)
]

# Отзывы с маркетплейса: своя природа сообщения, своя метрика (оценка товара)
# и почти весь негатив периода. В ленте инфоповодов им места нет — у них
# отдельный раздел.
CLIENT.db["platform_table_rows"] += [
    {
        "project_id": "tn_project",
        "period_id": PERIOD_ID,
        "table_name": "messages",
        "row_id": f"{PERIOD_ID}_rev{i}",
        "payload": {
            "message_id": f"{PERIOD_ID}_rev{i}",
            "period_id": PERIOD_ID,
            "date": "25.04.2026",
            "datetime": "2026-04-25T12:00:00",
            "sentiment": sentiment,
            "is_negative": sentiment == "негатив",
            "views": 0,
            "audience": 0,
            "engagement": 0,
            "text_clean": text,
            "message_raw": text,
            "message_link": f"https://wildberries.ru/review/{i}",
            "platform": "wildberries.ru",
            "platform_type": "Отзывы",
            "message_type": "Комментарий",
            "title": title,
            "rating": rating,
            "author": f"Покупатель {i}",
            "tags": "Кровля",
            "event_title": "",
        },
    }
    for i, (sentiment, rating, title, text) in enumerate(
        [
            (
                "негатив",
                "1",
                "ТН / Гибкая черепица 3м2",
                "Недостатки: Пришло в рваном пакете, часть гонтов сломана",
            ),
            (
                "негатив",
                "2",
                "ТН / Гибкая черепица 3м2",
                "Недостатки: Слабая клейкость лепестков",
            ),
            (
                "нейтрал",
                "5",
                "ТН / Гибкая черепица 3м2",
                "Плюсы товара: хорошее качество, внешний вид, простая установка",
            ),
            (
                "позитив",
                "5",
                "ТН / Мягкая кровля для беседки",
                "Плюсы товара: хорошее качество, внешний вид",
            ),
        ]
    )
]

CLIENT.db["platform_ingest_queue"] = [
    {
        "task_id": "ing_demo_1",
        "project_id": "tn_project",
        "source_key": "ba-weekly",
        "storage_path": "inbox/ba-weekly/week1.xlsx",
        "original_filename": "Выгрузка недели.xlsx",
        "file_sha256": "abc",
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
        "period_name": "",
        "error_message": "",
        "created_at": now,
        "finished_at": None,
    },
    {
        "task_id": "ing_demo_2",
        "project_id": "tn_project",
        "source_key": "ba-weekly",
        "storage_path": "inbox/ba-weekly/broken.xlsx",
        "original_filename": "broken.xlsx",
        "file_sha256": "def",
        "status": "error",
        "attempts": 3,
        "max_attempts": 3,
        "period_name": "",
        "error_message": "Не удалось прочитать файл выгрузки.",
        "created_at": now,
        "finished_at": now,
    },
]
CLIENT.db["platform_ingest_sources"] = [
    {
        "source_key": "ba-weekly",
        "project_id": "tn_project",
        "title": "Еженедельный отчет BA",
        "source_system": "brand_analytics",
        "params": {"similarity_threshold": 0.3},
        "is_active": True,
    }
]

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Запуск приложения от имени владельца платформы")
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
at.session_state["platform_is_admin"] = True
at.run()
check("приложение стартовало без исключений", not at.exception, str(at.exception))

nav = {str(b.label): b for b in at.sidebar.button}
check("меню разделов в боковой панели", "Индексы бренда" in nav and "Инфоповоды" in nav, str(list(nav)))
check("работа с данными в том же меню", "Загрузка файла" in nav and "Автозагрузка" in nav, str(list(nav)))
check("раздел «Отчёт» появился", "Отчёт" in nav, str(list(nav)))
check("старого радио «Раздел» больше нет", not any("Раздел" == str(r.label) for r in at.sidebar.radio))


def open_section(name):
    """Нажать пункт меню и дождаться перерисовки."""
    button = {str(b.label): b for b in at.sidebar.button}[name]
    button.click().run()


print("2. Главный экран: шапка и метрики без лишних заголовков")
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader] + [h.value for h in at.header]
joined = " ".join(str(t) for t in texts)
check("имя проекта в шапке", "ТЕХНОНИКОЛЬ" in joined)
check("заголовок приложения ушёл со страницы", "Платформа дайджестов" not in joined, joined[:160])
check("подзаголовка «Период и основные метрики» больше нет", "Период и основные метрики" not in joined)
metric_labels = [str(m.label) for m in at.metric]
check("полоса метрик на месте", "Сообщений" in metric_labels, str(metric_labels[:6]))
check(
    "карточки тональности есть в «Обзоре»",
    "Нейтрал" in metric_labels,
    str(metric_labels[:8]),
)
# Логотип проекта делает рабочее поле брендированным. Берётся тот же файл, что
# уходит в отчёт, — чтобы на экране и в присланном заказчику документе был один
# и тот же знак.
check("логотип проекта в шапке", len(at.image) > 0, f"изображений на странице: {len(at.image)}")
view_controls = [str(c.label) for c in at.checkbox]
check(
    "настройки вида спрятаны в панель «Вид», а не в поток страницы",
    any("Метрики периода" in label for label in view_controls),
    str(view_controls),
)
check(
    "настройки графиков не висят на главной",
    not any("Показывать графики" in str(m.label) for m in at.multiselect),
    str([m.label for m in at.multiselect]),
)
check(
    "клиентский обзор (риски и сигналы) отрисован на главной",
    any("Риски и сигналы" in str(t) for t in texts),
    joined[:160],
)

print("2.5. Раздел «Динамика»: последовательное сравнение двух периодов")
period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
check(
    "селектор периодов найден",
    bool(period_multiselect),
    str([m.label for m in at.sidebar.multiselect]),
)
if period_multiselect:
    period_multiselect[0].set_value([PERIOD_ID, PERIOD_ID_2]).run()
    open_section("Динамика")
    check("раздел открылся без исключений", not at.exception, str(at.exception))
    texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
    check("заголовок «Сравнение периодов» появился", any("Сравнение периодов" in str(t) for t in texts))
    check("панель «Графики динамики» на месте", any("Графики динамики" in str(t) for t in texts))
    check("сравнительная таблица отрисована", bool(at.dataframe))
    metric_deltas = [str(m.delta) for m in at.metric if m.delta]
    check("у метрик есть дельта к предыдущему периоду", bool(metric_deltas), str(metric_deltas)[:200])

    print("2.55. Динамика основных метрик: подписи периодов не накладываются, цвет метрики закреплён")
    # Ровно тот график со скриншота, который пожаловался аналитик: в узких
    # facet-панелях подписи периодов «24.04.2026-30.04.2026» и соседняя
    # слипались при angle=0. Проверяем сам факт: ни у одной оси с периодами
    # угол не нулевой (иначе тест не поймает регресс к прежнему виду).
    all_specs = [
        json.loads(el.proto.spec)
        for el in at.get("vega_lite_chart")
        if getattr(el, "proto", None) is not None
    ]

    def _encodings(spec):
        # facet-график хранит encoding под spec["spec"]["encoding"], обычный - под spec["encoding"].
        found = []
        if "encoding" in spec:
            found.append(spec["encoding"])
        nested = spec.get("spec")
        if isinstance(nested, dict) and "encoding" in nested:
            found.append(nested["encoding"])
        return found

    period_axis_angles = [
        enc["x"]["axis"]["labelAngle"]
        for spec in all_specs
        for enc in _encodings(spec)
        if enc.get("x", {}).get("field") == "Период"
        and "axis" in enc.get("x", {})
        and "labelAngle" in enc["x"]["axis"]
    ]
    check(
        "хотя бы одна ось периода отрисована с закреплённым наклоном",
        bool(period_axis_angles),
        str(period_axis_angles),
    )
    check(
        "ни одна ось периода не осталась с angle=0 (та самая причина наложения подписей)",
        bool(period_axis_angles) and all(a != 0 for a in period_axis_angles),
        str(period_axis_angles),
    )

    metric_color_domains = [
        enc["color"]["scale"]["domain"]
        for spec in all_specs
        for enc in _encodings(spec)
        if enc.get("color", {}).get("field") == "Метрика"
        and "domain" in enc.get("color", {}).get("scale", {})
    ]
    check(
        "цвет метрики закреплён фиксированным доменом (не дефолтной схемой Vega)",
        any(set(d) == {"Сообщения", "Аудитория", "Охват", "Вовлеченность"} for d in metric_color_domains),
        str(metric_color_domains),
    )

    print("2.6. Динамика тональности: 100%-накопленный столбец")
    sentiment_type_select = [
        s for s in at.selectbox if str(s.label) == "Вид тональности"
    ]
    check("селектор вида тональности найден", bool(sentiment_type_select))
    if sentiment_type_select:
        sentiment_type_select[0].set_value("Столбчатая").run()
        check("выбор вида не роняет раздел", not at.exception, str(at.exception))
        bar_specs = [
            json.loads(el.proto.spec)
            for el in at.get("vega_lite_chart")
            if getattr(el, "proto", None) is not None
        ]
        stacked = [
            spec
            for spec in bar_specs
            if spec.get("mark", {}).get("type") == "bar"
            and spec.get("encoding", {}).get("y", {}).get("stack") == "normalize"
        ]
        check(
            "хотя бы один столбец нормализован в 100% (доли, а не сгруппированные рядом)",
            bool(stacked),
            str([spec.get("mark") for spec in bar_specs])[:300],
        )
        if stacked:
            color_domain = (
                stacked[0].get("encoding", {}).get("color", {}).get("scale", {}).get("domain")
            )
            check(
                "цвет закреплён за тональностью в фиксированном порядке (Позитив/Нейтрал/Негатив)",
                color_domain == ["Позитив", "Нейтрал", "Негатив"],
                str(color_domain),
            )
        sentiment_type_select = [
            s for s in at.selectbox if str(s.label) == "Вид тональности"
        ]
        if sentiment_type_select:
            sentiment_type_select[0].set_value("График").run()

    # Возвращаем выбор к одному периоду и на «Обзор» — дальше тест проверяет
    # разделы в исходном однопериодном состоянии.
    period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
    period_multiselect[0].set_value([PERIOD_ID]).run()
    open_section("Обзор")
    check("возврат к одному периоду не роняет приложение", not at.exception, str(at.exception))

print("3. Переход в «Индексы бренда» одним кликом")
open_section("Индексы бренда")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
check("заголовок раздела на месте", any("Индексы бренда" in str(t) for t in texts))
labels = {str(m.label): m.value for m in at.metric}
check("карточка BPI с расшифровкой названия", any("BPI · Индекс восприятия" in k for k in labels), str(list(labels))[:200])
check("карточка NSS с названием", any(k.startswith("NSS · ") for k in labels), str(list(labels))[:200])
check("саммари не примешивается к разделу", not any("Саммари периода" in str(t) for t in texts))
# Формулы — собственная методика платформы, а не то, что заказчик читает с
# экрана. Вместо них в таблице стоит вывод аналитика: что метрика означает для
# бренда. Разобрать формулы аналитик по-прежнему может — в блоке настройки.
captions = [str(c.value) for c in at.caption]
# Одинаковые числа должны и выглядеть одинаково: метрики рисовались в шести
# местах по-разному, и глаз каждый раз перестраивался заново.
index_labels = [str(m.label) for m in at.metric]
check(
    "все восемь индексов на месте",
    sum(1 for label in index_labels if " · " in label) >= 8,
    str(index_labels)[:220],
)
check(
    "столбец выводов объяснён",
    any("«Вывод» заполняется вручную" in c for c in captions),
    str(captions)[:220],
)
# Подпись обещала, что вывод «попадает в выгрузку», и аналитик искал его в
# отчёте Word/PDF/PNG, куда индексы не входят вовсе. Выводы уходят только в
# CSV таблицы индексов. Если индексы когда-нибудь добавят в конструктор отчёта,
# последняя проверка потребует поправить и подпись.
from services.dashboard_config import REPORT_SECTION_OPTIONS  # noqa: E402

notes_caption = next((c for c in captions if "«Вывод» заполняется вручную" in c), "")
check("подпись не обещает «попадает в выгрузку»", "попадает в выгрузку" not in notes_caption, notes_caption)
check("подпись называет CSV", "CSV" in notes_caption, notes_caption)
brand_in_report = any("индекс" in str(v).lower() for v in REPORT_SECTION_OPTIONS.values())
check(
    "подпись согласована с составом отчёта",
    brand_in_report == ("не входят" not in notes_caption),
    f"индексы в отчёте: {brand_in_report}; подпись: {notes_caption}",
)
downloads = [str(e.proto.label) for e in at.get("download_button")]
check("кнопка CSV на месте", "Скачать метрики в CSV" in downloads, str(downloads))
check(
    "раскрытие формул осталось владельцу, рядом с настройкой весов",
    any("Как считается каждая метрика" in str(e.label) for e in at.expander),
    str([e.label for e in at.expander]),
)
check(
    "методика не вынесена в подпись раздела",
    not any("Формулы и входные числа" in c for c in captions),
    str(captions)[:200],
)
# Отдельная выгрузка по категории — запасной путь: у большинства проектов
# конкуренты уже размечены тегами в самой выгрузке. Развёрнутым этот блок
# занимал экран под задачу, которая возникает редко.
check(
    "выгрузка по категории свёрнута в раскрывашку",
    any("Выгрузка по категории" in str(e.label) for e in at.expander),
    str([e.label for e in at.expander]),
)
check(
    "и больше не отдельный заголовок раздела",
    not any("Выгрузка по категории" == str(h.value).strip() for h in at.subheader),
    str([h.value for h in at.subheader]),
)

print("3.02. Индексы бренда: диапазон дат сужает выборку карточек")
# Сообщения периода p_2026_04 размечены тремя разными датами (24-26.04:
# основные, отзывы, остаток без сюжета) - ровно тот случай, ради которого
# нужен диапазон: посмотреть индексы за часть периода, а не только целиком.
range_expander = [e for e in at.expander if str(e.label) == "Диапазон дат"]
check(
    "раскрывашка «Диапазон дат» найдена (в данных больше одного дня)",
    bool(range_expander),
    str([e.label for e in at.expander]),
)
if range_expander:
    date_inputs = {str(d.label): d for d in at.date_input}
    check(
        "оба поля диапазона на месте (С / По)",
        "С" in date_inputs and "По" in date_inputs,
        str(list(date_inputs.keys())),
    )
    if "С" in date_inputs and "По" in date_inputs:
        original_from = date_inputs["С"].value
        original_to = date_inputs["По"].value
        check(
            "по умолчанию диапазон открыт на весь период (С раньше По)",
            original_from < original_to,
            str((original_from, original_to)),
        )
        cards_before = {
            str(m.label): str(m.value) for m in at.metric if " · " in str(m.label)
        }

        date_inputs["С"].set_value(original_from + timedelta(days=1)).run()
        check("сужение диапазона не роняет раздел", not at.exception, str(at.exception))
        captions_after = [str(c.value) for c in at.caption]
        check(
            "подпись честно называет число отфильтрованных сообщений",
            any(
                "Выбрано:" in c and "сообщений из" in c for c in captions_after
            ),
            str(captions_after)[:300],
        )
        check(
            "дельта к предыдущему периоду скрыта для произвольного диапазона",
            any(
                "Изменение к предыдущему периоду не показано" in c
                for c in captions_after
            ),
            str(captions_after)[:300],
        )
        cards_after = {
            str(m.label): str(m.value) for m in at.metric if " · " in str(m.label)
        }
        check(
            "хотя бы одна карточка индекса пересчиталась на суженном диапазоне",
            cards_before != cards_after,
            str((cards_before, cards_after))[:300],
        )

        # Инвертированный диапазон (начало позже конца) не должен падать -
        # ожидаем явное предупреждение и откат на полный период, а не тихий сбой.
        date_inputs = {str(d.label): d for d in at.date_input}
        date_inputs["По"].set_value(original_from).run()
        check(
            "инвертированный диапазон не роняет раздел", not at.exception, str(at.exception)
        )
        check(
            "инвертированный диапазон честно предупреждён, а не тихо проглочен",
            any(
                "Начало диапазона позже конца" in str(w.value) for w in at.warning
            ),
            str([str(w.value) for w in at.warning]),
        )

        # Возвращаем диапазон к исходному - дальше тест полагается на карточки
        # без сужения.
        date_inputs = {str(d.label): d for d in at.date_input}
        date_inputs["С"].set_value(original_from).run()
        date_inputs = {str(d.label): d for d in at.date_input}
        date_inputs["По"].set_value(original_to).run()
        check(
            "возврат диапазона к исходному не роняет раздел",
            not at.exception,
            str(at.exception),
        )

print("3.1. Динамика индексов: свёрнута и в хронологическом порядке")
# Периоды выбираются в обратном порядке: май, потом апрель. График динамики
# рисует линию между соседними точками, и порядок выбора выдал бы движение,
# которого не было.
period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
if period_multiselect:
    period_multiselect[0].set_value([PERIOD_ID_2, PERIOD_ID]).run()
    open_section("Индексы бренда")
    check("раздел открылся без исключений", not at.exception, str(at.exception))
    check(
        "динамика убрана в раскрывашку",
        any("Динамика индексов" in str(e.label) for e in at.expander),
        str([e.label for e in at.expander]),
    )
    check(
        "и больше не отдельный заголовок",
        not any("Динамика индексов" == str(h.value).strip() for h in at.subheader),
        str([h.value for h in at.subheader]),
    )
    dynamics = [
        list(d.value["Период"])
        for d in at.dataframe
        if "Период" in getattr(d.value, "columns", [])
    ]
    check(
        "периоды идут хронологически, а не в порядке выбора",
        any(
            len(rows) >= 2 and "24.04" in str(rows[0]) and "01.05" in str(rows[1])
            for rows in dynamics
        ),
        str(dynamics)[:220],
    )

    print("3.12. Динамика индексов бренда: тот же наклон осей и закреплённый цвет метрики")
    brand_specs = [
        json.loads(el.proto.spec)
        for el in at.get("vega_lite_chart")
        if getattr(el, "proto", None) is not None
    ]
    brand_period_angles = [
        spec["encoding"]["x"]["axis"]["labelAngle"]
        for spec in brand_specs
        if spec.get("encoding", {}).get("x", {}).get("field") == "Период"
        and "axis" in spec.get("encoding", {}).get("x", {})
        and "labelAngle" in spec["encoding"]["x"]["axis"]
    ]
    check(
        "ось периода в «Индексах бренда» тоже с ненулевым наклоном",
        bool(brand_period_angles) and all(a != 0 for a in brand_period_angles),
        str(brand_period_angles),
    )
    brand_color_domains = [
        spec["encoding"]["color"]["scale"]["domain"]
        for spec in brand_specs
        if spec.get("encoding", {}).get("color", {}).get("field") == "Метрика"
        and "domain" in spec.get("encoding", {}).get("color", {}).get("scale", {})
    ]
    check(
        "цвет метрики бренда закреплён полным доменом BPI..ERR, а не только выбранными",
        any(
            {"BPI", "NSS", "SES", "TVS", "SOV", "ReachScore", "ER", "ERR"} == set(d)
            for d in brand_color_domains
        ),
        str(brand_color_domains),
    )

    # Возвращаем один период: дальше тест проверяет разделы в исходном виде.
    period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
    period_multiselect[0].set_value([PERIOD_ID]).run()

    print("3.15. Динамика индексов: опциональная подгрузка всех периодов проекта")
    # В боковой панели выбран один период из двух — чекбокс «все периоды»
    # должен появиться (тянуть данные лишнего периода без спроса нельзя) и,
    # при включении, честно посчитать динамику по обоим.
    open_section("Индексы бренда")
    check("раздел открылся без исключений", not at.exception, str(at.exception))
    all_periods_checkbox = [
        c for c in at.checkbox if "Учесть все периоды проекта" in str(c.label)
    ]
    check(
        "чекбокс появляется, когда выбран не весь проект",
        len(all_periods_checkbox) == 1,
        str([str(c.label) for c in at.checkbox]),
    )
    if all_periods_checkbox:
        check(
            "подпись честно называет числа (2 всего, 1 выбран)",
            "(2)" in str(all_periods_checkbox[0].label)
            and "(1)" in str(all_periods_checkbox[0].label),
            str(all_periods_checkbox[0].label),
        )
        all_periods_checkbox[0].set_value(True).run()
        check(
            "включение не роняет раздел",
            not at.exception,
            str(at.exception),
        )
        dynamics_all = [
            list(d.value["Период"])
            for d in at.dataframe
            if "Период" in getattr(d.value, "columns", [])
        ]
        check(
            "динамика теперь по обоим периодам, а не только по выбранному",
            any(
                len(rows) >= 2 and "24.04" in str(rows[0]) and "01.05" in str(rows[1])
                for rows in dynamics_all
            ),
            str(dynamics_all)[:220],
        )
        # Возвращаем чекбокс в исходное состояние — дальше тест снова
        # рассчитывает на один выбранный период.
        all_periods_checkbox = [
            c for c in at.checkbox if "Учесть все периоды проекта" in str(c.label)
        ]
        if all_periods_checkbox:
            all_periods_checkbox[0].set_value(False).run()

print("3.2. Раздел «Теги»: статистика и карточка тега")
open_section("Теги")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
check("заголовок раздела на месте", any("Статистика тегов" in str(t) for t in texts))
check("таблица статистики тегов отрисована", bool(at.dataframe))
# Полоса из семи карточек уместна в «Обзоре», где показатели периода и есть
# содержание. В рабочих разделах она занимала треть экрана и отодвигала вниз
# таблицы, ради которых раздел и открывают.
work_metrics = [str(m.label) for m in at.metric]
check(
    "полосы тональности в рабочем разделе нет",
    "Нейтрал" not in work_metrics,
    str(work_metrics[:8]),
)
captions = [str(c.value) for c in at.caption]
check(
    "но числа периода остались одной строкой",
    any("сообщений" in c and "аудитория" in c for c in captions),
    str(captions)[:220],
)

print("3.5. Раздел «Инфоповоды»: склейка похожих заголовков видна аналитику")
open_section("Инфоповоды")
check("раздел открылся без исключений", not at.exception, str(at.exception))
# Инфоповоды собирает машина: часть приходит размеченной из системы мониторинга,
# часть платформа досчитывает сама. Аналитик должен знать, что список нужно
# проверить, прежде чем отправлять его заказчику.
infos = [str(i.value) for i in at.info]
check(
    "предупреждение о машинной сборке на месте",
    any("требуют проверки аналитика" in text for text in infos),
    str(infos)[:220],
)
check(
    "сказано, где править",
    any("переименовать" in text or "объединить" in text for text in infos),
    str(infos)[:220],
)

print("3.55. Топ инфоповодов: горизонтальный бар с долей негатива по цвету")
event_bar_specs = [
    json.loads(el.proto.spec)
    for el in at.get("vega_lite_chart")
    if getattr(el, "proto", None) is not None
]
top_events_specs = [
    spec
    for spec in event_bar_specs
    if spec.get("mark", {}).get("type") == "bar"
    and spec.get("encoding", {}).get("color", {}).get("field") == "Доля негатива"
]
check(
    "график топ-инфоповодов отрисован",
    bool(top_events_specs),
    str([spec.get("mark") for spec in event_bar_specs])[:300],
)
if top_events_specs:
    color_scale = top_events_specs[0]["encoding"]["color"].get("scale", {})
    check(
        "доля негатива — один оттенок (последовательная шкала), не радуга",
        color_scale.get("scheme") == "reds",
        str(color_scale),
    )
    check(
        "домен цвета зафиксирован 0..1 — периоды сравнимы между собой по цвету",
        color_scale.get("domain") == [0, 1],
        str(color_scale),
    )

expanders = [str(e.label) for e in at.expander]
check(
    "блок со склейкой заголовков на месте",
    any("Склеено похожих заголовков" in label for label in expanders),
    str(expanders),
)
# Описание правилось и раньше, но в свёрнутом блоке под таблицей: выбрать
# строку, прокрутить, раскрыть, сохранить — и так для каждого из двадцати
# инфоповодов дайджеста. Теперь правка идёт прямо в таблице.
event_tables = [
    set(str(c) for c in getattr(d.value, "columns", []))
    for d in at.dataframe
]
check(
    "таблица инфоповодов даёт править описание",
    any({"Описание", "Открыть"} <= cols for cols in event_tables),
    str(event_tables)[:260],
)
# То, что не сложилось в инфоповод, показывается отдельным блоком — и не как
# тупик: автоматика видит одиночную публикацию, аналитик видит, что она про ту
# же историю, и должен иметь возможность отнести её к теме.
check(
    "блок «Вне инфоповодов» на месте",
    any("Вне инфоповодов" in str(e.label) for e in at.expander),
    str([e.label for e in at.expander]),
)
check(
    "сообщение из остатка можно отнести к инфоповоду",
    any("Отнести к инфоповоду" in cols for cols in event_tables),
    str(event_tables)[:300],
)
check(
    "остаток не встал первым в списке инфоповодов",
    not any("Без сюжета" in str(t) for t in texts[:3]),
    str(texts[:3])[:200],
)
view_mode = [r for r in at.sidebar.radio if str(r.label) == "Вид дашборда"]
if view_mode:
    view_mode[0].set_value("analyst").run()
merge_control = [str(s.label) for s in at.selectbox]
check(
    "в аналитическом виде есть переключатель силы склейки",
    any("Склейка похожих заголовков" in label for label in merge_control),
    str(merge_control),
)
check("аналитический вид не уронил раздел", not at.exception, str(at.exception))

print("3.56. Ручные правки инфоповодов: скрытую тему можно вернуть")
# Скрытая по ошибке тема исчезала из таблицы вместе с кнопками правки, и
# вернуть её из интерфейса было нельзя. Теперь такие правки собраны в список
# с кнопкой «Отменить». Счётчики версий кеша общие на процесс, поэтому
# clear_platform_caches отсюда виден и прогону AppTest.
from services.cached_store import clear_platform_caches  # noqa: E402


def event_table_titles():
    """Названия строк таблицы инфоповодов на экране."""
    for frame in at.dataframe:
        value = frame.value
        if "Сюжет / инфоповод" in getattr(value, "columns", []):
            return [str(x) for x in value["Сюжет / инфоповод"]]
    return []


check("до скрытия тема «Запуск завода» в таблице", any("Запуск завода" in t for t in event_table_titles()), str(event_table_titles()))
# load_generated_tables приписывает к id события префикс периода: настоящий
# id темы — p_2026_04__e0. С голым «e0» правка ни на что не действовала бы, и
# тест проверял бы только список, а не то, что тема пропала и вернулась.
HIDDEN_EVENT_ID = f"{PERIOD_ID}__e0"
CLIENT.db.setdefault("platform_manual_rows", []).append(
    {
        "project_id": "tn_project",
        "table_name": "event_edits",
        "row_key": f"event_edit::{HIDDEN_EVENT_ID}",
        "payload": {
            "event_id": HIDDEN_EVENT_ID,
            "status": "hidden",
            "op_id": "op-smoke",
            "label": "Запуск завода",
        },
        "updated_at": now,
    }
)
clear_platform_caches("tn_project")
at.run()
check("раздел со скрытой темой открылся без исключений", not at.exception, str(at.exception))
check(
    "скрытая тема действительно пропала из таблицы",
    event_table_titles() and not any("Запуск завода" in t for t in event_table_titles()),
    str(event_table_titles()),
)
undo_expanders = [str(e.label) for e in at.expander if "Ручные правки инфоповодов" in str(e.label)]
check("список ручных правок на месте", bool(undo_expanders), str([str(e.label) for e in at.expander]))
undo_captions = [str(c.value) for c in at.caption if "Скрыт инфоповод" in str(c.value)]
check("пункт отмены назван по теме, а не по id", undo_captions == ["Скрыт инфоповод «Запуск завода»"], str(undo_captions))
undo_buttons = [b for b in at.button if str(b.label) == "Отменить" and "op-smoke" in str(b.key)]
check("у скрытой темы есть кнопка «Отменить»", bool(undo_buttons), str([b.key for b in at.button if str(b.label) == "Отменить"]))
if undo_buttons:
    undo_buttons[0].click().run()
    check("отмена не уронила раздел", not at.exception, str(at.exception))
    check(
        "после «Отменить» тема снова в таблице",
        any("Запуск завода" in t for t in event_table_titles()),
        str(event_table_titles()),
    )
    stored_e0 = [
        row
        for row in CLIENT.db["platform_manual_rows"]
        if row.get("project_id") == "tn_project"
        and row.get("row_key") == f"event_edit::{HIDDEN_EVENT_ID}"
    ]
    check(
        "правок у темы до скрытия не было — строка правки удалена, а не превращена в ручную",
        not stored_e0,
        str(stored_e0),
    )
    check(
        "после отмены список правок пуст и исчез",
        not any("Ручные правки инфоповодов" in str(e.label) for e in at.expander),
        str([str(e.label) for e in at.expander]),
    )

print("3.6. Раздел «Отзывы»: репутация товара и претензии покупателей")
open_section("Отзывы")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
check("заголовок раздела на месте", any("Отзывы о товаре" in str(t) for t in texts))
review_metrics = {str(m.label): str(m.value) for m in at.metric}
check("счётчик отзывов посчитал все четыре", review_metrics.get("Отзывов") == "4", str(review_metrics))
check(
    "средняя оценка посчитана по колонке «Оценка»",
    review_metrics.get("Средняя оценка") == "3.25",
    str(review_metrics),
)
check(
    "претензии отделены от похвалы",
    review_metrics.get("Претензий") == "2",
    str(review_metrics),
)
check("товары посчитаны", review_metrics.get("Товаров") == "2", str(review_metrics))
check(
    "плюсы товара сложились в счёт",
    any("Хорошее качество — **2**" in str(t) for t in texts),
    str([t for t in texts if "качество" in str(t)])[:200],
)
check(
    "претензии показаны таблицей, а не сводкой",
    bool(at.dataframe),
    "таблиц на странице нет",
)

print("3.7. Раздел «Сообщения»: топ и вся лента рендерятся без исключений")
open_section("Сообщения")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
# Заголовок нейтрален к режиму (см. messages_ui.render_messages_block) -
# раньше здесь было "Ключевые сообщения", что врало при "Всей ленте".
check("заголовок раздела на месте", any("Сообщения" in str(t) for t in texts))
mode_control = [r for r in at.radio if str(r.label) == "Режим просмотра сообщений"]
check("переключатель режима на месте", bool(mode_control), str([str(r.label) for r in at.radio]))
if mode_control:
    mode_control[0].set_value("Вся лента").run()
    check("вся лента открывается без исключений", not at.exception, str(at.exception))

print("4. Раздел «Отчёт» держит саммари и выгрузки")
open_section("Отчёт")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader]
check("саммари переехало сюда", any("Саммари" in str(t) for t in texts), str(texts)[:200])
expanders = [str(e.label) for e in at.expander]
check(
    "панель генерации ИИ видна владельцу платформы",
    any("Тексты от ИИ" in label for label in expanders),
    str(expanders),
)
infos = [str(i.value) for i in at.info]
check(
    "без ключей панель объясняет, чего не хватает, а не падает",
    any("не настроена" in text for text in infos),
    str(infos)[:300],
)
ai_access = [str(s.label) for s in at.selectbox]
check(
    "владелец может открыть генерацию редакторам проекта",
    any("Кому доступна генерация" in label for label in ai_access),
    str(ai_access),
)

print("5. Раздел «Автозагрузка» по-прежнему работает")
# Второй источник, который замолчал 20 дней назад: письмо не пришло или n8n
# упал — задачи нет, и очередь об этом не скажет. Задача «done», чтобы не
# менять счётчик «В очереди» ниже.
_silent_since = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
CLIENT.db["platform_ingest_sources"].append(
    {
        "source_key": "ba-silent",
        "project_id": "tn_project",
        "title": "Замолчавшая рассылка",
        "source_system": "brand_analytics",
        "params": {},
        "is_active": True,
        "created_at": _silent_since,
    }
)
CLIENT.db["platform_ingest_queue"].append(
    {
        "task_id": "ing_demo_silent",
        "project_id": "tn_project",
        "source_key": "ba-silent",
        "storage_path": "inbox/ba-silent/old.xlsx",
        "original_filename": "old.xlsx",
        "file_sha256": "ghi",
        "status": "done",
        "attempts": 1,
        "max_attempts": 3,
        "period_name": "",
        "error_message": "",
        "created_at": _silent_since,
        "finished_at": _silent_since,
    }
)
open_section("Автозагрузка")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.subheader] + [h.value for h in at.header]
check("очередь автозагрузки на месте", any("Очередь автозагрузки" in str(t) for t in texts))
metrics = {str(m.label): m.value for m in at.metric}
check("метрика «В очереди» = 1", any("В очереди" in k and v == "1" for k, v in metrics.items()), str(metrics))

print("5.1. Автозагрузка: молчание источника видно")
check("блок «Поступление файлов» на месте", any("Поступление файлов" in str(t) for t in texts))
captions = [str(c.value) for c in at.caption]
check(
    "честно сказано, что платформа видит только дошедшие файлы",
    any("видит только те файлы" in c and "не появится" in c for c in captions),
    str(captions)[:300],
)
warnings = [str(w.value) for w in at.warning]
check(
    "предупреждение о замолчавшем источнике",
    any("Замолчавшая рассылка" in w and "больше 8 дней" in w for w in warnings),
    str(warnings),
)
# Название источника по умолчанию — имя проекта, у двух источников оно
# совпадает; различает их ключ из шаблона n8n.
check(
    "в предупреждении назван ключ источника",
    any("Замолчавшая рассылка" in w and "«ba-silent»" in w for w in warnings),
    str(warnings),
)
check(
    "источник, от которого файл пришёл сегодня, не тревожит",
    not any("Еженедельный отчет BA" in w for w in warnings),
    str(warnings),
)
freshness_tables = [
    d.value for d in at.dataframe if "Последний файл" in getattr(d.value, "columns", [])
]
check(
    "таблица с датой последнего файла по каждому источнику",
    bool(freshness_tables) and len(freshness_tables[0]) == 2,
    str([list(getattr(d.value, "columns", [])) for d in at.dataframe]),
)
check(
    "в таблице есть ключ каждого источника",
    bool(freshness_tables)
    and "Ключ" in freshness_tables[0].columns
    and "ba-silent" in set(freshness_tables[0]["Ключ"]),
    str([list(getattr(d.value, "columns", [])) for d in at.dataframe]),
)
# Порог настраивается в форме источника: у месячной выгрузки 8 дней тишины —
# норма. Без записи в params настройка была бы пустой ручкой.
[t for t in at.text_input if str(t.label) == "Ключ источника"][0].set_value("ba-monthly")
[n for n in at.number_input if "файлов нет дольше" in str(n.label)][0].set_value(31)
[b for b in at.button if str(b.label) == "Сохранить источник"][0].click().run()
check("сохранение источника не роняет раздел", not at.exception, str(at.exception))
saved = next(
    (s for s in CLIENT.db["platform_ingest_sources"] if s["source_key"] == "ba-monthly"), {}
)
check(
    "порог из формы сохранён в источник",
    (saved.get("params") or {}).get("stale_after_days") == 31,
    str(saved),
)

print("6. Страница загрузки файла")
open_section("Загрузка файла")
check("страница загрузки без исключений", not at.exception, str(at.exception))
upload_expanders = [str(e.label) for e in at.expander]
check(
    "блок порогов сборки инфоповодов виден",
    any("Что платформа считает инфоповодом" in label for label in upload_expanders),
    str(upload_expanders),
)

print("6.5. История периодов: список отрисован")
open_section("История периодов")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [h.value for h in at.header]
check("заголовок раздела на месте", any("История периодов" in str(t) for t in texts))
check("таблица периодов отрисована", bool(at.dataframe))

print("6.7. Сессии: собственная вкладка видна как онлайн")
open_section("Сессии")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.header]
check("заголовок раздела на месте", any("Сессии" in str(t) for t in texts))
presence_metrics = {str(m.label): str(m.value) for m in at.metric}
check(
    "счётчик «Сейчас онлайн» посчитал текущую вкладку",
    presence_metrics.get("Сейчас онлайн") == "1",
    str(presence_metrics),
)
check(
    "роль владельца попала в таблицу сессий",
    any("Онлайн сейчас" in str(t) for t in texts),
    str(texts)[:200],
)

print("7. Раздел «Платформа»: управление проектами")
open_section("Проекты")
check("раздел открылся без исключений", not at.exception, str(at.exception))
texts = [m.value for m in at.markdown] + [h.value for h in at.header]
check("заголовок раздела на месте", any("Управление проектами" in str(t) for t in texts))
check("существующий проект виден в таблице", bool(at.dataframe))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Дымовой тест интерфейса пройден.")
