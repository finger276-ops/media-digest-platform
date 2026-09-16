# -*- coding: utf-8 -*-
"""Прямые тесты ручной модерации (src/services/manual_moderation.py).

Модуль накатывает правки аналитика (скрытие, перенос, слияние, редактирование)
поверх готовых кадров events/messages. Цена ошибки здесь — не исключение, а
тихо поехавшая цифра в отчёте: она возникает на стыке шагов (слияние →
перенос → пересчёт счётчиков → фильтр статусов), поэтому основная часть
проверок идёт через apply_manual_overrides целиком, а не по функциям поодиночке.

Часть проверок фиксирует ТЕКУЩЕЕ поведение, включая известные пробелы —
цепочка слияний A→B→C не разворачивается транзитивно, счётчики опустевшего
инфоповода-источника не обнуляются. Такие отмечены комментарием
«# текущее поведение» — правка это не баг-репорт, а фиксация контракта,
который нельзя менять по неосторожности.

Мутационные проверки (что ломает какой тест) — построчно у каждого блока,
плюс сводка в конце файла.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

from services.cached_store import clear_platform_caches  # noqa: E402
from services import manual_moderation as mm  # noqa: E402

PROJECT = "proj-1"
failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def set_manual(rows: list[tuple[str, str, dict]]) -> None:
    """Переписать таблицу правок и сбросить кеш.

    list_manual идёт через @st.cache_data (cached_store.py) и работает даже
    без рантайма Streamlit — прямая запись в CLIENT.db без сброса невидима
    следующему вызову get_manual_state. updated_at задаём возрастающим:
    list_manual сортирует по нему, и от этого порядка зависит порядок ключей
    в event_merges (важно для блока 8).
    """
    CLIENT.db["platform_manual_rows"] = [
        {
            "project_id": PROJECT,
            "table_name": table_name,
            "row_key": row_key,
            "payload": payload,
            "updated_at": f"2026-05-01T10:00:{i:02d}",
        }
        for i, (table_name, row_key, payload) in enumerate(rows)
    ]
    clear_platform_caches(PROJECT)


def base_events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "event_id": "e1",
                "event_title": "Авария на заводе",
                "event_summary": "с1",
                "display_description": "о1",
                "main_tags": "Производство",
                "status": "active",
                "message_count": 2,
                "chat_count": 2,
                "negative_count": 1,
                "importance_score": 10,
                "start_date": pd.to_datetime("2026-04-24"),
                "end_date": pd.to_datetime("2026-04-24"),
            },
            {
                "event_id": "e2",
                "event_title": "Запуск линии",
                "event_summary": "с2",
                "display_description": "о2",
                "main_tags": "Развитие",
                "status": "active",
                "message_count": 2,
                "chat_count": 1,
                "negative_count": 0,
                "importance_score": 5,
                "start_date": pd.to_datetime("2026-04-25"),
                "end_date": pd.to_datetime("2026-04-25"),
            },
            {
                "event_id": "e3",
                "event_title": "Отраслевая статистика",
                "event_summary": "с3",
                "display_description": "о3",
                "main_tags": "Рынок",
                "status": "active",
                "message_count": 1,
                "chat_count": 1,
                "negative_count": 0,
                "importance_score": 1,
                "start_date": pd.to_datetime("2026-04-26"),
                "end_date": pd.to_datetime("2026-04-26"),
            },
        ]
    )


def base_messages() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"message_id": "m1", "event_id": "e1", "event_title": "Авария на заводе", "sentiment": "негатив", "chat_title": "ВК Новости", "datetime": "2026-04-24T10:00:00"},
            {"message_id": "m2", "event_id": "e1", "event_title": "Авария на заводе", "sentiment": "нейтрал", "chat_title": "Телеграм", "datetime": "2026-04-24T12:00:00"},
            {"message_id": "m3", "event_id": "e2", "event_title": "Запуск линии", "sentiment": "Негативная", "chat_title": "ВК Новости", "datetime": "2026-04-25T09:00:00"},
            {"message_id": "m4", "event_id": "e2", "event_title": "Запуск линии", "sentiment": "позитив", "chat_title": "ВК Новости", "datetime": "2026-04-25T11:00:00"},
            {"message_id": "m5", "event_id": "e3", "event_title": "Отраслевая статистика", "sentiment": "нейтрал", "chat_title": "", "datetime": "2026-04-26T08:00:00"},
        ]
    )


def cell(df: pd.DataFrame, event_id: str, column: str):
    """Значение колонки строки с этим event_id — по маске, не по позиции
    (фильтр статусов сохраняет исходный индекс)."""
    row = df[df["event_id"].astype(str) == event_id]
    return row.iloc[0][column] if not row.empty else None


def counts(df: pd.DataFrame, event_id: str) -> tuple[int, int, int]:
    row = df[df["event_id"].astype(str) == event_id]
    if row.empty:
        return (-1, -1, -1)
    r = row.iloc[0]
    return (int(r["message_count"]), int(r["chat_count"]), int(r["negative_count"]))


# ---------------------------------------------------------------------------
# Блок 1. manual_payloads: фильтр по таблице и подмешивание _row_key
# ---------------------------------------------------------------------------

print("1. manual_payloads: фильтр по таблице и подмешивание _row_key")
df1 = pd.DataFrame(
    [
        {"table_name": "event_edits", "row_key": "event_edit::e1", "payload": "строка"},
        {"table_name": "event_edits", "row_key": "event_edit::e2", "payload": None},
        {"table_name": "event_edits", "row_key": "event_edit::e3", "payload": {"title": "ок"}},
        {"table_name": "message_moves", "row_key": "message_move::m1", "payload": {"x": 1}},
    ]
)
result1 = mm.manual_payloads(df1, "event_edits")
check("чужая таблица не попала, не-dict payload пропущен", result1 == [{"title": "ок", "_row_key": "event_edit::e3"}], str(result1))
check("исходный кадр не мутирован (закешированный кадр list_manual)", df1.loc[2, "payload"] == {"title": "ок"})
check("пустой кадр даёт []", mm.manual_payloads(pd.DataFrame(), "event_edits") == [])
check("None даёт []", mm.manual_payloads(None, "event_edits") == [])
df1_no_table = pd.DataFrame([{"row_key": "x", "payload": {}}])
check("кадр без колонки table_name даёт []", mm.manual_payloads(df1_no_table, "event_edits") == [])
df1_own_key = pd.DataFrame([{"table_name": "t", "row_key": "rk", "payload": {"_row_key": "своё"}}])
check("свой _row_key не затирается (setdefault)", mm.manual_payloads(df1_own_key, "t") == [{"_row_key": "своё"}])


# ---------------------------------------------------------------------------
# Блок 2. get_manual_state: 10 ключей, разбор id из row_key, числовые id
# ---------------------------------------------------------------------------

print("2. get_manual_state: 10 ключей, разбор id из row_key, защита от ошибки")
set_manual(
    [
        ("message_hidden", "message_hidden::m2", {}),
        ("message_moves", "message_move::m1", {"target_event_id": "e2"}),
        ("event_merges", "event_merge::e1", {"target_event_id": "e2"}),
        ("event_edits", "event_edit::e3", {"title": "Заголовок"}),
        ("title_merge_blocks", "title_merge_block::Запуск  ЛИНИИ, в Рязани", {}),
        ("manual_events", "manual_event::manual_x", {"event_id": "manual_x", "title": "Ручная тема"}),
        ("message_irrelevant", "message_irrelevant::e1::m9", {"event_id": "e1", "message_id": "m9"}),
    ]
)
state2 = mm.get_manual_state(PROJECT)
check("ровно 10 ключей состояния", sorted(state2) == sorted(["manual_df", "hidden_messages", "hidden_message_keys", "irrelevant_pairs", "irrelevant_keys", "move_map", "event_edits", "event_merges", "manual_events", "title_merge_blocks"]), str(sorted(state2)))
check("hidden_messages разобран из row_key (таблица message_hidden, префикс message_hidden::)", state2["hidden_messages"] == {"m2"})
check("hidden_message_keys хранит исходный row_key", state2["hidden_message_keys"] == {"m2": "message_hidden::m2"})
check("move_map разобран из row_key (таблица message_moves, префикс message_move::)", state2["move_map"] == {"m1": "e2"})
check("event_merges разобран из row_key (таблица event_merges, префикс event_merge::)", state2["event_merges"] == {"e1": "e2"})
check("event_edits ключ из row_key, payload содержит _row_key", "e3" in state2["event_edits"] and "_row_key" in state2["event_edits"]["e3"])
check("title_merge_blocks — сырой заголовок, без нормализации", state2["title_merge_blocks"] == {"Запуск  ЛИНИИ, в Рязани"})
check("irrelevant_pairs разобраны из payload", state2["irrelevant_pairs"] == {("e1", "m9")})
check("manual_events содержит одну запись", len(state2["manual_events"]) == 1)

set_manual(
    [
        ("message_moves", "message_move::x", {"message_id": 1, "target_event_id": 2}),
        ("event_edits", "event_edit::e9", {"event_id": 9, "title": 777}),
    ]
)
state2b = mm.get_manual_state(PROJECT)
check("числовые id приводятся к строке в move_map", state2b["move_map"] == {"1": "2"}, str(state2b["move_map"]))
check("ключ event_edits — строка, а сырой payload не приводится", "9" in state2b["event_edits"] and state2b["event_edits"]["9"]["title"] == 777)

_orig_list_manual = mm.list_manual
try:
    def _boom(*a, **k):
        raise RuntimeError("нет связи с базой")

    mm.list_manual = _boom
    state2c = mm.get_manual_state(PROJECT)
    check(
        "ошибка чтения даёт валидное пустое состояние, а не падение",
        sorted(state2c) == sorted(state2) and all(
            (not state2c[k]) for k in state2c if k != "manual_df"
        ) and isinstance(state2c["manual_df"], pd.DataFrame) and state2c["manual_df"].empty,
    )
finally:
    mm.list_manual = _orig_list_manual


# ---------------------------------------------------------------------------
# Блок 3. manual_versions: снимок по позиции, дубликаты, NaT
# ---------------------------------------------------------------------------

print("3. manual_versions: снимок по позиции, дубликаты, NaT")
snap = pd.DataFrame(
    {"row_key": ["a", "b", "a"], "updated_at": pd.to_datetime(["2026-01-01", pd.NaT, "2026-02-02"])},
    index=[10, 11, 12],
)
versions3 = mm.manual_versions({"manual_df": snap})
check("побеждает последний дубликат по позиции", versions3.get("a") == pd.Timestamp("2026-02-02"), str(versions3.get("a")))
check("NaT становится ровно None (не pd.isna-совместимым суррогатом)", versions3.get("b") is None)
check("кадр без updated_at даёт None у всех ключей", mm.manual_versions({"manual_df": pd.DataFrame({"row_key": ["a"]})}) == {"a": None})
check("manual_df не DataFrame → {}", mm.manual_versions({"manual_df": {"row_key": "a"}}) == {})
check("None state → {}", mm.manual_versions(None) == {})
check("пустой DataFrame → {}", mm.manual_versions({"manual_df": pd.DataFrame()}) == {})


# ---------------------------------------------------------------------------
# Блок 4. blocked_title_merges: нормализация заголовков
# ---------------------------------------------------------------------------

print("4. blocked_title_merges: нормализация заголовков")
blocked4 = mm.blocked_title_merges({"title_merge_blocks": {"Запуск  ЛИНИИ, в Рязани", "   ", "ёлка"}})
check("регистр/пунктуация/двойной пробел/ё→е, пробельный отброшен", blocked4 == {"запуск линии в рязани", "елка"}, str(blocked4))
check("None → set()", mm.blocked_title_merges(None) == set())
check("{} → set()", mm.blocked_title_merges({}) == set())
check("пустой набор → set()", mm.blocked_title_merges({"title_merge_blocks": set()}) == set())
check(
    "стык с get_manual_state: сырой заголовок из блока 2 нормализуется здесь",
    mm.blocked_title_merges(state2) == {"запуск линии в рязани"},
)


# ---------------------------------------------------------------------------
# Блок 5. append_manual_events: фильтры, колонки, дефолты, индекс
# ---------------------------------------------------------------------------

print("5. append_manual_events: фильтры, колонки, дефолты, индекс")
ev5 = base_events()
check("пустой список правок — тот же объект", mm.append_manual_events(ev5, []) is ev5)
ev5b = mm.append_manual_events(ev5, [{"event_id": "", "title": "нет id"}, {"event_id": "x", "title": "   "}])
check("записи без id и с пробельным названием отброшены", ev5b is ev5)
added5 = mm.append_manual_events(ev5, [{"event_id": "manual_x", "title": "Ручная тема", "description": "текст"}])
check("длина выросла на одну строку", len(added5) == 4, str(len(added5)))
expected_cols = ["event_id", "event_title", "event_summary", "display_description", "main_tags", "status", "message_count", "chat_count", "negative_count", "importance_score", "start_date", "end_date", "is_manual_event"]
check("ровно 13 колонок у новой записи", list(added5.columns) == expected_cols, str(list(added5.columns)))
new_row5 = added5[added5["event_id"] == "manual_x"].iloc[0]
check("описание попадает и в summary, и в display_description", new_row5["event_summary"] == "текст" and new_row5["display_description"] == "текст")
check("дефолт тегов «Ручной инфоповод»", new_row5["main_tags"] == "Ручной инфоповод")
check("статус active, счётчики нулевые, is_manual_event True", new_row5["status"] == "active" and int(new_row5["message_count"]) == 0 and bool(new_row5["is_manual_event"]) is True)
check("даты пустые (NaT)", pd.isna(new_row5["start_date"]) and pd.isna(new_row5["end_date"]))
check("индекс не дублируется (ignore_index=True)", added5.index.is_unique)
empty_added5 = mm.append_manual_events(pd.DataFrame(), [{"event_id": "y", "title": "Тема"}])
check("пустой исходный кадр даёт кадр 1×13", empty_added5.shape == (1, 13), str(empty_added5.shape))


# ---------------------------------------------------------------------------
# Блок 6. recompute_event_counts напрямую
# ---------------------------------------------------------------------------

print("6. recompute_event_counts: счётчики, площадки, негатив, даты")
rec6 = mm.recompute_event_counts(base_events(), base_messages())
check("e1 пересчитан (2 сообщ., 2 площадки, 1 негатив)", counts(rec6, "e1") == (2, 2, 1), str(counts(rec6, "e1")))
check("e2: 'Негативная' посчитана как негатив (.str.lower())", counts(rec6, "e2") == (2, 1, 1), str(counts(rec6, "e2")))
check("e3: пустой chat_title не даёт площадку", counts(rec6, "e3") == (1, 0, 0), str(counts(rec6, "e3")))
check("даты обсуждения — min/max по datetime сообщений", cell(rec6, "e2", "start_date") == pd.Timestamp("2026-04-25 09:00:00") and cell(rec6, "e2", "end_date") == pd.Timestamp("2026-04-25 11:00:00"))

check("пустой messages — исходный объект", mm.recompute_event_counts(base_events(), pd.DataFrame()) is not None and mm.recompute_event_counts(base_events(), pd.DataFrame()).equals(base_events()))
check("None messages не падает", mm.recompute_event_counts(base_events(), None).equals(base_events()))
no_event_id = base_messages().drop(columns=["event_id"])
check("messages без event_id — счётчики не тронуты", mm.recompute_event_counts(base_events(), no_event_id).equals(base_events()))
all_empty_ids = base_messages().assign(event_id="")
check("все event_id пустые строки — счётчики прежние", counts(mm.recompute_event_counts(base_events(), all_empty_ids), "e1") == (2, 2, 1))

only_e1 = base_messages()[base_messages()["event_id"] == "e1"]
rec6b = mm.recompute_event_counts(base_events(), only_e1)
check("событие без сообщений сохраняет СТАРЫЕ счётчики (обнуления нет)", counts(rec6b, "e2") == (2, 1, 0) and counts(rec6b, "e3") == (1, 1, 0), f"e2={counts(rec6b, 'e2')} e3={counts(rec6b, 'e3')}")

bad_dates = base_messages().assign(datetime="плохая дата")
rec6c = mm.recompute_event_counts(base_events(), bad_dates)
check("нечитаемые даты не падают, счётчики пересчитаны, даты прежние", counts(rec6c, "e1") == (2, 2, 1) and cell(rec6c, "e1", "start_date") == pd.Timestamp("2026-04-24"))


# ---------------------------------------------------------------------------
# Блок 7. Правки инфоповода поверх кадров (apply_manual_overrides)
# ---------------------------------------------------------------------------

print("7. Правки инфоповода: пустое название игнорируется, пустое описание — нет")
set_manual(
    [
        ("event_edits", "event_edit::e1", {"title": "   ", "description": "", "tags": "  ", "status": " "}),
        ("event_edits", "event_edit::e2", {"title": "Новое имя", "tags": "Тег"}),
        ("event_edits", "event_edit::нет", {"title": "Мимо"}),
    ]
)
ev7, msg7, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
check("e1: пробельное название игнорируется", cell(ev7, "e1", "event_title") == "Авария на заводе")
check("e1: пробельные теги/статус игнорируются", cell(ev7, "e1", "main_tags") == "Производство" and cell(ev7, "e1", "status") == "active")
check("e1: пустая строка по КЛЮЧУ description затирает описание", cell(ev7, "e1", "display_description") == "" and cell(ev7, "e1", "event_summary") == "")
check("e2: заголовок и теги применились", cell(ev7, "e2", "event_title") == "Новое имя" and cell(ev7, "e2", "main_tags") == "Тег")
check("e2: без ключа description описание не тронуто", cell(ev7, "e2", "display_description") == "о2" and cell(ev7, "e2", "event_summary") == "с2")
check("правка на несуществующий event_id не создаёт строк", len(ev7) == 3, str(len(ev7)))
check("переименование e2 доехало до сообщений m3/m4", (msg7[msg7["message_id"].isin(["m3", "m4"])]["event_title"] == "Новое имя").all())
check("m1/m2 заголовок не тронут", (msg7[msg7["message_id"].isin(["m1", "m2"])]["event_title"] == "Авария на заводе").all())

thin7 = pd.DataFrame([{"event_id": "e1", "event_title": "Т", "message_count": 1}])
set_manual([("event_edits", "event_edit::e1", {"description": "новое описание"})])
ev7b, _, _ = mm.apply_manual_overrides(PROJECT, thin7, pd.DataFrame())
check(
    "досоздание колонок: правка тонкого кадра не падает KeyError",
    cell(ev7b, "e1", "display_description") == "новое описание" and cell(ev7b, "e1", "event_summary") == "новое описание",
)


# ---------------------------------------------------------------------------
# Блок 8. Цепочка объединений A→B→C (главный блок)
# ---------------------------------------------------------------------------

print("8. Цепочка объединений e1→e2→e3 (нетранзитивность — текущее поведение)")
# Строки в ОБРАТНОМ порядке: если title_map/summary_map случайно окажутся
# внутри цикла (а не сняты снимком до него), e1 получил бы заголовок e3, а не
# e2 — мутация без этого порядка осталась бы незамеченной.
set_manual(
    [
        ("event_merges", "event_merge::e2", {"source_event_id": "e2", "target_event_id": "e3"}),
        ("event_merges", "event_merge::e1", {"source_event_id": "e1", "target_event_id": "e2"}),
    ]
)
ev8, msg8, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
check("e1 получает заголовок e2 СНЯТЫЙ ДО цикла слияний (не транзитивный e3)", cell(ev8, "e1", "event_title") == "Запуск линии", str(cell(ev8, "e1", "event_title")))
check("e1: описание — из e2, event_summary слияние не трогает", cell(ev8, "e1", "display_description") == "о2" and cell(ev8, "e1", "event_summary") == "с1")
check("e1: merged_into == e2", cell(ev8, "e1", "merged_into") == "e2")
check("e2 получает заголовок e3, merged_into == e3", cell(ev8, "e2", "event_title") == "Отраслевая статистика" and cell(ev8, "e2", "merged_into") == "e3")
check("e3: merged_into отсутствует (NaN)", pd.isna(cell(ev8, "e3", "merged_into")))
check("сообщения m1/m2 уезжают РОВНО на e2 (без транзитивности до e3)", set(msg8[msg8["message_id"].isin(["m1", "m2"])]["event_id"]) == {"e2"})
check("сообщения m3/m4/m5 на e3", set(msg8[msg8["message_id"].isin(["m3", "m4", "m5"])]["event_id"]) == {"e3"})
check("у всех пяти сообщений заголовок e3 — «Отраслевая статистика»", (msg8["event_title"] == "Отраслевая статистика").all())
check("e1: счётчики УСТАРЕВШИЕ — сообщений на нём нет # текущее поведение", counts(ev8, "e1") == (2, 2, 1))
check("e2 получил счётчики m1+m2", counts(ev8, "e2") == (2, 2, 1), str(counts(ev8, "e2")))
check("e3 получил счётчики m3+m4+m5", counts(ev8, "e3") == (3, 1, 1), str(counts(ev8, "e3")))
check(
    "# текущее поведение: сумма message_count по таблице завышена — 7 при 5 реальных сообщениях",
    int(ev8["message_count"].sum()) == 7,
    str(int(ev8["message_count"].sum())),
)


# ---------------------------------------------------------------------------
# Блок 9. Слияние в несуществующий инфоповод
# ---------------------------------------------------------------------------

print("9. Слияние в несуществующий инфоповод")
set_manual([("event_merges", "event_merge::e1", {"source_event_id": "e1", "target_event_id": "e_нет"})])
ev9, msg9, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
check("строка e1 остаётся, заголовком становится сырой id # текущее поведение", cell(ev9, "e1", "event_title") == "e_нет", str(cell(ev9, "e1", "event_title")))
check("описание обнулено, merged_into == e_нет", cell(ev9, "e1", "display_description") == "" and cell(ev9, "e1", "merged_into") == "e_нет")
check("сообщения m1/m2 получили event_id e_нет", set(msg9[msg9["message_id"].isin(["m1", "m2"])]["event_id"]) == {"e_нет"})
check("но заголовок m1/m2 остался прежним — рассинхронизация id/заголовка", (msg9[msg9["message_id"].isin(["m1", "m2"])]["event_title"] == "Авария на заводе").all())
check("ни одно событие не получило эти сообщения в счётчики", counts(ev9, "e1") == (2, 2, 1) and counts(ev9, "e2") == (2, 1, 1) and counts(ev9, "e3") == (1, 0, 0))


# ---------------------------------------------------------------------------
# Блок 10. Переносы сообщений
# ---------------------------------------------------------------------------

print("10. Переносы сообщений: обычный, в пустоту, поверх слияния")
set_manual([("message_moves", "message_move::m3", {"message_id": "m3", "target_event_id": "e1"})])
ev10a, msg10a, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
check("10.1 обычный перенос m3 → e1", counts(ev10a, "e1") == (3, 2, 2) and counts(ev10a, "e2") == (1, 1, 0), f"e1={counts(ev10a,'e1')} e2={counts(ev10a,'e2')}")

set_manual([("message_moves", "message_move::m1", {"message_id": "m1", "target_event_id": "e_нет"})])
ev10b, msg10b, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
m1_row = msg10b[msg10b["message_id"] == "m1"].iloc[0]
check("10.2 перенос в пустоту: сообщение остаётся с новым event_id, старым заголовком", m1_row["event_id"] == "e_нет" and m1_row["event_title"] == "Авария на заводе")
check("10.2 e1 теряет счётчики перенесённого сообщения", counts(ev10b, "e1") == (1, 1, 0), str(counts(ev10b, "e1")))
check("10.2 сумма счётчиков по таблице == 4 при 5 сообщениях в ленте", int(ev10b["message_count"].sum()) == 4 and len(msg10b) == 5)

set_manual(
    [
        ("event_merges", "event_merge::e1", {"source_event_id": "e1", "target_event_id": "e2"}),
        ("message_moves", "message_move::m1", {"message_id": "m1", "target_event_id": "e1"}),
    ]
)
ev10c, msg10c, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
m1c = msg10c[msg10c["message_id"] == "m1"].iloc[0]
m2c = msg10c[msg10c["message_id"] == "m2"].iloc[0]
check("10.3 ручной перенос побеждает автоматический редирект слияния: m1 остаётся на e1", m1c["event_id"] == "e1")
check("10.3 m2 (без ручного переноса) уезжает на e2 по слиянию", m2c["event_id"] == "e2")
check("10.3 счётчики: e1=(1,1,1), e2=(3,2,1), сумма 5", counts(ev10c, "e1") == (1, 1, 1) and counts(ev10c, "e2") == (3, 2, 1) and int(ev10c["message_count"].sum()) == 5, f"e1={counts(ev10c,'e1')} e2={counts(ev10c,'e2')} sum={int(ev10c['message_count'].sum())}")
check("10.3 заголовок m1 — «Запуск линии» (e1 после слияния носит заголовок e2)", m1c["event_title"] == "Запуск линии")


# ---------------------------------------------------------------------------
# Блок 11. Скрытие сообщений: конфликты и краевые случаи id
# ---------------------------------------------------------------------------

print("11. Скрытие сообщений: конфликт с переносом, числовой id, id из индекса")
set_manual(
    [
        ("message_moves", "message_move::m3", {"message_id": "m3", "target_event_id": "e1"}),
        ("message_hidden", "message_hidden::m3", {"message_id": "m3"}),
    ]
)
ev11a, msg11a, state11a = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
check("11.1 скрытое сообщение отсутствует в ленте", "m3" not in set(msg11a["message_id"]))
check("11.1 скрытие побеждает перенос: e1 без m3", counts(ev11a, "e1") == (2, 2, 1))
check("11.1 e2 теряет m3", counts(ev11a, "e2") == (1, 1, 0))
check("11.1 state честно содержит и hidden, и move_map (обе правки прочитаны)", state11a["hidden_messages"] == {"m3"} and state11a["move_map"] == {"m3": "e1"})

set_manual([("message_hidden", "message_hidden::7", {})])
msgs_num = pd.DataFrame([{"message_id": 7, "event_id": "e1"}, {"message_id": 8, "event_id": "e1"}])
_, msg11b, _ = mm.apply_manual_overrides(PROJECT, base_events(), msgs_num)
check("11.2 числовой message_id приводится к строке для сравнения со скрытыми", list(msg11b["message_id"]) == ["8"], str(list(msg11b["message_id"])))

set_manual([("message_hidden", "message_hidden::5", {})])
msgs_idx = pd.DataFrame([{"event_id": "e1", "text": "a"}, {"event_id": "e2", "text": "b"}], index=[5, 9])
_, msg11c, _ = mm.apply_manual_overrides(PROJECT, base_events(), msgs_idx)
check("11.3 message_id берётся из НЕпоследовательного индекса, а не range(len)", list(msg11c["message_id"]) == ["9"] and list(msg11c["event_id"]) == ["e2"], str(list(msg11c["message_id"])))

set_manual([("message_hidden", "message_hidden::m1", {})])
msgs_noevent = pd.DataFrame([{"message_id": "m1", "text": "a"}, {"message_id": "m2", "text": "b"}])
ev11d, msg11d, _ = mm.apply_manual_overrides(PROJECT, base_events(), msgs_noevent)
check("11.4 скрытие работает без колонки event_id, счётчики не пересчитываются", list(msg11d["message_id"]) == ["m2"] and counts(ev11d, "e1") == (2, 2, 1))


# ---------------------------------------------------------------------------
# Блок 12. Скрытие инфоповодов по статусу
# ---------------------------------------------------------------------------

print("12. Скрытие инфоповодов по статусу")
set_manual(
    [
        ("event_edits", "event_edit::e1", {"status": "HIDDEN"}),
        ("event_edits", "event_edit::e2", {"status": "archived"}),
        ("event_edits", "event_edit::e3", {"status": "черновик"}),
    ]
)
ev12, msg12, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
check("осталась ровно одна строка — e3 (регистр статуса не важен)", list(ev12["event_id"]) == ["e3"], str(list(ev12["event_id"])))
check("фильтр не переиндексирует кадр — искать нужно по event_id, а не .iloc[0]", list(ev12.index) == [2], str(list(ev12.index)))
check("сообщения скрытых тем ОСТАЮТСЯ в ленте со своими заголовками", len(msg12) == 5 and set(msg12[msg12["event_id"] == "e1"]["event_title"]) == {"Авария на заводе"} and set(msg12[msg12["event_id"] == "e2"]["event_title"]) == {"Запуск линии"})
check("счётчик выжившего e3 посчитан ДО фильтра", counts(ev12, "e3") == (1, 0, 0))


# ---------------------------------------------------------------------------
# Блок 13. Ручные инфоповоды: пустые кадры и перенос на ручную тему
# ---------------------------------------------------------------------------

print("13. Ручные инфоповоды: пустой вход и перенос сообщения на ручную тему")
set_manual([("manual_events", "manual_event::manual_x", {"event_id": "manual_x", "title": "Ручная тема", "description": "текст"})])
ev13a, msg13a, _ = mm.apply_manual_overrides(PROJECT, None, None)
check("13.1 пустой вход + ручной инфоповод: кадр 1×13, а не пустой", ev13a.shape == (1, 13), str(ev13a.shape))
row13a = ev13a.iloc[0]
check("13.1 main_tags дефолт, счётчик 0, is_manual_event True", row13a["main_tags"] == "Ручной инфоповод" and int(row13a["message_count"]) == 0 and bool(row13a["is_manual_event"]) is True)
check("13.1 messages_out пустой, без падения", isinstance(msg13a, pd.DataFrame) and msg13a.empty)

set_manual(
    [
        ("manual_events", "manual_event::manual_x", {"event_id": "manual_x", "title": "Ручная тема"}),
        ("message_moves", "message_move::m1", {"message_id": "m1", "target_event_id": "manual_x"}),
    ]
)
ev13b, msg13b, _ = mm.apply_manual_overrides(PROJECT, base_events(), base_messages())
check("13.2 ручная тема получает перенесённое сообщение", counts(ev13b, "manual_x") == (1, 1, 1), str(counts(ev13b, "manual_x")))
check("13.2 дата ручной темы — из перенесённого сообщения", cell(ev13b, "manual_x", "start_date") == pd.Timestamp("2026-04-24 10:00:00"))
check("13.2 m1 получает заголовок «Ручная тема»", msg13b[msg13b["message_id"] == "m1"].iloc[0]["event_title"] == "Ручная тема")
check(
    # У e1 в исходном кадре два сообщения (m1, m2); переносится только m1 —
    # m2 остаётся на e1, поэтому счётчик честно пересчитан по остатку, а не
    # устарел. Устаревшим он остаётся только когда у события НЕ остаётся ни
    # одного сообщения вовсе (recompute_event_counts не трогает такие группы
    # — это отдельно проверено в блоке 6, "событие без сообщений сохраняет
    # СТАРЫЕ счётчики").
    "13.2 e1 пересчитан по оставшемуся m2 (перенесён только m1)",
    counts(ev13b, "e1") == (1, 1, 0),
    str(counts(ev13b, "e1")),
)


# ---------------------------------------------------------------------------
# Блок 14. create_manual_event: форма id и payload
# ---------------------------------------------------------------------------

print("14. create_manual_event: форма id и payload")
CLIENT.db["platform_manual_rows"] = []
clear_platform_caches(PROJECT)
new_id14 = mm.create_manual_event(PROJECT, "  Новая тема  ", description="  опис  ", tags="  ")
check("id начинается с manual_ и имеет длину 19", new_id14.startswith("manual_") and len(new_id14) == 19, f"{new_id14!r} len={len(new_id14)}")
rows14 = CLIENT.db["platform_manual_rows"]
check("ровно одна строка в таблице manual_events", len(rows14) == 1 and rows14[0]["table_name"] == "manual_events")
check("row_key == manual_event::<id>", rows14[0]["row_key"] == f"manual_event::{new_id14}")
payload14 = rows14[0]["payload"]
check(
    "payload: strip на всех полях, дефолт тегов",
    payload14 == {"event_id": new_id14, "title": "Новая тема", "description": "опис", "tags": "Ручной инфоповод", "status": "active"},
    str(payload14),
)
check(
    "запись сразу видна через get_manual_state — без ручного сброса кеша (сохранено через cached_store)",
    mm.get_manual_state(PROJECT)["manual_events"] and mm.get_manual_state(PROJECT)["manual_events"][0]["event_id"] == new_id14,
)
empty_id14 = mm.create_manual_event(PROJECT, "  ")
ev14, _, _ = mm.apply_manual_overrides(PROJECT, pd.DataFrame(), pd.DataFrame())
check("сервис сам не отсеивает пустое название — это делает append_manual_events", len(ev14) == 1, str(len(ev14)))


# ---------------------------------------------------------------------------
# Блок 15. event_select_options
# ---------------------------------------------------------------------------

print("15. event_select_options")
agg15 = pd.DataFrame(
    [
        {"title": "Авария на заводе", "message_count": 3, "event_ids": ["e1", "e1b"]},
        {"title": "", "message_count": 0, "event_ids": ["e2"]},
        {"title": "Пустая", "message_count": 1, "event_ids": []},
    ]
)
opts15 = mm.event_select_options(agg15)
check("возвращается первый id группы, пустой заголовок → id, группа без event_ids пропущена", opts15 == [("e1", "Авария на заводе · 3 сообщ."), ("e2", "e2 · 0 сообщ.")], str(opts15))
check("исключение по НЕпервому id группы убирает группу целиком", mm.event_select_options(agg15, {"e1b"}) == [("e2", "e2 · 0 сообщ.")])
check("исключение по первому id группы даёт тот же результат", mm.event_select_options(agg15, {"e1"}) == mm.event_select_options(agg15, {"e1b"}))
check("None/пустой кадр → []", mm.event_select_options(None) == [] and mm.event_select_options(pd.DataFrame()) == [])
check("кадр без event_ids → []", mm.event_select_options(pd.DataFrame([{"title": "x"}])) == [])


# ---------------------------------------------------------------------------
# Блок 16. Регресс на ловушку кеша (страхует сам тест, не продакшен)
# ---------------------------------------------------------------------------

print("16. Инфраструктура теста: кеш без сброса невидим")
CLIENT.db["platform_manual_rows"] = [
    {"project_id": PROJECT, "table_name": "event_edits", "row_key": "event_edit::e1", "payload": {"title": "Без сброса"}, "updated_at": "2026-05-01T10:00:00"}
]
# Без clear_platform_caches — предыдущее состояние ещё в кеше list_manual.
before16 = mm.get_manual_state(PROJECT)["event_edits"]
clear_platform_caches(PROJECT)
after16 = mm.get_manual_state(PROJECT)["event_edits"]
check(
    "до сброса кеша правка не видна, после — видна (страховка от потери clear_platform_caches в set_manual)",
    before16 == {} and "e1" in after16,
    f"before={before16} after={after16}",
)


print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Все проверки ручной модерации пройдены.")
