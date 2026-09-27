# -*- coding: utf-8 -*-
"""Прочерк вместо нуля: аудитории, охвата и вовлечённости нет в выгрузке (105).

Пустая ячейка при разборе выгрузки становится нулём. Поэтому выгрузка без
колонки «Просмотры» показывала «Охват 0» в «Обзоре», «Динамике», отчёте и
саммари, а сравнение с периодом, где охват был, — «−100 %» и красную стрелку.
Ноль и «данных нет» теперь различаются:

- при загрузке в каждое сообщение пишется, какие метрики в выгрузке были
  (message_normalize.metrics_in_source → колонка metrics_in_source);
- у периодов, загруженных раньше, признака нет — действует правило «весь
  период нули, значит, колонки не было»;
- признак ставится на выгрузку-период и наследуется срезами (день, тег,
  инфоповод), как признак разметки тональности;
- изменение метрики показывается, только если она есть в обоих периодах.

Мутационные проверки (что ломает какой тест):
- metrics_in_source считает колонку из нулей отсутствующей (ищет не цифру, а
  ненулевую цифру) -> «колонка из нулей — законный ноль» краснеет;
- preprocess не пишет metrics_in_source -> «вовлечённость из нулей после
  загрузки — законный ноль» краснеет (правило для старых периодов дало бы
  прочерк);
- _row_metric_known без правила для старых периодов (всегда True) -> «старый
  период без охвата: прочерк» краснеет;
- metric_known считает по срезу, а не по периоду (без PERIOD_METRIC_COLUMNS)
  -> «день с нулевым охватом внутри выгрузки с охватом — ноль» краснеет;
- metric_text без проверки -> «Обзор: охват — прочерк» краснеет;
- comparison_row без metrics_comparable -> «Динамика: Δ охвата — прочерк, не
  −100 %» краснеет;
- build_period_change_insights без проверки -> «клиентский обзор: нет
  «снизилось» по охвату» краснеет;
- comparison_block без проверки -> «карточка ИИ: нет «было …, стало 0»»
  краснеет;
- _docx_metric печатает число -> «Word: охват — нет в выгрузке» краснеет;
- в render_project_intro снова показывать дельту без metrics_comparable ->
  «Обзор: к прошлому периоду без охвата изменения нет» краснеет;
- _volume_cards без metric_missing -> «Обзор: охват — прочерк» краснеет;
- карточка сообщения печатает число без признака -> «Сообщения: «охват: —»»
  краснеет;
- таблица тегов без прочерка -> «Теги: охват у тегов — прочерк» краснеет;
- comparison_visual_rows без признака -> «на графике точка без охвата
  помечена» краснеет;
- build_tag_change_table без comparable -> «изменения тегов: … — прочерк»
  краснеет;
- metrics_block без metric_missing -> «карточка ИИ: охват — «нет данных»»
  краснеет;
- без подписей _metric_notes -> «сказано, почему изменения нет» и «сказано,
  что охват есть не везде» краснеют.
"""

import os
import sys
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"
os.environ["AI_PROVIDER"] = "off"

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services import metrics_compute as mc  # noqa: E402
from services.message_normalize import metrics_in_source  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. metrics_in_source: какие метрики были в выгрузке")
full = pd.DataFrame({"Аудитория": ["100", ""], "Просмотры": ["5 000", ""], "Вовлечённость": ["3", "1"]})
check("все три колонки с числами", metrics_in_source(full) == "audience|reach|engagement", metrics_in_source(full))
no_views = pd.DataFrame({"Аудитория": ["100"], "Просмотры": ["", ""][:1], "Вовлечённость": ["0"]})
check("пустая колонка «Просмотры» — охвата нет", "reach" not in metrics_in_source(no_views), metrics_in_source(no_views))
check("колонка из нулей — законный ноль", "engagement" in metrics_in_source(no_views), metrics_in_source(no_views))
check("синоним «Охват» тоже считается", metrics_in_source(pd.DataFrame({"Охват": ["7"]})) == "reach")
check("без колонок — пусто", metrics_in_source(pd.DataFrame({"Текст": ["a"]})) == "")

print("2. Признак периода: точный у новых загрузок, правило у старых")


