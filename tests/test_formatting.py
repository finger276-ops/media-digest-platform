# -*- coding: utf-8 -*-
"""Короткие даты без года и подпись периода в сайдбар-селекторе.

Аналитик заметил: пилюля селектора периодов показывала одну и ту же дату
дважды - "24.04.2026-30.04.2026 · 24.04.2026-30.04.2026" (автосгенерированное
имя периода И есть дата, а рядом ещё раз выводился fmt_period с той же
датой). Заодно попросил убрать год - он не несёт пользы внутри одного
проекта.

period_picker_label - единая точка, которая решает: автогенерированное имя
периода (только цифры/точки/дефисы - looks_like_date_range) показывается
один раз короткими датами; осмысленное имя аналитика показывается рядом с
датами, а не вместо них.

Мутационные проверки (что ломает какой тест):
- в looks_like_date_range убрать re.match и вернуть True всегда -> "Апрельская
  волна" потеряла бы имя в подписи, тест "осмысленное имя не теряется" красный;
- в period_picker_label убрать ветку looks_like_date_range (всегда name ·
  date_part) -> тест "дата не дублируется для автосгенерированного имени"
  красный (снова "24.04-30.04 · 24.04-30.04");
- в fmt_date_short не резать год (вернуть fmt_date как есть) -> тест "без
  года" красный;
- в fmt_date вернуть dayfirst=True для любых строк -> тест "ISO-дата с днём
  до 12 не превращается в другой месяц" красный (2026-04-05 -> 04.05.2026);
- в status_title / source_system_title вернуть код как есть -> тесты
  "статус по-русски" и "формат по-русски" красные.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import pandas as pd  # noqa: E402

from services.formatting import (  # noqa: E402
    fmt_date,
    fmt_date_short,
    fmt_period_short,
    looks_like_date_range,
    period_picker_label,
    source_system_title,
    status_title,
)

failures = []


def check(label, condition, detail=""):
    mark = "  ✓ " if condition else "  ✗ "
    print(mark + label + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. fmt_date_short / fmt_period_short: без года")
check("одиночная дата без года", fmt_date_short("2026-04-24") == "24.04", fmt_date_short("2026-04-24"))
check("пустое значение -> пустая строка, не падение", fmt_date_short(None) == "")
check("неразбираемая дата -> пустая строка", fmt_date_short("не дата") == "")
row = pd.Series({"date_from": "2026-04-24", "date_to": "2026-04-30"})
check("диапазон дат без года", fmt_period_short(row) == "24.04–30.04", fmt_period_short(row))
same_day = pd.Series({"date_from": "2026-04-24", "date_to": "2026-04-24"})
check("однодневный период - одна дата, не дублируется дефисом", fmt_period_short(same_day) == "24.04", fmt_period_short(same_day))

print("2. looks_like_date_range: отличает автоимя от осмысленного")
check("автосгенерированное имя (одна дата)", looks_like_date_range("24.04.2026"))
check("автосгенерированное имя (диапазон, тире)", looks_like_date_range("24.04.2026-30.04.2026"))
check("автосгенерированное имя (диапазон, en-dash)", looks_like_date_range("24.04.2026–30.04.2026"))
check("осмысленное имя - не похоже на дату", not looks_like_date_range("Апрельская волна"))
check("имя с датой И текстом - не считается чистой датой", not looks_like_date_range("Апрель (24.04.2026)"))
check("пустая строка - не дата", not looks_like_date_range(""))

print("3. period_picker_label: не дублирует дату, не теряет осмысленное имя")
auto_row = pd.Series(
    {
        "period_name": "24.04.2026–30.04.2026",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
    }
)
check(
    "дата не дублируется для автосгенерированного имени",
    period_picker_label(auto_row) == "24.04–30.04",
    period_picker_label(auto_row),
)
named_row = pd.Series(
    {
        "period_name": "Апрельская волна",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
    }
)
check(
    "осмысленное имя не теряется - показано рядом с короткими датами",
    period_picker_label(named_row) == "Апрельская волна · 24.04–30.04",
    period_picker_label(named_row),
)
no_name_row = pd.Series({"period_name": "", "date_from": "2026-04-24", "date_to": "2026-04-30"})
check(
    "без имени и без дат-в-имени - просто короткие даты",
    period_picker_label(no_name_row, fallback="p1") == "24.04–30.04",
    period_picker_label(no_name_row, fallback="p1"),
)
empty_row = pd.Series({"period_name": "", "date_from": None, "date_to": None})
check(
    "совсем без данных - запасное значение (period_id), не пустая подпись",
    period_picker_label(empty_row, fallback="p1") == "p1",
    period_picker_label(empty_row, fallback="p1"),
)

print("4. fmt_date: ISO-дата из базы не меняет день с месяцем местами")
# pandas 3 с dayfirst=True читает «2026-04-05» как 4 мая. Дни до 12 — ровно
# те, на которых это незаметно в тестах с 24-м и 30-м числом.
for raw in ("2026-04-05", "2026-04-05T10:11:12+00:00", "2026-04-05 10:11:12"):
    check(
        f"ISO-дата с днём до 12 не превращается в другой месяц: {raw}",
        fmt_date(raw) == "05.04.2026",
        fmt_date(raw),
    )
check("русская дата читается днём вперёд", fmt_date("05.04.2026") == "05.04.2026", fmt_date("05.04.2026"))
check(
    "Timestamp как был",
    fmt_date(pd.Timestamp("2026-04-05")) == "05.04.2026",
    fmt_date(pd.Timestamp("2026-04-05")),
)
check(
    "короткая дата из ISO тоже верная",
    fmt_date_short("2026-04-05") == "05.04",
    fmt_date_short("2026-04-05"),
)

print("5. Внутренние коды платформы показываются по-русски")
check("статус по-русски: active", status_title("active") == "Активен", status_title("active"))
check("статус по-русски: hidden", status_title("hidden") == "Скрыт", status_title("hidden"))
check("статус по-русски: archived", status_title("archived") == "В архиве", status_title("archived"))
check("пустой статус — активен, как и в базе", status_title(None) == "Активен", status_title(None))
check(
    "формат по-русски: mediologia_excel",
    source_system_title("mediologia_excel") == "Медиалогия Excel",
    source_system_title("mediologia_excel"),
)
check(
    "формат по-русски: brand_analytics",
    source_system_title("brand_analytics") == "Brand Analytics",
    source_system_title("brand_analytics"),
)
check(
    "незнакомый код не теряется",
    source_system_title("new_system") == "new_system",
    source_system_title("new_system"),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Короткие даты и подпись периода работают корректно.")
