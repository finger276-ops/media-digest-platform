# -*- coding: utf-8 -*-
"""Раздел «Сообщения»: фильтры по тегам и типу сообщения, разбивка по типам.

Мутационные проверки (что ломает какой тест):
- filter_messages_by_tags сравнивает с учётом регистра -> «тег находится без
  учёта регистра и ё» краснеет;
- «со всеми тегами» работает как «с любым» -> «со всеми тегами — только
  сообщения с обоими» краснеет;
- tag_options без сортировки по частоте -> «самые частые теги первыми»
  краснеет;
- render_messages_block не применяет фильтр тегов -> «выбран тег — в ленте
  только его сообщения» краснеет;
- выбор тегов не сверяется с новой выборкой по ключу -> «тот же тег в другом
  написании остаётся выбранным» краснеет;
- разбивка по типам считается до фильтра тегов -> «типы — по отобранным
  сообщениям» краснеет;
- message_type_counts без «Тип не указан» в конце -> «Тип не указан — в
  конце» краснеет;
- фильтр типа не применяется к ленте -> «выбран тип — в ленте только его
  сообщения» краснеет;
- карточки считаются после фильтра типа -> «фильтр типа не меняет карточки»
  краснеет;
- список типов считается до фильтра тегов -> «число в списке типов — с учётом
  тегов» краснеет;
- выбор типов не сверяется с новой выборкой по ключу -> «тот же тип в другом
  написании остаётся выбранным» краснеет.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.message_kinds import filter_messages_by_type, message_type_counts  # noqa: E402
from services.tag_compute import filter_messages_by_tags, tag_options  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("1. Теги: варианты и отбор")
sample = pd.DataFrame(
    {
        "tags": ["Технониколь|Кровля", "ТехноНИКОЛЬ", "Кнауф|Кровля", "", None, "Технониколь"],
    }
)
options = tag_options(sample)
check("самые частые теги первыми", [label for label, _ in options] == ["Технониколь", "Кровля", "Кнауф"], str(options))
check("одно название в разном регистре — один тег", dict(options).get("Технониколь") == 3, str(options))
check("тег находится без учёта регистра и ё",
      filter_messages_by_tags(sample, ["технониколь"]).index.tolist() == [0, 1, 5])
check("с любым из тегов", filter_messages_by_tags(sample, ["Технониколь", "Кнауф"]).index.tolist() == [0, 1, 2, 5])
check("со всеми тегами — только сообщения с обоими",
      filter_messages_by_tags(sample, ["Технониколь", "Кровля"], match_all=True).index.tolist() == [0])
check("пустой выбор — вся выборка", len(filter_messages_by_tags(sample, [])) == len(sample))
check("нет колонки тегов — ничего не найдено", filter_messages_by_tags(pd.DataFrame({"x": [1]}), ["a"]).empty)
check("запятая — часть названия тега", [label for label, _ in tag_options(pd.DataFrame(
    {"tags": ["Проблемы, жалобы и негативный опыт"]}))] == ["Проблемы, жалобы и негативный опыт"])

print("2. Тип сообщения")
types = message_type_counts(pd.DataFrame({"message_type": ["Пост", "пост", "Комментарий", "", None, "Пост", "Репост"]}))
check("типы как в выгрузке, самые частые первыми", types[:3] == [("Пост", 3), ("Комментарий", 1), ("Репост", 1)], str(types))
check("Тип не указан — в конце", types[-1] == ("Тип не указан", 2), str(types))
check("выгрузка без типа — разбивки нет", message_type_counts(pd.DataFrame({"x": [1, 2]})) == [])
typed = pd.DataFrame({"message_type": ["Пост", "пост", "Комментарий", "", None, "Репост"]})
check("отбор по типу без учёта регистра", filter_messages_by_type(typed, ["ПОСТ"]).index.tolist() == [0, 1])
check("несколько типов", filter_messages_by_type(typed, ["Пост", "Репост"]).index.tolist() == [0, 1, 5])
check("«Тип не указан» — сообщения без типа", filter_messages_by_type(typed, ["Тип не указан"]).index.tolist() == [3, 4])
check("пустой выбор типов — вся выборка", len(filter_messages_by_type(typed, [])) == len(typed))

print("3. Раздел «Сообщения»")
from streamlit.testing.v1 import AppTest  # noqa: E402


def messages_app():
    import pandas as pd
    import streamlit as st

    from messages_ui import render_messages_block

    variant = st.session_state.get("variant", "full")
    rows = []
    for i in range(60):
        # 12 сообщений с Технониколь (4 из них ещё и с Кровлей), 48 без.
        if i < 8:
            tags, kind = "Технониколь", "Комментарий"
        elif i < 12:
            tags, kind = "Технониколь|Кровля", "Пост"
        elif i < 30:
            tags, kind = "Кровля", "Пост"
        else:
            tags, kind = "Кнауф", "Репост"
        if variant == "no_technonicol" and "Технониколь" in tags:
            continue
        if variant == "respelled":
            tags = tags.replace("Технониколь", "ТЕХНОНИКОЛЬ")
        row = {
            "message_id": f"m{i}", "tags": tags, "text_clean": f"Сообщение {i}",
            "datetime": f"2026-05-{1 + i % 28:02d}T10:00:00", "engagement": i, "views": 10, "audience": 100,
            "sentiment": "нейтральная", "message_link": f"https://vk.com/wall-1_{i}",
        }
        if variant == "types_upper":
            kind = kind.upper()
        if variant != "no_types":
            row["message_type"] = kind
        rows.append(row)
    render_messages_block(pd.DataFrame(rows), project_id="p")


def tag_captions(at):
    return [str(c.value) for c in at.caption if str(c.value).startswith("Теги: ")]


def cards(at):
    return {str(m.label): str(m.value) for m in at.metric}


def tag_select(at):
    return next((m for m in at.multiselect if str(m.label) == "Теги"), None)


def type_select(at):
    return next((m for m in at.multiselect if str(m.label) == "Тип сообщения"), None)


def shown_ids(at):
    """Номера показанных сообщений: текст карточки — «Сообщение N»."""
    return sorted(int(str(m.value).split()[-1]) for m in at.markdown
                  if str(m.value).startswith("Сообщение ") and str(m.value).split()[-1].isdigit())


at = AppTest.from_function(messages_app, default_timeout=60)
at.run()
check("раздел открылся", not at.exception, str(at.exception))
select = tag_select(at)
check("фильтр «Теги» на месте", select is not None, str([str(m.label) for m in at.multiselect]))
check("в списке — число сообщений тега", select is not None and select.format_func("Технониколь") == "Технониколь · 12",
      select.format_func("Технониколь") if select is not None else "")
check("без фильтра — все типы выборки", cards(at) == {"Репост": "30 · 50%", "Пост": "22 · 37%", "Комментарий": "8 · 13%"},
      str(cards(at)))

select.select("Технониколь").run()
captions = tag_captions(at)
check("выбран тег — в ленте только его сообщения", captions and all("Технониколь" in c for c in captions),
      str(captions[:5]))
check("в ключевых сообщениях — все 12 с тегом", len(captions) == 12, str(len(captions)))
check("подпись называет тег", any("с тегом «Технониколь»" in str(c.value) for c in at.caption))
check("типы — по отобранным сообщениям", cards(at) == {"Комментарий": "8 · 67%", "Пост": "4 · 33%"}, str(cards(at)))

tag_select(at).select("Кровля").run()
match = next((r for r in at.radio if str(r.label) == "Сообщения"), None)
check("два тега — выбор «любой / все»", match is not None)
check("с любым из тегов — 30 сообщений", "Пост" in cards(at) and cards(at)["Пост"].startswith("22 ")
      and cards(at)["Комментарий"].startswith("8 "), str(cards(at)))
match.set_value("Со всеми тегами сразу").run()
captions = tag_captions(at)
check("со всеми тегами — только сообщения с обоими", len(captions) == 4
      and all("Технониколь" in c and "Кровля" in c for c in captions), str(captions))

mode = next(r for r in at.radio if str(r.label) == "Режим просмотра сообщений")
mode.set_value("Вся лента").run()
check("вся лента с фильтром открылась", not at.exception, str(at.exception))
check("найдено — только отобранные", any("Найдено сообщений: 4." in str(c.value) for c in at.caption),
      str([str(c.value) for c in at.caption if "Найдено" in str(c.value)]))

print("4. Фильтр по типу сообщения")
at = AppTest.from_function(messages_app, default_timeout=60)
at.run()
kinds = type_select(at)
check("фильтр «Тип сообщения» на месте", kinds is not None, str([str(m.label) for m in at.multiselect]))
check("в списке типов — число сообщений", kinds is not None and kinds.format_func("Пост") == "Пост · 22",
      kinds.format_func("Пост") if kinds is not None else "")
kinds.select("Комментарий").run()
check("выбран тип — в ленте только его сообщения", shown_ids(at) == list(range(8)), str(shown_ids(at)))
check("подпись называет тип", any("с типом «Комментарий»" in str(c.value) for c in at.caption))
check("фильтр типа не меняет карточки", cards(at) == {"Репост": "30 · 50%", "Пост": "22 · 37%", "Комментарий": "8 · 13%"},
      str(cards(at)))
check("над карточками — что в ленте", any("в ленте только: Комментарий" in str(c.value) for c in at.caption))
type_select(at).set_value([]).run()
tag_select(at).select("Технониколь").run()
check("число в списке типов — с учётом тегов", type_select(at).format_func("Пост") == "Пост · 4",
      type_select(at).format_func("Пост"))
type_select(at).select("Пост").run()
check("тег и тип вместе", shown_ids(at) == [8, 9, 10, 11], str(shown_ids(at)))
check("подпись называет тег и тип", any("с тегом «Технониколь» и с типом «Пост»" in str(c.value) for c in at.caption))
tag_select(at).set_value([]).run()
type_select(at).set_value(["Репост"]).run()
next(r for r in at.radio if str(r.label) == "Режим просмотра сообщений").set_value("Вся лента").run()
check("вся лента по типу", any("Найдено сообщений: 30." in str(c.value) for c in at.caption),
      str([str(c.value) for c in at.caption if "Найдено" in str(c.value)]))

at = AppTest.from_function(messages_app, default_timeout=60)
at.run()
type_select(at).select("Комментарий").run()
at.session_state["variant"] = "types_upper"
at.run()
check("тот же тип в другом написании остаётся выбранным", not at.exception
      and list(type_select(at).value) == ["КОММЕНТАРИЙ"] and shown_ids(at) == list(range(8)),
      str(at.exception) or str(type_select(at).value))

at = AppTest.from_function(messages_app, default_timeout=60)
at.session_state["variant"] = "no_types"
at.run()
check("выгрузка без типа — фильтра типа нет", not at.exception and type_select(at) is None and tag_select(at) is not None)

print("5. Страницы и смена выборки")
at = AppTest.from_function(messages_app, default_timeout=60)
at.run()
next(r for r in at.radio if str(r.label) == "Режим просмотра сообщений").set_value("Вся лента").run()
next(s for s in at.selectbox if str(s.label) == "Сообщений на странице").set_value(25).run()
next(n for n in at.number_input if str(n.label) == "Страница").set_value(3).run()
check("третья страница всей ленты", any("Показано: 51–60 из 60" in str(c.value) for c in at.caption),
      str([str(c.value) for c in at.caption if "Показано" in str(c.value)]))
tag_select(at).select("Технониколь").run()
check("фильтр на третьей странице — без ошибки и с первой страницы", not at.exception
      and any("Показано: 1–12 из 12" in str(c.value) for c in at.caption),
      str(at.exception) or str([str(c.value) for c in at.caption if "Показано" in str(c.value)]))

at = AppTest.from_function(messages_app, default_timeout=60)
at.run()
tag_select(at).select("Технониколь").run()
at.session_state["variant"] = "no_technonicol"
at.run()
check("тег, которого нет в выборке, снимается без ошибки", not at.exception and tag_select(at) is not None
      and list(tag_select(at).value) == [] and len(tag_captions(at)) == 15,
      str(at.exception) or str(tag_select(at).value if tag_select(at) else None))

at = AppTest.from_function(messages_app, default_timeout=60)
at.run()
tag_select(at).select("Технониколь").run()
at.session_state["variant"] = "respelled"
at.run()
check("тот же тег в другом написании остаётся выбранным", not at.exception
      and list(tag_select(at).value) == ["ТЕХНОНИКОЛЬ"] and len(tag_captions(at)) == 12,
      str(at.exception) or str(tag_select(at).value))

at = AppTest.from_function(messages_app, default_timeout=60)
at.session_state["variant"] = "no_types"
at.run()
check("выгрузка без типа — подпись вместо карточек", not at.metric
      and any("Разбивки по типу сообщения нет" in str(c.value) for c in at.caption))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Фильтры по тегам и типу сообщения и разбивка по типам работают.")
