# -*- coding: utf-8 -*-
"""Шапка «Обзора»: сравнение с прошлым периодом скрывается, когда оно нечестно.

«Прошлый период» — это ровно ОДИН период перед самым ранним из выбранных
(previous_period_id). Карточки шапки при этом показывают сумму по ВСЕМ
выбранным периодам. Если выбрано 2+ периода, сумма против одного целого
прошлого периода — не сравнение, а фокус: два периода по 3 сообщения против
одного такого же дадут «+3 (+100%)» на ровном месте, хотя ничего не выросло.

Проверено на живом прогоне streamlit_app.py (не только по коду): три периода
подряд, P0 — до выбранных. Выбор одного P1 сравнивает честно (P0 — реальный
прошлый период). Выбор [P1, P2] вместе — ложное сравнение, которое теперь
скрыто, а под карточками сказано, почему.

Мутационные проверки (app.py):
- убрать `len(selected_period_ids) >= 2` из `comparable_previous` -> падают
  «при двух периодах дельты не показаны» и «подпись объясняет причину»;
- убрать `not granularity_narrowed` из `comparable_previous` -> падает «часть
  дней одного периода — дельты нет» (шапка снова показывала ложное падение);
- не передавать granularity_narrowed в render_client_insights -> падает
  «Клиентский обзор при сужении не выдумывает рост»;
- показывать подпись без проверки prev_id -> падает «у первого периода
  проекта подписи о причине нет».
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from datetime import datetime, timezone

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

now = datetime.now(timezone.utc).isoformat()

CLIENT.db["platform_projects"] = [
    {
        "project_id": "ov_project",
        "project_name": "ПрошлыйПериод",
        "status": "active",
        "settings": {},
        "created_at": now,
        "updated_at": now,
    }
]

# Три недели подряд: P0 до выбранных, P1 и P2 — то, что выбирает аналитик.
PERIODS = [
    ("p0", "01.09.2026–07.09.2026", "2026-09-01", "2026-09-07"),
    ("p1", "08.09.2026–14.09.2026", "2026-09-08", "2026-09-14"),
    ("p2", "15.09.2026–21.09.2026", "2026-09-15", "2026-09-21"),
]
CLIENT.db["platform_periods"] = [
    {
        "project_id": "ov_project",
        "period_id": pid,
        "period_name": name,
        "date_from": date_from,
        "date_to": date_to,
        "source_filename": f"{pid}.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    }
    for pid, name, date_from, date_to in PERIODS
]

# P0 — 2 сообщения, P1 — 3, P2 — 5: сумма P1+P2 (8) против одного P0 (2) дала
# бы «+6 (+300%)», которых на самом деле не было — ни один период не рос.
_COUNTS = {"p0": 2, "p1": 3, "p2": 5}
_DATE = {"p0": "03.09.2026", "p1": "10.09.2026", "p2": "17.09.2026"}
_DATETIME = {"p0": "2026-09-03T10:00:00", "p1": "2026-09-10T10:00:00", "p2": "2026-09-17T10:00:00"}


def _message_row(period_id, index):
    date, datetime_value = _DATE[period_id], _DATETIME[period_id]
    # У P1 последнее сообщение — на следующий день: без второго дня внутри
    # периода гранулярностью нечего сужать.
    if period_id == "p1" and index == _COUNTS["p1"] - 1:
        date, datetime_value = "11.09.2026", "2026-09-11T10:00:00"
    payload = {
        "message_id": f"{period_id}_m{index}",
        "period_id": period_id,
        "date": date,
        "datetime": datetime_value,
        "sentiment": "нейтрал",
        "views": 1000,
        "audience": 500,
        "engagement": 10,
        "likes": 10,
        "comments": 0,
        "reposts": 0,
        "text_clean": f"Сообщение {period_id} №{index}",
        "message_link": f"https://example.com/{period_id}_{index}",
        "platform": "vk.com",
        "author": f"user_{period_id}_{index}",
        "tags": "Тема",
        "event_title": "Тема",
    }
    return {
        "project_id": "ov_project",
        "period_id": period_id,
        "table_name": "messages",
        "row_id": payload["message_id"],
        "payload": payload,
    }


CLIENT.db["platform_table_rows"] = [
    _message_row(pid, i) for pid, count in _COUNTS.items() for i in range(count)
]

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail != "" and not condition else ""))
    if not condition:
        failures.append(label)


at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
at.session_state["platform_is_admin"] = True
at.run()
check("приложение стартовало без исключений", not at.exception, str(at.exception))


def _metric(label):
    for m in at.metric:
        if str(m.label) == label:
            return m
    return None


def _captions():
    return [str(c.value) for c in at.caption]


period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
check("выбор периодов найден в боковой панели", bool(period_multiselect), str([m.label for m in at.sidebar.multiselect]))

if period_multiselect:
    print("1. Один период (P1) — есть настоящий прошлый период (P0), сравнение честное")
    period_multiselect[0].set_value(["p1"]).run()
    check("выбор одного периода не роняет страницу", not at.exception, str(at.exception))
    messages_card = _metric("Сообщений")
    check("карточка «Сообщений» найдена", messages_card is not None)
    if messages_card is not None:
        check(
            "при одном периоде дельта к P0 показана (3 против 2 — рост)",
            bool(messages_card.delta),
            repr(messages_card.delta),
        )
    check(
        "подпись называет реальный прошлый период",
        any("01.09.2026–07.09.2026" in c for c in _captions()),
        str([c for c in _captions() if "предыдущ" in c]),
    )

    print("2. Два периода вместе (P1+P2) — сравнение с одним P0 нечестно, оно скрыто")
    period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
    period_multiselect[0].set_value(["p1", "p2"]).run()
    check("выбор двух периодов не роняет страницу", not at.exception, str(at.exception))
    messages_card = _metric("Сообщений")
    check(
        "сумма по двум периодам верна (3+5=8), это не баг суммирования",
        messages_card is not None and str(messages_card.value) == "8",
        str(messages_card.value if messages_card else None),
    )
    check(
        "при двух периодах дельты не показаны ни у одной карточки шапки",
        messages_card is not None and not messages_card.delta,
        repr(messages_card.delta if messages_card else None),
    )
    check(
        "ложных «+6 (+300%)» на экране нет",
        not any("+6" in str(c) for c in _captions()) and (messages_card is None or "300" not in str(messages_card.delta)),
    )
    check(
        "подпись объясняет причину — выбрано несколько периодов",
        any("выбрано несколько периодов" in c for c in _captions()),
        str([c for c in _captions() if "предыдущ" in c]),
    )
    check(
        "старой подписи с конкретным периодом нет",
        not any("Изменения — к предыдущему периоду:" in c for c in _captions()),
        str([c for c in _captions() if "предыдущ" in c]),
    )

    print("3. Возврат к одному периоду — сравнение снова на месте")
    period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
    period_multiselect[0].set_value(["p2"]).run()
    check("возврат к одному периоду не роняет страницу", not at.exception, str(at.exception))
    check(
        "подпись снова называет конкретный прошлый период",
        any("Изменения — к предыдущему периоду:" in c for c in _captions()),
        str([c for c in _captions() if "предыдущ" in c]),
    )


def _day_picker():
    return [ms for ms in at.multiselect if "Дни/недели/месяцы" in str(ms.label)]


def _texts():
    return " ".join(str(m.value) for m in at.markdown)


if period_multiselect:
    print("4. Один период, в гранулярности отмечена часть дней — сравнение скрыто")
    # Карточки считаются по отмеченным дням, а прошлый период — целиком: без
    # защиты шапка показывала ложное падение.
    period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
    period_multiselect[0].set_value(["p1"]).run()
    picker = _day_picker()
    check("пикер дней найден", bool(picker), str([m.label for m in at.multiselect]))
    if picker:
        picker[0].set_value(["2026-09-10"]).run()
        messages_card = _metric("Сообщений")
        check(
            "отмечен один день из двух — в карточке 2 сообщения",
            messages_card is not None and str(messages_card.value) == "2",
            str(messages_card.value if messages_card else None),
        )
        check(
            "часть дней одного периода — дельты нет",
            messages_card is not None and not messages_card.delta,
            repr(messages_card.delta if messages_card else None),
        )
        check(
            "подпись объясняет: отмечены не все дни",
            any("отмечены не все дни периода" in c for c in _captions()),
            str([c for c in _captions() if "не показано" in c]),
        )

    print("5. Два периода: подпись шапки не спорит с «Клиентским обзором»")
    period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
    period_multiselect[0].set_value(["p1", "p2"]).run()
    check(
        "шапка говорит о периоде ДО выбранных и отсылает к изменениям ниже",
        any("периоду до выбранных" in c and "Клиентском обзоре" in c for c in _captions()),
        str([c for c in _captions() if "не показано" in c]),
    )
    check(
        "а «Клиентский обзор» ниже показывает изменение между выбранными периодами",
        "Количество сообщений выросло" in _texts(),
        _texts()[:300],
    )
    picker = _day_picker()
    if picker:
        picker[0].set_value(["2026-09-17"]).run()
        check(
            "Клиентский обзор при сужении не выдумывает рост (день только из P2)",
            "Количество сообщений выросло" not in _texts()
            and any("отмечены не все дни периода" in c for c in _captions()),
            str([c for c in _captions() if "не показано" in c]) + " | " + _texts()[:200],
        )
        check(
            "подпись шапки не отсылает к изменениям ниже, которых теперь нет",
            not any("Клиентском обзоре" in c for c in _captions()),
            str([c for c in _captions() if "не показано" in c]),
        )

    print("6. Первый период проекта — сравнивать не с чем, подписи о причине нет")
    period_multiselect = [m for m in at.sidebar.multiselect if str(m.label) == "Периоды"]
    period_multiselect[0].set_value(["p0", "p1"]).run()
    check(
        "у первых периодов проекта подписи о причине нет (прошлого периода нет вовсе)",
        not any("до выбранных не показано" in c for c in _captions()),
        str([c for c in _captions() if "не показано" in c]),
    )

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Шапка «Обзора» не сравнивает несколько периодов с одним прошлым.")
