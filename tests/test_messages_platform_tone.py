# -*- coding: utf-8 -*-
"""«Сообщения»: фильтры площадки и тональности; переход из «Источников».

Мутационные проверки (что ломает какой тест):
- фильтр площадки не применяется -> «выбрана площадка — только её
  сообщения» краснеет;
- площадка сравнивается без алиасов (t.me ≠ telegram.org) -> «t.me — та же
  площадка telegram.org» краснеет;
- фильтр тональности не применяется -> «выбран негатив — только негатив»
  краснеет;
- тональность фильтра расходится со счётчиками (позитив важнее негатива) ->
  «грязная разметка: негатив важнее позитива» краснеет;
- фильтр тональности без проверки разметки -> «нет разметки — нет фильтра
  тональности» краснеет;
- числа площадок до фильтра тегов -> «число у площадки — с учётом тегов»
  краснеет;
- «Без площадки» не в конце -> «Без площадки — в конце списка» краснеет;
- переход не сбрасывает прочие фильтры -> «переход сбрасывает прочие
  фильтры ленты» краснеет;
- переход не открывает всю ленту -> «переход открывает всю ленту площадки»
  краснеет.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

import pandas as pd  # noqa: E402

from services.metrics_compute import sentiment_labels  # noqa: E402
from services.source_stats import filter_messages_by_platform, platform_options  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Площадки и тональность сообщений")
# «Без площадки» — у трёх сообщений, больше всех: в конце списка он не по числу.
sample = pd.DataFrame({
    "platform": ["vk.com", "t.me", "telegram.org", "", "VK.com", "", ""],
    "message_link": ["", "", "", "", "", "", ""],
    "sentiment": ["негатив", "позитив", "нейтрал", "", "позитивно-негативная", "", ""],
})
options = platform_options(sample)
# По два сообщения у vk.com и telegram.org: при равенстве — по алфавиту.
check("площадки — домены, самые частые первыми", [label for label, _ in options][:2] == ["telegram.org", "vk.com"]
      and dict(options).get("vk.com") == 2 and dict(options).get("telegram.org") == 2, str(options))
check("Без площадки — в конце списка", options[-1] == ("Без площадки", 3), str(options))
check("t.me — та же площадка telegram.org", filter_messages_by_platform(sample, ["telegram.org"]).index.tolist() == [1, 2])
check("пустой выбор площадок — все", len(filter_messages_by_platform(sample, [])) == len(sample))
check("тональность словом", sentiment_labels(sample).tolist()[:4] == ["Негатив", "Позитив", "Нейтрал", "Нейтрал"],
      str(sentiment_labels(sample).tolist()))
check("грязная разметка: негатив важнее позитива", sentiment_labels(sample).tolist()[4] == "Негатив")

print("2. Фильтры в «Сообщениях»")
from streamlit.testing.v1 import AppTest  # noqa: E402


def feed_app():
    import pandas as pd
    import streamlit as st

    import messages_ui

    if not getattr(messages_ui, "_tone_spy", False):
        original = messages_ui._render_excel_button

        def spy(export_set, **kwargs):
            st.session_state["exported"] = (len(export_set), list(kwargs["filters"]))
            original(export_set, **kwargs)

        messages_ui._render_excel_button = spy
        messages_ui._tone_spy = True
    variant = st.session_state.get("variant", "")
    rows = []
    for i in range(30):
        # vk.com — 0..11, t.me — 12..23, отзыв без площадки со ссылкой — 24..29.
        platform = "vk.com" if i < 12 else ("t.me" if i < 24 else "")
        rows.append({
            "message_id": f"m{i}", "platform": platform,
            "message_link": f"https://otzovik.com/r/{i}" if i >= 24 else f"https://example.com/{i}",
            "tags": "Технониколь" if i % 2 == 0 else "Кнауф",
            "sentiment": "" if variant == "no_tone" else ["негатив", "нейтрал", "позитив"][i % 3],
            "text_clean": f"Сообщение {i}", "datetime": f"2026-04-{1 + i % 28:02d}T10:00:00",
            "engagement": i, "views": 10, "audience": 100,
        })
    messages_ui.render_messages_block(pd.DataFrame(rows), project_id="p", project_name="Проект", period_label="Апрель")


def shown_ids(at):
    return sorted(int(str(m.value).split()[-1]) for m in at.markdown
                  if str(m.value).startswith("Сообщение ") and str(m.value).split()[-1].isdigit())


def select(at, label):
    return next((m for m in at.multiselect if str(m.label) == label), None)


at = AppTest.from_function(feed_app, default_timeout=60)
at.run()
check("раздел открылся", not at.exception, str(at.exception))
check("фильтры «Площадка» и «Тональность» на месте", select(at, "Площадка") is not None and select(at, "Тональность") is not None,
      str([str(m.label) for m in at.multiselect]))
check("в списке площадок — домены с числом", select(at, "Площадка").format_func("telegram.org") == "telegram.org · 12"
      and "otzovik.com · 6" in list(select(at, "Площадка").options), str(list(select(at, "Площадка").options)))
select(at, "Площадка").select("telegram.org").run()
check("выбрана площадка — только её сообщения", shown_ids(at) == list(range(12, 24)), str(shown_ids(at)))
check("подпись называет площадку", any("на площадке telegram.org" in str(c.value) for c in at.caption))
select(at, "Тональность").select("Негатив").run()
check("выбран негатив — только негатив", shown_ids(at) == [12, 15, 18, 21], str(shown_ids(at)))
check("отбор для Excel называет площадку и тональность",
      at.session_state["exported"] == (4, ["площадка: telegram.org", "тональность: негатив"]),
      str(at.session_state["exported"]))
select(at, "Тональность").set_value([]).run()
select(at, "Площадка").set_value([]).run()
select(at, "Теги").select("Технониколь").run()
check("число у площадки — с учётом тегов", select(at, "Площадка").format_func("telegram.org") == "telegram.org · 6",
      select(at, "Площадка").format_func("telegram.org"))

at = AppTest.from_function(feed_app, default_timeout=60)
at.session_state["variant"] = "no_tone"
at.run()
check("нет разметки — нет фильтра тональности", not at.exception and select(at, "Тональность") is None
      and select(at, "Площадка") is not None)

print("3. Переход из «Источников»")
from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT
NOW = datetime.now(timezone.utc).isoformat()
PROJECT = "jump"
CLIENT.db["platform_projects"] = [{"project_id": PROJECT, "project_name": "Переход", "status": "active", "settings": {},
                                   "created_at": NOW, "updated_at": NOW}]
CLIENT.db["platform_periods"] = [{"project_id": PROJECT, "period_id": "p1", "period_name": "Апрель",
                                  "date_from": "2026-04-01", "date_to": "2026-04-07", "source_filename": "f.xlsx",
                                  "status": "active", "manifest": {}, "uploaded_at": NOW}]
rows = []
for i in range(9):
    payload = {"message_id": f"m{i}", "period_id": "p1", "date": "2026-04-02", "datetime": f"2026-04-02T1{i}:00:00",
               "platform": "vk.com" if i < 6 else "t.me", "chat_title": "Сообщество", "author": f"a{i}",
               "text_clean": f"Сообщение {i}", "message_link": f"https://example.com/{i}", "sentiment": "нейтрал",
               "tags": "Кровля" if i % 2 else "Стены", "event_title": "", "views": 1, "audience": 1, "engagement": i}
    rows.append({"project_id": PROJECT, "period_id": "p1", "table_name": "messages", "row_id": payload["message_id"],
                 "payload": payload})
CLIENT.db["platform_table_rows"] = rows

import streamlit as st  # noqa: E402

st.cache_data.clear()
at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=180)
at.session_state["platform_project_id"] = PROJECT
at.session_state["platform_project_role"] = "viewer"
at.session_state["platform_nav_page"] = "Источники"
at.session_state[f"period_select_{PROJECT}"] = ["p1"]
# Хвост прошлого просмотра ленты: тег, который спрятал бы часть сообщений площадки.
at.session_state[f"messages_tag_filter_{PROJECT}"] = ["Кровля"]
at.session_state["sources_table"] = {"selection": {"rows": [0], "columns": []}}
at.run()
check("«Источники» открылись", not at.exception, str(at.exception))
jump = next((b for b in at.button if "в «Сообщениях»" in str(b.label)), None)
check("у выбранной площадки — кнопка перехода", jump is not None and "Все 6 сообщ." in str(jump.label),
      str([str(b.label) for b in at.button]))
jump.click().run()
check("переход открыл раздел «Сообщения»", not at.exception and at.session_state["platform_nav_page"] == "Сообщения",
      str(at.exception))
check("выбрана площадка vk.com", list(select(at, "Площадка").value) == ["vk.com"] if select(at, "Площадка") else False)
check("переход сбрасывает прочие фильтры ленты", list(select(at, "Теги").value) == [] if select(at, "Теги") else False)
mode = next((r for r in at.radio if str(r.label) == "Режим просмотра сообщений"), None)
check("переход открывает всю ленту площадки", mode is not None and mode.value == "Вся лента"
      and any("Найдено сообщений: 6." in str(c.value) for c in at.caption),
      str([str(c.value) for c in at.caption if "Найдено" in str(c.value)]))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Фильтры площадки и тональности и переход из «Источников» работают.")