def frame(period_id, *, views, audience=None, engagement=None, declared=None):
    n = len(views)
    data = {
        "period_id": [period_id] * n,
        "views": views,
        "audience": audience if audience is not None else [100] * n,
        "engagement": engagement if engagement is not None else [1] * n,
        "sentiment": ["нейтральная"] * n,
    }
    if declared is not None:
        data[mc.SOURCE_METRICS_COLUMN] = [declared] * n
    return pd.DataFrame(data)


new_zero = mc.prepare_dashboard_messages(frame("n", views=[0, 0], engagement=[0, 0], declared="audience|engagement"))
check("новая загрузка без просмотров: охвата нет", mc.metric_known(new_zero, "reach") is False)
check("новая загрузка с нулевой вовлечённостью: законный ноль", mc.metric_known(new_zero, "engagement") is True)
old_zero = mc.prepare_dashboard_messages(frame("o", views=[0, 0, 0]))
check("старый период без охвата: прочерк", mc.metric_known(old_zero, "reach") is False)
old_some = mc.prepare_dashboard_messages(frame("o", views=[0, 12, 0]))
check("старый период с охватом у части сообщений: охват есть", mc.metric_known(old_some, "reach") is True)
check(
    "день с нулевым охватом внутри выгрузки с охватом — ноль",
    mc.metric_known(old_some.iloc[[0]], "reach") is True,
    str(old_some.iloc[[0]].filter(like="_period_has").to_dict("records")),
)
check("пустой набор — не «нет данных»", mc.metric_known(pd.DataFrame(), "reach") is True)
mixed = mc.prepare_dashboard_messages(
    pd.concat([frame("a", views=[10, 20]), frame("b", views=[0, 0], declared="audience|engagement")], ignore_index=True)
)
check("смесь периодов: охват есть в сумме", mc.metric_known(mixed, "reach") is True)
check("смесь периодов: помечено, что охват есть не везде", mc.metric_partly_known(mixed, "reach") is True)
check("вовлечённость есть везде — не помечена", mc.metric_partly_known(mixed, "engagement") is False)

print("3. overview_metrics и тексты")
metrics_zero = mc.overview_metrics(new_zero)
check("known в словаре метрик", metrics_zero["known"] == {"audience": True, "reach": False, "engagement": True}, str(metrics_zero["known"]))
check("охват — прочерк", mc.metric_text(metrics_zero, "reach") == "—")
check("вовлечённость — законный «0»", mc.metric_text(metrics_zero, "engagement") == "0")
check("старый словарь без known — число, как раньше", mc.metric_text({"reach": 5}, "reach") == "5")
metrics_full = mc.overview_metrics(old_some)
check("сравнивать можно, только если метрика есть в обоих", not mc.metrics_comparable(metrics_zero, metrics_full, "reach"))
check("сообщения сравниваются всегда", mc.metrics_comparable(metrics_zero, metrics_full, "messages"))

print("4. «Динамика»: таблица и графики")
from services.period_comparison import comparison_row, comparison_visual_rows  # noqa: E402

row = comparison_row({**metrics_zero, "label": "Май"}, {**metrics_full, "label": "Апрель"})
check("Динамика: охват — прочерк", row["Охват"] == "—", str(row))
check("Динамика: Δ охвата — прочерк, не −100 %", row["Δ охвата"] == "—", str(row))
check("Динамика: Δ вовлечённости считается (есть в обоих)", row["Δ вовлеченности"] not in ("—",), str(row))
visual = comparison_visual_rows([{**metrics_full, "label": "Апрель"}, {**metrics_zero, "label": "Май"}])
check(
    "на графике точка без охвата помечена",
    visual["Охват в выгрузке"].tolist() == [True, False],
    str(visual.filter(like="в выгрузке").to_dict("records")),
)

print("5. Клиентский обзор, карточка ИИ, саммари")
from client_insights_ui import build_period_change_insights, build_tag_change_table  # noqa: E402
from services.ai_summary import comparison_block, metrics_block  # noqa: E402

