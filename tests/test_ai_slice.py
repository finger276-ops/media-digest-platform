# -*- coding: utf-8 -*-
"""Тексты ИИ по срезу: генерация, сохранение и саммари среза в живом приложении.

Провайдер настроен (YandexGPT), сеть подменена поддельным транспортом, Supabase —
поддельный клиент: проверяется то, что уходит в модель, и то, куда пишутся
тексты.

Мутационные проверки (что ломает какой тест):
- срез не передаётся в карточку данных -> «модель знает, что это срез»
  краснеет;
- черновик без среза в ключе -> «черновик среза не виден без среза»
  краснеет;
- сохранение текста ИИ без среза в ключе -> «текст ИИ сохранён под ключом
  среза» краснеет;
- «Сделать саммари среза» пишет в саммари периода -> «саммари среза из текста
  ИИ, саммари периода не тронуто» краснеет;
- комментарий к индексам бренда при срезе -> «при срезе нет комментария к
  индексам бренда» краснеет.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

for _key in list(os.environ):
    if _key.startswith(("AI_", "YANDEX_", "GIGACHAT_")):
        os.environ.pop(_key)
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"
os.environ["AI_PROVIDER"] = "yandex"
os.environ["YANDEX_API_KEY"] = "test-key"
os.environ["YANDEX_FOLDER_ID"] = "test-folder"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

PROJECT = "ai_slice"
PERIOD = "p1"
NOW = datetime.now(timezone.utc).isoformat()
CLIENT.db["platform_projects"] = [{"project_id": PROJECT, "project_name": "Кровля", "status": "active",
                                   "settings": {}, "created_at": NOW, "updated_at": NOW}]
CLIENT.db["platform_periods"] = [{"project_id": PROJECT, "period_id": PERIOD, "period_name": "Апрель",
                                  "date_from": "2026-04-01", "date_to": "2026-04-07", "source_filename": "f.xlsx",
                                  "status": "active", "manifest": {}, "uploaded_at": NOW}]
rows = []
for i in range(12):
    brand = "Технониколь" if i < 8 else "Кнауф"
    payload = {"message_id": f"m{i}", "period_id": PERIOD, "date": "02.04.2026", "datetime": f"2026-04-02T1{i % 10}:00:00",
               "sentiment": ["негатив", "нейтрал"][i % 2], "views": 100, "audience": 1000, "engagement": 5,
               "text_clean": f"Сообщение про {brand} №{i}", "platform": "vk.com", "chat_title": "Сообщество",
               "author": f"a{i}", "tags": brand, "event_title": "", "message_type": "Пост"}
    rows.append({"project_id": PROJECT, "period_id": PERIOD, "table_name": "messages", "row_id": payload["message_id"],
                 "payload": payload})
CLIENT.db["platform_table_rows"] = rows
store.save_manual(PROJECT, "summaries", f"summary::{PERIOD}", {"summary": "Саммари всего периода.", "period_ids": [PERIOD]})


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeProvider:
    def __init__(self):
        self.bodies = []

    def Session(self):  # noqa: N802 — повторяем имя из requests
        return self

    def post(self, url, **kwargs):
        self.bodies.append(json.dumps(kwargs.get("json") or {}, ensure_ascii=False))
        return FakeResponse({"result": {"alternatives": [{"message": {"text": "Текст ИИ про Технониколь."}}]}})


from services import ai_provider  # noqa: E402

PROVIDER = FakeProvider()
ai_provider._requests = lambda: PROVIDER

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from services.ai_summary import KIND_BRAND, KIND_SUMMARY, KIND_TITLES  # noqa: E402
from tag_slice_ui import slice_state_key  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def manual():
    return {row["row_key"]: row["payload"] for row in CLIENT.db["platform_manual_rows"]}


def button(at, label):
    return next((b for b in at.button if str(b.label) == label), None)


st.cache_data.clear()
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
at.session_state["platform_is_admin"] = True
at.session_state["platform_project_id"] = PROJECT
at.session_state["platform_nav_page"] = "Отчёт"
at.session_state[f"period_select_{PROJECT}"] = [PERIOD]
at.session_state[f"granularity_mode::{PROJECT}::{PERIOD}"] = "period"
at.session_state[slice_state_key(PROJECT)] = ["Технониколь"]
at.run()
check("Отчёт со срезом открылся", not at.exception, str(at.exception))
check("при срезе нет комментария к индексам бренда", button(at, KIND_TITLES[KIND_BRAND]) is None
      and button(at, KIND_TITLES[KIND_SUMMARY]) is not None, str([str(b.label) for b in at.button]))
button(at, KIND_TITLES[KIND_SUMMARY]).click().run()
body = PROVIDER.bodies[-1] if PROVIDER.bodies else ""
check("модель знает, что это срез", "Срез: только сообщения с тегами (тег: Технониколь)" in body, body[:300])
check("цифры в карточке — по срезу", "Сообщений: 8" in body, body[:400])
check("черновик показан", button(at, "Сохранить") is not None, str([str(b.label) for b in at.button]))
button(at, "Сохранить").click().run()
check("текст ИИ сохранён под ключом среза",
      manual().get(f"ai_text::summary::{PERIOD}::tags=технониколь", {}).get("text") == "Текст ИИ про Технониколь.",
      str([k for k in manual() if k.startswith("ai_text")]))
check("текст ИИ периода не появился", f"ai_text::summary::{PERIOD}" not in manual())
button(at, "Сделать саммари среза").click().run()
check("саммари среза из текста ИИ, саммари периода не тронуто",
      manual().get(f"summary::{PERIOD}::tags=технониколь", {}).get("source") == "ai"
      and manual().get(f"summary::{PERIOD}", {}).get("summary") == "Саммари всего периода.",
      str({k: v.get("summary") for k, v in manual().items() if k.startswith("summary")}))

# Та же вкладка, срез сброшен: у периода черновика нет, текст среза не подставляется.
button(at, KIND_TITLES[KIND_SUMMARY]).click().run()
at.session_state[slice_state_key(PROJECT)] = []
at.run()
fields = [str(t.value) for t in at.text_area if str(t.label) == "Текст"]
check("черновик среза не виден без среза", "Текст ИИ про Технониколь." not in fields, str(fields))
check("без среза — саммари периода", "Саммари всего периода." in " ".join(str(m.value) for m in at.markdown))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Тексты ИИ по срезу пишутся и хранятся отдельно от текстов периода.")
