"""Индексы бренда по периодам вне выбранных: динамика и изменение к прошлому.

Дашборд готовит сообщения выбранных периодов (инфоповоды, ручные правки,
служебные колонки), а динамика «по всем периодам» и изменение к прошлому
периоду раньше брали остальные периоды сырыми. Из-за этого одинаковые по
данным периоды давали разные значения: у выбранного SES 25 %, у догруженного —
прочерк и NSS −100 %, а скрытое аналитиком сообщение продолжало считаться.

Supabase подменён поддельным клиентом, тест не ходит в сеть.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from brand_metrics_ui import previous_period_metrics  # noqa: E402
from services.brand_metrics import merge_settings  # noqa: E402
from services.dashboard_data import prepare_period_messages  # noqa: E402

PROJECT_ID = "proj-dyn"

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def close(actual, expected, tolerance=0.05):
    try:
        return abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def message_row(period_id, index, sentiment):
    payload = {
        "message_id": f"{period_id}_m{index}",
        "period_id": period_id,
        "datetime": "2026-04-01T10:00:00",
        "sentiment": sentiment,
        "views": 1000,
        "audience": 5000,
        "engagement": 10,
        "likes": 10,
        "comments": 0,
        "reposts": 0,
        "author": f"user{index}",
        "chat_profile": f"https://vk.com/{index}",
        "event_title": f"Тема {index}",
    }
    return {
        "project_id": PROJECT_ID,
        "period_id": period_id,
        "table_name": "messages",
        "row_id": payload["message_id"],
        "payload": payload,
    }


# Два периода с одинаковыми сообщениями: два позитивных, негативное и
# нейтральное. Во втором аналитик скрыл негативное сообщение.
SENTIMENTS = ["позитивная", "позитивная", "негативная", "нейтральная"]
CLIENT.db["platform_table_rows"] = [
    message_row(period_id, i, sentiment)
    for period_id in ("p1", "p2")
    for i, sentiment in enumerate(SENTIMENTS)
]
CLIENT.db["platform_manual_rows"] = [
    {
        "project_id": PROJECT_ID,
        "table_name": "message_hidden",
        "row_key": "message_hidden::p2_m2",
        "payload": {"message_id": "p2_m2"},
        "updated_at": "2026-04-10T10:00:00+00:00",
    }
]

print("Периоды вне выбранных готовятся так же, как выбранные")
prepared = prepare_period_messages(PROJECT_ID, ["p2"])
check("служебные колонки расчёта на месте", "_sentiment_lower" in prepared.columns, str(list(prepared.columns)))
check("скрытое аналитиком сообщение не считается", "p2_m2" not in set(prepared["message_id"]),
      str(prepared["message_id"].tolist()))

print("Изменение к прошлому периоду учитывает ручные правки")
previous = previous_period_metrics(PROJECT_ID, "p2", merge_settings(None), {"own": [], "competitors": []})
# Без скрытого негатива: (2 − 0) / 3 = 66,67 %. Сырые данные дали бы 25 %.
check("ToneVolumeScore прошлого периода без скрытого сообщения", close(previous.get("ToneVolumeScore"), 66.67),
      str(previous))
check("SES прошлого периода без скрытого сообщения", close(previous.get("SES"), 66.67), str(previous))

print("Динамика по всем периодам проекта")
from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(REPO / "tests" / "brand_dynamics_app.py"), default_timeout=90)
at.run()
check("приложение без исключений", not at.exception, str(at.exception))
toggle = [box for box in at.checkbox if box.key == f"brand_metrics_chart_all_periods_{PROJECT_ID}"]
check("есть переключатель «Учесть все периоды»", bool(toggle))
if toggle:
    toggle[0].check().run()
    check("после переключения без исключений", not at.exception, str(at.exception))
    table = at.dataframe[0].value if at.dataframe else None
    check("таблица динамики построена", table is not None and len(table) == 2, str(table))
    if table is not None and len(table) == 2:
        rows = {str(row["Период"]): row for _, row in table.iterrows()}
        check("выбранный период: SES 25 %", close(rows["Неделя 1"]["SES"], 25.0), str(table.to_dict("records")))
        # Догруженный период раньше шёл сырым: SES выпадал в прочерк, NSS
        # уходил в −100 %, а скрытое сообщение считалось.
        check("догруженный период: SES посчитан с учётом правок", close(rows["Неделя 2"]["SES"], 66.67),
              str(table.to_dict("records")))
        # NSS по инфоповодам: у каждого сообщения своя тема. Выбранный период —
        # две позитивные темы и одна негативная из четырёх, догруженный — две
        # позитивные из трёх.
        check("выбранный период: NSS 25 %", close(rows["Неделя 1"]["NSS"], 25.0), str(table.to_dict("records")))
        check("догруженный период: NSS посчитан с учётом правок", close(rows["Неделя 2"]["NSS"], 66.67),
              str(table.to_dict("records")))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Все проверки индексов по периодам пройдены.")