periods = pd.DataFrame(
    [
        {"period_id": "a", "period_name": "Апрель", "date_from": "2026-04-01", "date_to": "2026-04-07"},
        {"period_id": "b", "period_name": "Май", "date_from": "2026-05-01", "date_to": "2026-05-07"},
    ]
)
tagged = mixed.assign(tags="Тарифы", text="сообщение")
insights = " ".join(build_period_change_insights(tagged, periods, ["a", "b"]))
check("клиентский обзор: нет «снизилось» по охвату", "охвата" not in insights, insights)
tag_changes = build_tag_change_table(tagged, periods, ["a", "b"])
check(
    "изменения тегов: охват сейчас и Δ охвата — прочерк",
    not tag_changes.empty and set(tag_changes["Охват сейчас"]) == {"—"} and set(tag_changes["Δ охвата"]) == {"—"},
    str(tag_changes.to_dict("records")),
)
card = metrics_block(new_zero, metrics_zero)
check("карточка ИИ: охват — «нет данных», а не 0", "Суммарный охват: нет данных" in card, card)
check("карточка ИИ: вовлечённость — законный 0", "Суммарная вовлечённость: 0" in card, card)
dynamics = comparison_block(
    {"comparison": {"previous": {**metrics_full, "label": "Апрель"}, "current": {**metrics_zero, "label": "Май"}}}
)
check("карточка ИИ: нет «было …, стало 0» по охвату", "охват" not in dynamics, dynamics)

print("6. Отчёт: Word, PDF, PNG")
from docx import Document  # noqa: E402

from services import report_export  # noqa: E402

payload = report_export.summary_export_payload(
    "Проект", "Май", "Текст.", metrics_zero, messages=new_zero, events_agg=pd.DataFrame()
)
check("payload несёт признак", payload["known"]["reach"] is False and payload["known"]["engagement"] is True)
docx_text = "\n".join(p.text for p in Document(BytesIO(report_export.generate_summary_docx(payload))).paragraphs)
check("Word: охват — нет в выгрузке", "охват — нет в выгрузке" in docx_text, docx_text[:400])
check("Word: вовлечённость — законный 0", "вовлеченность — 0" in docx_text, docx_text[:400])
pdf_cards, _ = report_export._pdf_metric_cards(payload)
check("PDF: карточка охвата — прочерк", dict((t, v) for t, v, _s in pdf_cards)["Охват"] == "—", str(pdf_cards))
check("PNG собирается с прочерком", len(report_export.generate_summary_infographic_png(payload)) > 1000)

print("7. Загрузка: признак записывается и доезжает до экрана")
from services.dashboard_data import prepare_period_data  # noqa: E402
from services.ingest import ingest_file_bytes  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()
PROJECT = "metrics"
CLIENT.db["platform_projects"] = [
    {"project_id": PROJECT, "project_name": "Метрики", "status": "active", "settings": {},
     "created_at": NOW, "updated_at": NOW}
]
CLIENT.db["platform_periods"] = []
CLIENT.db["platform_table_rows"] = []
CLIENT.db["platform_manual_edits"] = []
WORK_DIR = tempfile.mkdtemp(prefix="metric_availability_")


def export(day, *, views):
    rows = []
    for i in range(12):
        row = {
            "Время публикации": f"{day + i % 5:02d}.04.2026 10:{i:02d}",
            "Кто пишет": f"Автор {i}",
            "Где пишет": ["vk.com/a", "vk.com/b", "t.me/c"][i % 3],
            "Текст": f"Сообщение номер {i} про новый тариф и водителей",
            "Ссылка": f"https://example.com/{day}/{i}",
            "Тональность": ["Негативная", "Нейтральная"][i % 2],
            "Аудитория блога": str(1000 + i),
            "Вовлеченность": "0",
            "Теги": "Тарифы",
        }
        if views:
            row["Просмотры"] = str(500 + i)
        rows.append(row)
    buf = BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


with_views = ingest_file_bytes(export(1, views=True), project_id=PROJECT, source_filename="april.xlsx",
                               source_system="auto", work_dir=WORK_DIR)
without_views = ingest_file_bytes(export(10, views=False), project_id=PROJECT, source_filename="may.xlsx",
                                  source_system="auto", work_dir=WORK_DIR)
views_again = ingest_file_bytes(export(20, views=True), project_id=PROJECT, source_filename="june.xlsx",
                                source_system="auto", work_dir=WORK_DIR)
