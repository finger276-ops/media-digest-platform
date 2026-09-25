"""Кеш платформы виден одинаково во всех вкладках и сессиях.

Счётчики версий, которыми кеш сбрасывается, раньше жили в st.session_state —
отдельно у каждой вкладки. Сам кеш (@st.cache_data) при этом общий на весь
процесс: ключ записи — (аргументы, номер версии). Из-за этого:
  - вкладка, которая ничего не правила (заказчик, вторая вкладка аналитика),
    после чужой правки до истечения TTL видела старый снимок — её собственный
    счётчик версии остался на месте;
  - два аналитика одного проекта, сделав каждый по одной правке, получали
    ОДИНАКОВЫЙ номер версии (оба считали от своего 0) — и тот, кто сохранил
    вторым, читал из кеша результат первого, а не свою же правку.

Счётчики стали общими на процесс (services/cached_store.py), поэтому кеш
сбрасывается для всех вкладок сразу. Тест воспроизводит обе ошибки через два
независимых экземпляра AppTest — это и есть две вкладки: у каждого свой
st.session_state, но один и тот же процесс и один и тот же @st.cache_data.

Мутационная проверка: вернуть версии в st.session_state (как было) — падают
«вторая вкладка не видит чужую правку сразу» и «второй редактор видит свою
правку, а не чужую».
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from streamlit.testing.v1 import AppTest  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

PROJECT = "proj-cache"
ROW_KEY = "event_edit::e1"
failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def _probe_app():
    # AppTest.from_function исполняет тело в изолированном модуле — глобальные
    # имена файла теста (PROJECT, ROW_KEY) сюда не попадают, только session_state.
    import streamlit as st

    from services import cached_store

    project, row_key = "proj-cache", "event_edit::e1"
    if st.session_state.get("probe_write"):
        cached_store.save_manual(
            project, "event_edits", row_key, {"note": st.session_state["probe_write"]}
        )
    manual = cached_store.list_manual(project)
    rows = manual[manual.get("row_key") == row_key] if not manual.empty else manual
    note = rows.iloc[0]["payload"].get("note") if len(rows) else None
    st.write(f"note={note}")


def _note_of(app) -> str:
    lines = [str(m.value) for m in app.markdown if str(m.value).startswith("note=")]
    return lines[-1][len("note="):] if lines else "<нет>"


def run_tab(write_value=None):
    app = AppTest.from_function(_probe_app, default_timeout=60)
    if write_value is not None:
        app.session_state["probe_write"] = write_value
    app.run()
    check(f"вкладка ({write_value!r}) открылась без исключений", not app.exception, str(app.exception))
    return app


print("1. Вкладка без правок видит чужую правку сразу, не дожидаясь TTL")
# Вкладка B открыта раньше — успела прочитать проект пустым и закешировать
# это под своим (тогда ещё локальным) счётчиком версии.
tab_b = run_tab()
check("вкладка B изначально видит пусто", _note_of(tab_b) == "None", _note_of(tab_b))

# Вкладка A правит и сразу видит свою правку — так работало и до фикса.
tab_a = run_tab(write_value="Правка A")
check("вкладка A видит свою правку", _note_of(tab_a) == "Правка A", _note_of(tab_a))

# Вкладка C — ещё одна, которая ничего не правила (или B перезагрузилась).
# До фикса её локальный счётчик версии остался бы на 0 и она получила бы
# из общего кеша тот самый пустой снимок, который закешировала вкладка B.
tab_c = run_tab()
check(
    "вкладка C видит правку A, не дожидаясь TTL",
    _note_of(tab_c) == "Правка A",
    _note_of(tab_c),
)

print("2. Второй редактор видит свою правку, а не правку первого")
# Оба редактора — свежие вкладки, у каждой своя, до этого не тронутая копия
# счётчика версии (в старом коде оба считали бы от локального 0). До фикса
# правка D пришлась бы на тот же номер версии, что и более ранняя правка A,
# поэтому D получил(а) бы из кеша результат A, а не собственную запись.
tab_d = run_tab(write_value="Правка D")
check(
    "второй редактор видит собственную правку",
    _note_of(tab_d) == "Правка D",
    _note_of(tab_d),
)
check(
    "в базе действительно правка D, а не A",
    store.get_manual(PROJECT, ROW_KEY) == {"note": "Правка D"},
    str(store.get_manual(PROJECT, ROW_KEY)),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Кеш ведёт себя одинаково во всех вкладках.")
