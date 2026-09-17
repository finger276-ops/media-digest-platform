# -*- coding: utf-8 -*-
"""Аналитика по уровням тегов: own/subtree агрегаты и переработанный UI.

Модуль services (compute_tier_aggregates) не имел ни одного теста, хотя
считает единственную нетривиальную вещь в разделе — поддерево без двойного
счёта (сообщение с двумя дочерними тегами одной ветки считается в поддереве
ОДИН раз, а не дважды). Заодно проверяется переработка UI: раньше блок
дублировал числа — bar_chart и таблица под ним показывали одно и то же,
что аналитик и заметил на скриншоте живой платформы («нагромождено, не даёт
практического знания»). Теперь доля встроена в таблицу как ProgressColumn,
отдельного графика нет вовсе.

Второй заход (после показа в песочнице, scripts/run_sandbox.py): аналитику
не понравился и способ навигации — «Тир 1» сверху, потом выбор узла из
списка с подписью «(Тир 2)» и таблица только по одной ветке за раз. Термин
«тир» ничего не говорит о смысле. Заменено на одну таблицу-дерево, глубина —
отступом и значком связи.

Третий заход (там же, в песочнице): отступы и значок связи тоже не
понравились — попросили «1 окно, которое раскрывается, и там уже выбрать
ветку». Итог: одна свёрнутая по умолчанию раскрывашка (st.expander), внутри
— таблица верхнего уровня и выпадающий список «Показать состав ветки:» с
плоскими именами тегов (без «(Тир N)», без значков), список включает ЛЮБОЙ
узел с детьми, а не только верхний уровень — проверено вживую в песочнице на
«Доставка» (тир 2, у неё свой ребёнок «Гарантия» тира 3) и здесь же тестом
ниже.

Мутационные проверки (что ломает какой тест):
- в compute_tier_aggregates убрать пересечение own_key с subtree и считать
  subtree_keys только по потомкам (без самого узла) → «own входит в
  subtree» и точные числа «Продукция» краснеют;
- посчитать subtree суммой own по потомкам вместо пересечения множеств
  (снять дедупликацию) → «Продукция» получит 7 вместо 6 (сообщение с двумя
  тегами посчитано дважды) — ровно тот случай, который явно проверяется;
- в render_tier_analytics_block вернуть st.bar_chart → «графиков нет»
  краснеет;
- сузить список веток до узлов первого уровня (только верхний уровень) →
  «список веток включает не только верхний уровень» краснеет — листья
  (без детей) не должны попасть в список ни при каком уровне вложенности;
- убрать column_config ProgressColumn у «Доля от всех»/«Доля в ветке» →
  «доля — прогресс-бар, не голое число» краснеет.
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

import json  # noqa: E402

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

# Подменить клиент здесь, ДО импорта services.tag_hierarchy_store (её тянет
# tag_tier_analytics_ui): модуль делает `from platform_store import
# get_supabase_client` — это копирует ссылку на функцию в момент импорта, и
# более поздний store.get_supabase_client = ... на неё уже не подействует.
# Тот же порядок нужен и харнессу tag_tier_app.py ниже (см. AppTest в блоке 5) —
# оба используют один процесс и один кеш sys.modules.
CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services.tag_hierarchy import parse_tag_rows  # noqa: E402
from tag_tier_analytics_ui import compute_tier_aggregates  # noqa: E402

failures = []


def check(label, condition, detail=""):
    mark = "  ✓ " if condition else "  ✗ "
    print(mark + label + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


HIERARCHY = parse_tag_rows(
    [
        {"tag": "Продукция", "tier": 1, "parent": ""},
        {"tag": "Кнауф Норд", "tier": 2, "parent": "Продукция"},
        {"tag": "Тисма", "tier": 2, "parent": "Продукция"},
        {"tag": "Конкуренты", "tier": 1, "parent": ""},
        {"tag": "ТЕХНОНИКОЛЬ", "tier": 2, "parent": "Конкуренты"},
    ]
)


def messages() -> pd.DataFrame:
    rows = []
    for i in range(4):
        tags = "Кнауф Норд|Тисма" if i == 0 else "Кнауф Норд"
        rows.append({"message_id": f"m{i}", "tags": tags})
    for i in range(2):
        rows.append({"message_id": f"mt{i}", "tags": "Тисма"})
    for i in range(3):
        rows.append({"message_id": f"mc{i}", "tags": "ТЕХНОНИКОЛЬ"})
    rows.append({"message_id": "mx", "tags": "Погода"})
    return pd.DataFrame(rows)


def row(table: pd.DataFrame, tag: str) -> dict:
    match = table[table["Тег"] == tag]
    return match.iloc[0].to_dict() if not match.empty else {}


print("1. own/subtree: поддерево без двойного счёта")
table, coverage = compute_tier_aggregates(HIERARCHY, messages())
check("Кнауф Норд: own=4, subtree=4 (нет детей)", row(table, "Кнауф Норд")["Сообщений (сам тег)"] == 4 and row(table, "Кнауф Норд")["Сообщений (с потомками)"] == 4)
check("Тисма: own=3, subtree=3", row(table, "Тисма")["Сообщений (сам тег)"] == 3 and row(table, "Тисма")["Сообщений (с потомками)"] == 3)
check(
    "Продукция: own=0 (нет сообщений с тегом-родителем буквально)",
    row(table, "Продукция")["Сообщений (сам тег)"] == 0,
)
check(
    "Продукция: subtree=6 - сообщение с двумя дочерними тегами посчитано ОДИН раз, не 4+3=7",
    row(table, "Продукция")["Сообщений (с потомками)"] == 6,
    str(row(table, "Продукция")),
)
check("Конкуренты: subtree=3", row(table, "Конкуренты")["Сообщений (с потомками)"] == 3)
check("ТЕХНОНИКОЛЬ: own=subtree=3", row(table, "ТЕХНОНИКОЛЬ")["Сообщений (сам тег)"] == 3 and row(table, "ТЕХНОНИКОЛЬ")["Сообщений (с потомками)"] == 3)

print("2. Доля от всех - процент от общего числа сообщений (10)")
check("Продукция: 60.0%", row(table, "Продукция")["Доля от всех"] == 60.0, str(row(table, "Продукция")))
check("Конкуренты: 30.0%", row(table, "Конкуренты")["Доля от всех"] == 30.0)
check("Кнауф Норд: 40.0%", row(table, "Кнауф Норд")["Доля от всех"] == 40.0)

print("3. Покрытие структурой: 9 из 10, тег вне структуры")
check("total=10", coverage["total"] == 10, str(coverage))
check("covered=9 (всё, кроме постороннего тега)", coverage["covered"] == 9, str(coverage))
check("covered_pct=90.0", coverage["covered_pct"] == 90.0, str(coverage))
check(
    "посторонний тег - единственный вне структуры, встречается 1 раз",
    len(coverage["outside_tags"]) == 1 and coverage["outside_tags"][0][1] == 1,
    str(coverage["outside_tags"]),
)

print("4. Пустые сообщения / иерархия без совпадений не падают")
empty_table, empty_coverage = compute_tier_aggregates(HIERARCHY, pd.DataFrame())
check("пустые сообщения -> total=0, covered_pct=0.0, без деления на ноль", empty_coverage["total"] == 0 and empty_coverage["covered_pct"] == 0.0)
no_tags_df = pd.DataFrame([{"message_id": "z1", "tags": ""}])
_, no_match_coverage = compute_tier_aggregates(HIERARCHY, no_tags_df)
check("сообщение без тегов не попадает ни в покрытие, ни в outside_tags", no_match_coverage["covered"] == 0 and not no_match_coverage["outside_tags"])

print("5. UI: блок отрисован через изолированный харнесс (tag_tier_app.py)")
from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(REPO / "tests" / "tag_tier_app.py"), default_timeout=90)
at.run()
check("харнесс отрисовался без исключений", not at.exception, str(at.exception))

check(
    "графиков (bar_chart/altair) в блоке больше нет - раньше дублировали таблицу",
    len(at.get("vega_lite_chart")) == 0,
    str(len(at.get("vega_lite_chart"))),
)

check(
    "вся структура - одна свёрнутая по умолчанию раскрывашка «Структура тегов»",
    any(str(e.label) == "Структура тегов" and not e.proto.expanded for e in at.expander),
    str([(str(e.label), e.proto.expanded) for e in at.expander]),
)

# Раскрывашка свёрнута визуально, но код внутри неё всё равно исполняется -
# AppTest видит элементы независимо от expanded=False, поэтому дальше можно
# проверять содержимое как обычно.
dataframes = list(at.dataframe)
check("хотя бы одна таблица отрисована", bool(dataframes))

progress_columns = []
for el in dataframes:
    try:
        cfg = json.loads(el.proto.columns or "{}")
    except (json.JSONDecodeError, TypeError):
        cfg = {}
    for col_name, col_cfg in cfg.items():
        if col_cfg.get("type_config", {}).get("type") == "progress":
            progress_columns.append(col_name)
check(
    "доля показана прогресс-индикатором внутри таблицы, а не отдельным числом/графиком",
    "Доля от всех" in progress_columns,
    str(progress_columns),
)

top_frame = None
for el in dataframes:
    value = el.value
    if "Тег" in getattr(value, "columns", []) and "Доля от всех" in getattr(value, "columns", []):
        top_frame = value
        break
check(
    "таблица верхнего уровня содержит все три корня (Продукция, Конкуренты, Форматы), без листьев",
    top_frame is not None and set(top_frame["Тег"]) == {"Продукция", "Конкуренты", "Форматы"},
    str(top_frame["Тег"].tolist() if top_frame is not None else None),
)
check(
    "в таблице верхнего уровня нет значка связи - только имя тега",
    top_frame is None or not top_frame["Тег"].astype(str).str.contains("↳").any(),
    str(top_frame["Тег"].tolist() if top_frame is not None else None),
)
if top_frame is not None:
    prod_row = top_frame[top_frame["Тег"] == "Продукция"].iloc[0]
    check(
        "Продукция в верхнем уровне подтверждает subtree=6 из 12 сообщений (доля 50.0)",
        int(prod_row["Сообщений (с потомками)"]) == 6 and float(prod_row["Доля от всех"]) == 50.0,
        str(prod_row.to_dict()),
    )

check(
    "нигде в тексте раздела не осталось слова «Тир» - с него и начались жалобы",
    not any("Тир" in str(m.value) for m in at.markdown)
    and not any("Тир" in str(c.value) for c in at.caption)
    and not any("Тир" in str(s.label) for s in at.selectbox)
    and (top_frame is None or not top_frame["Тег"].astype(str).str.contains("Тир").any()),
)

branch_select = [s for s in at.selectbox if str(s.label) == "Показать состав ветки:"]
check("селектор ветки найден", bool(branch_select))
if branch_select:
    options = [str(o) for o in branch_select[0].options]
    check(
        "в списке веток - все узлы с детьми, любого уровня (Продукция, Конкуренты, Форматы, Отзыв)",
        set(options) == {"Продукция", "Конкуренты", "Форматы", "Отзыв"},
        str(options),
    )
    check(
        "«Отзыв» - тир 2, не верхний уровень, но у него есть свой ребёнок «Развёрнутый» (тир 3) - он в списке наравне с тир-1",
        "Отзыв" in options,
        str(options),
    )
    check(
        "листья (без своих детей) в списке веток отсутствуют - им нечего показывать",
        not ({"Кнауф Норд", "Тисма", "ТЕХНОНИКОЛЬ", "Развёрнутый"} & set(options)),
        str(options),
    )
    check("список веток отсортирован по алфавиту", options == sorted(options), str(options))

    branch_select[0].set_value("Продукция").run()
    check("выбор ветки не роняет блок", not at.exception, str(at.exception))
    child_frame = None
    for el in at.dataframe:
        value = el.value
        if "Доля в ветке" in getattr(value, "columns", []):
            child_frame = value
            break
    check("таблица состава ветки «Продукция» отрисована", child_frame is not None)
    if child_frame is not None:
        check(
            "в составе ветки тоже нет значка связи - плоский список детей",
            not child_frame["Тег"].astype(str).str.contains("↳").any(),
            str(child_frame["Тег"].tolist()),
        )
        by_tag = {r["Тег"]: r for _, r in child_frame.iterrows()}
        check(
            "Кнауф Норд: 4 из 6 в ветке = 66.7%",
            "Кнауф Норд" in by_tag and float(by_tag["Кнауф Норд"]["Доля в ветке"]) == 66.7,
            str(by_tag.get("Кнауф Норд")),
        )
        check(
            "Тисма: 3 из 6 в ветке = 50.0%",
            "Тисма" in by_tag and float(by_tag["Тисма"]["Доля в ветке"]) == 50.0,
            str(by_tag.get("Тисма")),
        )

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Аналитика по уровням тегов работает корректно.")