_events, loaded, _state = prepare_period_data(PROJECT, [without_views["period_id"]])
loaded_metrics = mc.overview_metrics(loaded)
check("после загрузки без просмотров: охват — прочерк", mc.metric_text(loaded_metrics, "reach") == "—")
check(
    "вовлечённость из нулей после загрузки — законный ноль",
    mc.metric_text(loaded_metrics, "engagement") == "0",
    str(loaded_metrics.get("known")),
)
manifests = [p.get("manifest", {}).get("metrics_in_source") for p in CLIENT.db["platform_periods"]]
check("в манифесте периода записано, что было", ["audience", "engagement"] in manifests, str(manifests))

print("8. Сквозной прогон приложения")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APRIL, MAY, JUNE = (str(r["period_id"]) for r in (with_views, without_views, views_again))


def open_app(section, period_ids):
    st.cache_data.clear()
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
    at.session_state["platform_project_id"] = PROJECT
    at.session_state["platform_project_role"] = "viewer"
    at.session_state["platform_nav_page"] = section
    at.session_state[f"period_select_{PROJECT}"] = period_ids
    at.run()
    return at


def page_text(at):
    out = []
    for kind in ("markdown", "caption", "info", "warning", "subheader"):
        out += [str(el.value) for el in getattr(at, kind)]
    for frame_el in at.dataframe:
        value = frame_el.value
        out.append(" | ".join(map(str, value.columns)))
        out += [" | ".join(r) for r in value.astype(str).values.tolist()]
    return "\n".join(out)


def metric_cards(at):
    return {str(m.label): (str(m.value), m.delta) for m in at.metric}


at = open_app("Обзор", [MAY])
check("Обзор: открылся без исключений", not at.exception, str(at.exception))
cards = metric_cards(at)
check("Обзор: охват — прочерк", cards.get("Охват", ("",))[0] == "—", str(cards))
check("Обзор: вовлечённость — законный 0", cards.get("Вовлеченность", ("",))[0] == "0", str(cards))
check("Обзор: у прочерка нет стрелки изменения", cards.get("Охват", ("", "x"))[1] in (None, ""), str(cards))

at = open_app("Обзор", [APRIL])
cards = metric_cards(at)
check("Обзор: в апреле охват есть", cards.get("Охват", ("—",))[0] not in ("—", "0"), str(cards))

at = open_app("Обзор", [JUNE])
cards = metric_cards(at)
check("Обзор: охват снова есть", cards.get("Охват", ("—",))[0] not in ("—", "0"), str(cards))
check(
    "Обзор: к прошлому периоду без охвата изменения нет",
    not cards.get("Охват", ("", "x"))[1] and bool(cards.get("Сообщений", ("", ""))[1]),
    str(cards),
)
check(
    "Обзор: сказано, почему изменения нет",
    any("В прошлом периоде нет колонок: охват" in str(c.value) for c in at.caption),
    str([str(c.value) for c in at.caption])[:400],
)

at = open_app("Обзор", [APRIL, MAY])
cards = metric_cards(at)
check("Обзор за два периода: охват — сумма по апрелю", cards.get("Охват", ("—",))[0] not in ("—", "0"), str(cards))
check(
    "Обзор за два периода: сказано, что охват есть не везде",
    any("В части выбранных периодов нет колонок: охват" in str(c.value) for c in at.caption),
    str([str(c.value) for c in at.caption])[:400],
)

at = open_app("Сообщения", [MAY])
text = page_text(at)
check("Сообщения: «охват: —» в карточке", "охват: —" in text and "охват: 0" not in text, text[:500])
check("строка метрик: «охват —»", "охват —" in text, text[:500])

at = open_app("Теги", [MAY])
tag_frames = [el.value for el in at.dataframe if "Охват" in getattr(el.value, "columns", [])]
check(
    "Теги: охват у тегов — прочерк",
    bool(tag_frames) and set(tag_frames[0]["Охват"].astype(str)) == {"—"},
    str(tag_frames[0].to_dict("records")[:2]) if tag_frames else "таблицы нет",
)

at = open_app("Динамика", [APRIL, MAY])
check("Динамика: открылась без исключений", not at.exception, str(at.exception))
text = page_text(at)
check("Динамика: нигде нет −100 %", "-100%" not in text and "−100%" not in text, text[:600])
cards = metric_cards(at)
check("Динамика: карточка охвата — прочерк без стрелки", cards.get("Охват", ("", "x"))[0] == "—" and not cards["Охват"][1], str(cards))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Прочерк вместо ложного нуля — везде, где метрики нет в выгрузке.")
