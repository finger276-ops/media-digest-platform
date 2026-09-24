# -*- coding: utf-8 -*-
"""Запасной путь индексов бренда: отдельная выгрузка по категории.

Если конкурентов нет в выгрузке проекта, аналитик приносит файл по всей
категории. Платформа сворачивает его в агрегаты по брендам и хранит одну
строку на период (services/category_store.py, блок «Выгрузка по категории
отдельным файлом» — brand_metrics_ui.render_category_upload). Путь редкий,
поэтому ломается незаметно: основной путь — бренды из тегов проекта — уже
проверяют test_brand_metrics.py, test_brand_metrics_periods.py и
test_ai_summary.py.

Supabase подменён поддельным клиентом, тест не ходит в сеть.

Мутационные проверки (каждая правка обязана покраснить тест):
- в aggregate_by_brand_columns вернуть audience_place_key(table) вместо
  _audience_place(table) → краснеют «канон выгрузки: площадка учтена один
  раз» и «аудитория из интерфейса без двойного счёта площадки»;
- в aggregate_by_brand_values убрать строку work["_audience_place"] = ... →
  краснеет «по значениям: площадка учтена один раз внутри бренда»;
- в merged_benchmark группировать по "brand" вместо "_key" → краснеют
  «Rockwool и ROCKWOOL — один конкурент» и «лидером категории не назван
  собственный бренд»;
- в merged_benchmark снова отмечать своим только own_brand первого периода →
  краснеет «свой бренд берётся из отметки каждого периода»;
- в save_benchmark убрать on_conflict → краснеет «другой период — отдельная
  запись» (поддельный клиент без ключа конфликта перезаписывает чужую строку);
- в delete_benchmark убрать фильтр по периоду → краснеет «другой период
  проекта на месте»; в load_benchmarks убрать фильтр по проекту → краснеет
  «строки чужого проекта не подмешиваются»;
- в render_category_upload вернуть default=candidates[:12] → краснеет «по
  умолчанию отмечены только теговые колонки».
"""

import io
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"

import pandas as pd  # noqa: E402

from fake_supabase import FakeClient  # noqa: E402
from import_adapters import canonicalize_table  # noqa: E402
from services import category_store  # noqa: E402
from services.brand_metrics import compute_reach_score, compute_sov  # noqa: E402

# category_store забирает get_supabase_client к себе при импорте, поэтому
# подменять нужно у него, а не в platform_store.
CLIENT = FakeClient()
category_store.get_supabase_client = lambda: CLIENT
TABLE = category_store.TABLE
PROJECT_ID = "proj-cat"

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


def by_brand(frame):
    """Строки агрегата по имени бренда — порядок строк тестам не важен."""
    if frame is None or frame.empty:
        return {}
    return {str(row["brand"]): row for row in frame.to_dict("records")}


def ba_row(index, place, place_url, audience, views, engagement, story, brands):
    """Строка выгрузки Brand Analytics: теговые колонки идут после «Обработано»."""
    row = {
        "Дата": f"0{1 + index}.08.2026 10:00",
        "Текст": f"Сообщение {index}",
        "Hash сообщения": f"h{index}",
        "ID сообщения": str(index),
        "Источник": "vk.com",
        "Url": f"https://vk.com/wall-{index}",
        "Тип источника": "Соцсети",
        "Сюжет": story,
        "Автор": f"Автор {index}",
        "Url автора": f"https://vk.com/id{index}",
        "Место публикации": place,
        "Url места публикации": place_url,
        "Аудитория": audience,
        "Просмотров": views,
        "Вовлеченность": engagement,
        "Обработано": "Да",
        # Собственное поле системы за маркером — тегом и брендом не является.
        "Место": "Россия, Москва",
    }
    for name in ("Docke", "Tegola", "Ruflex"):
        row[name] = name if name in brands else ""
    return row


# Паблик club1 опубликовал пост, под ним два комментария — у всех трёх строк
# одна аудитория площадки (10 000). Четвёртое сообщение — другой паблик.
BA_RAW = pd.DataFrame(
    [
        ba_row(0, "Паблик про кровлю", "https://vk.com/club1", "10 000", "500", "5", "Кровля", ["Docke"]),
        ba_row(1, "Паблик про кровлю", "https://vk.com/club1", "10 000", "0", "1", "Кровля", ["Docke"]),
        ba_row(2, "Паблик про кровлю", "https://vk.com/club1", "10 000", "0", "2", "Кровля",
               ["Docke", "Ruflex"]),
        ba_row(3, "Другой паблик", "https://vk.com/club2", "3 000", "300", "3", "", ["Docke", "Tegola"]),
    ]
)
# Тот же вид, что даёт read_canonical_bytes в блоке загрузки: русские имена
# колонок («Профиль блога», «Автор»), теги — отдельными колонками.
BA_CANON = canonicalize_table(BA_RAW, source_file="cat.csv", source_system="brand_analytics")
BRAND_COLUMNS = ["Docke", "Tegola", "Ruflex"]

print("1. Какие колонки предлагаются как бренды")
check("пустая таблица — кандидатов нет", category_store.candidate_brand_columns(pd.DataFrame()) == [])
check("None — кандидатов нет", category_store.candidate_brand_columns(None) == [])
candidates = category_store.candidate_brand_columns(BA_CANON)
check("теговые колонки Brand Analytics идут первыми, в порядке файла", candidates[:3] == BRAND_COLUMNS,
      str(candidates))
check("поле системы за «Обработано» брендом не предлагается", "Место" not in candidates, str(candidates))
check("заполненный «Сюжет» предлагается для режима «по значениям»", "Сюжет" in candidates, str(candidates))
check("пустые тематические колонки не предлагаются", not ({"Теги", "Категории", "Все темы"} & set(candidates)),
      str(candidates))
declared_missing = pd.DataFrame([{"A": "A", "source_tag_columns": "A|Пропала| A "}])
check("объявленная, но отсутствующая колонка пропускается, повтор не дублируется",
      category_store.candidate_brand_columns(declared_missing) == ["A"],
      str(category_store.candidate_brand_columns(declared_missing)))

print("2. Агрегаты, когда каждый бренд — отдельная колонка")
check("без выбранных колонок агрегатов нет", category_store.aggregate_by_brand_columns(BA_CANON, []).empty)
check("пустая таблица — агрегатов нет",
      category_store.aggregate_by_brand_columns(pd.DataFrame(), BRAND_COLUMNS).empty)
columns_result = category_store.aggregate_by_brand_columns(BA_CANON, BRAND_COLUMNS + ["Нет такой"])
rows = by_brand(columns_result)
check("несуществующая колонка молча пропущена", set(rows) == set(BRAND_COLUMNS), str(list(rows)))
check("сообщение с двумя брендами засчитано обоим",
      rows.get("Docke", {}).get("messages") == 4 and rows.get("Tegola", {}).get("messages") == 1
      and rows.get("Ruflex", {}).get("messages") == 1, str(columns_result.to_dict("records")))
check("охват сложен по сообщениям бренда", rows.get("Docke", {}).get("reach") == 800,
      str(rows.get("Docke")))
check("вовлечённость сложена по сообщениям бренда", rows.get("Docke", {}).get("engagement") == 11,
      str(rows.get("Docke")))
check("сначала самый упоминаемый бренд", str(columns_result.iloc[0]["brand"]) == "Docke",
      str(columns_result.to_dict("records")))
check("до выбора своего бренда своих нет", not bool(columns_result["is_own"].any()))
# Аудитория — свойство площадки: у поста и двух комментариев под ним одни и
# те же подписчики. Раньше площадка canon-таблицы не узнавалась (там колонки
# «Профиль блога», а не chat_profile), и club1 считался трижды: 33 000.
check("канон выгрузки: площадка учтена один раз", rows.get("Docke", {}).get("audience") == 13_000,
      str(rows.get("Docke")))
check("площадка из двух брендов считается в каждом",
      rows.get("Ruflex", {}).get("audience") == 10_000 and rows.get("Tegola", {}).get("audience") == 3_000,
      str(columns_result.to_dict("records")))
empty_column = BA_CANON.assign(Пусто="")
check("колонка без единой отметки брендом не становится",
      "Пусто" not in by_brand(category_store.aggregate_by_brand_columns(empty_column, ["Docke", "Пусто"])))

# Тот же расчёт на таблице в виде сообщений проекта (английские имена колонок).
messages_shape = pd.DataFrame(
    [
        {"chat_profile": "https://vk.com/club1", "audience": 10_000, "views": 500, "engagement": 5,
         "Docke": "Docke"},
        {"chat_profile": "https://vk.com/club1", "audience": 10_000, "views": 0, "engagement": 1,
         "Docke": "Docke"},
        {"chat_profile": "https://vk.com/club2", "audience": 3_000, "views": 300, "engagement": 3,
         "Docke": ""},
    ]
)
shape_rows = by_brand(category_store.aggregate_by_brand_columns(messages_shape, ["Docke"]))
check("таблица в виде сообщений проекта: площадка учтена один раз",
      shape_rows.get("Docke", {}).get("audience") == 10_000, str(shape_rows))

print("3. Агрегаты, когда бренд записан значением одной колонки")
generic = canonicalize_table(
    pd.DataFrame(
        [
            {"Дата": "01.08.2026", "Сообщение": "раз", "Блог": "Паблик 1", "Профиль блога": "https://vk.com/club1",
             "Аудитория": 10_000, "Просмотры": 100, "Вовлечённость": 1, "Категории": "Docke"},
            {"Дата": "01.08.2026", "Сообщение": "два", "Блог": "Паблик 1", "Профиль блога": "https://vk.com/club1",
             "Аудитория": 10_000, "Просмотры": 50, "Вовлечённость": 2, "Категории": " Docke "},
            {"Дата": "01.08.2026", "Сообщение": "три", "Блог": "Паблик 1", "Профиль блога": "https://vk.com/club1",
             "Аудитория": 10_000, "Просмотры": 10, "Вовлечённость": 1, "Категории": "Tegola"},
            {"Дата": "02.08.2026", "Сообщение": "четыре", "Блог": "Паблик 2",
             "Профиль блога": "https://vk.com/club2", "Аудитория": 2_000, "Просмотры": 20, "Вовлечённость": 1,
             "Категории": "Tegola"},
            {"Дата": "02.08.2026", "Сообщение": "пять", "Блог": "Паблик 3", "Профиль блога": "https://vk.com/club3",
             "Аудитория": 500, "Просмотры": 5, "Вовлечённость": 0, "Категории": "Tegola"},
            {"Дата": "02.08.2026", "Сообщение": "шесть", "Блог": "Паблик 4",
             "Профиль блога": "https://vk.com/club4", "Аудитория": 999, "Просмотры": 1, "Вовлечённость": 1,
             "Категории": ""},
        ]
    )
)
check("«Категории» с брендами предлагаются кандидатом",
      "Категории" in category_store.candidate_brand_columns(generic),
      str(category_store.candidate_brand_columns(generic)))
values_result = category_store.aggregate_by_brand_values(generic, "Категории")
values_rows = by_brand(values_result)
check("пробелы по краям не делают второй бренд", set(values_rows) == {"Docke", "Tegola"}, str(list(values_rows)))
check("строка без бренда не считается", int(values_result["messages"].sum()) == 5,
      str(values_result.to_dict("records")))
check("упоминания посчитаны",
      values_rows.get("Docke", {}).get("messages") == 2 and values_rows.get("Tegola", {}).get("messages") == 3,
      str(values_result.to_dict("records")))
check("охват и вовлечённость сложены",
      values_rows.get("Docke", {}).get("reach") == 150 and values_rows.get("Docke", {}).get("engagement") == 3,
      str(values_rows.get("Docke")))
check("по значениям: площадка учтена один раз внутри бренда",
      values_rows.get("Docke", {}).get("audience") == 10_000, str(values_rows.get("Docke")))
check("по значениям: площадка из двух брендов считается в каждом",
      values_rows.get("Tegola", {}).get("audience") == 12_500, str(values_rows.get("Tegola")))
check("числа целые — без дробей в таблице",
      all(str(values_result[col].dtype).startswith("int") for col in ["messages", "audience", "reach", "engagement"]),
      str(values_result.dtypes.to_dict()))
check("top_n оставляет самых упоминаемых",
      list(category_store.aggregate_by_brand_values(generic, "Категории", top_n=1)["brand"]) == ["Tegola"])
check("нет колонки — нет агрегатов", category_store.aggregate_by_brand_values(generic, "Нет такой").empty)
check("колонка без значений — нет агрегатов",
      category_store.aggregate_by_brand_values(generic.assign(Категории=""), "Категории").empty)

print("4. Отметка своего бренда")
marked = category_store.mark_own_brand(columns_result, "Ruflex")
check("свой бренд ровно один", list(marked.loc[marked["is_own"], "brand"]) == ["Ruflex"],
      str(marked.to_dict("records")))
check("исходная таблица не меняется", not bool(columns_result["is_own"].any()))
check("незнакомое имя — своих нет", not bool(category_store.mark_own_brand(columns_result, "Кнауф")["is_own"].any()))
check("None проходит насквозь", category_store.mark_own_brand(None, "Ruflex") is None)
check("пустая таблица проходит насквозь", category_store.mark_own_brand(pd.DataFrame(), "Ruflex").empty)

print("5. Сохранение: одна строка на период проекта, только агрегаты")
category_store.save_benchmark(
    project_id=PROJECT_ID,
    period_id="p1",
    brands=marked,
    own_brand="Ruflex",
    brand_mode=category_store.BRAND_MODE_COLUMNS,
    brand_source="Docke, Tegola, Ruflex",
    source_filename="cat.csv",
    messages_total=len(BA_CANON),
)
stored = CLIENT.db.get(TABLE, [])
check("записана одна строка", len(stored) == 1, str(stored))
record = stored[0] if stored else {}
check(
    "поля строки совпадают со схемой таблицы",
    set(record) == {"project_id", "period_id", "own_brand", "brands", "brand_mode", "brand_source",
                    "source_filename", "messages_total", "updated_at"},
    str(sorted(record)),
)
check("ключ строки — проект и период", record.get("project_id") == PROJECT_ID and record.get("period_id") == "p1",
      str(record))
check("служебные поля сохранены",
      record.get("own_brand") == "Ruflex" and record.get("brand_mode") == "columns"
      and record.get("brand_source") == "Docke, Tegola, Ruflex" and record.get("source_filename") == "cat.csv"
      and record.get("messages_total") == 4 and bool(record.get("updated_at")),
      str({k: v for k, v in record.items() if k != "brands"}))
brands_payload = record.get("brands") or []
check("у каждого бренда только шесть полей агрегата",
      bool(brands_payload) and all(set(b) == {"brand", "messages", "audience", "reach", "engagement", "is_own"}
                                   for b in brands_payload),
      str(brands_payload))
# В jsonb уходят обычные числа Python: numpy.int64 и numpy.bool_ клиент
# Supabase сериализовать не умеет.
check("числа и флаги — обычные типы Python",
      all(type(b[k]) is int for b in brands_payload for k in ("messages", "audience", "reach", "engagement"))
      and all(type(b["is_own"]) is bool for b in brands_payload),
      str([{k: type(v).__name__ for k, v in b.items()} for b in brands_payload]))
try:
    json.dumps(record, ensure_ascii=False)
    serializable = True
except TypeError:
    serializable = False
check("строка сериализуется в JSON", serializable)
check("свой бренд в сохранённых агрегатах один",
      [b["brand"] for b in brands_payload if b["is_own"]] == ["Ruflex"], str(brands_payload))

values_marked = category_store.mark_own_brand(values_result, "Docke")
category_store.save_benchmark(
    project_id=PROJECT_ID, period_id="p1", brands=values_marked, own_brand="Docke",
    brand_mode=category_store.BRAND_MODE_VALUES, brand_source="Категории", source_filename="cat2.xlsx",
    messages_total=6,
)
p1_rows = [r for r in CLIENT.db[TABLE] if r["project_id"] == PROJECT_ID and r["period_id"] == "p1"]
check("повторная загрузка за период заменяет строку, а не добавляет", len(p1_rows) == 1, str(p1_rows))
check("в строке новая выгрузка",
      bool(p1_rows) and p1_rows[0]["brand_mode"] == "values" and p1_rows[0]["own_brand"] == "Docke"
      and {b["brand"] for b in p1_rows[0]["brands"]} == {"Docke", "Tegola"},
      str(p1_rows))
category_store.save_benchmark(project_id=PROJECT_ID, period_id="p2", brands=marked, own_brand="Ruflex")
category_store.save_benchmark(project_id="proj-other", period_id="p1", brands=marked, own_brand="Ruflex")
check("другой период — отдельная запись", len(CLIENT.db[TABLE]) == 3, str(CLIENT.db[TABLE]))
still_p1 = [r for r in CLIENT.db[TABLE] if r["project_id"] == PROJECT_ID and r["period_id"] == "p1"]
check("чужой проект с тем же периодом строку не трогает",
      len(still_p1) == 1 and still_p1[0]["own_brand"] == "Docke", str(still_p1))
check("по умолчанию режим — колонки брендов",
      next(r for r in CLIENT.db[TABLE] if r["period_id"] == "p2")["brand_mode"] == "columns")

before = len(CLIENT.db[TABLE])
for empty in (pd.DataFrame(), None):
    try:
        category_store.save_benchmark(project_id=PROJECT_ID, period_id="p3", brands=empty, own_brand="X")
        raised = False
    except ValueError:
        raised = True
    check(f"пустые агрегаты ({type(empty).__name__}) не сохраняются, а объясняются", raised)
check("и в базу ничего не попало", len(CLIENT.db[TABLE]) == before)

print("6. Чтение")
loaded = category_store.load_benchmark(PROJECT_ID, "p1")
check("строка периода читается", bool(loaded) and loaded.get("own_brand") == "Docke", str(loaded))
check("нет строки — None", category_store.load_benchmark(PROJECT_ID, "p9") is None)
many = category_store.load_benchmarks(PROJECT_ID, ["p1", "p2", "p9", ""])
check("несколько периодов одним запросом — по ключу периода", set(many) == {"p1", "p2"}, str(list(many)))
check("строки чужого проекта не подмешиваются",
      all(r.get("project_id") == PROJECT_ID for r in many.values()), str(many))


def _no_network():
    raise AssertionError("без периодов в базу ходить незачем")


category_store.get_supabase_client = _no_network
try:
    check("без периодов — пустой ответ без запроса",
          category_store.load_benchmarks(PROJECT_ID, []) == {} and category_store.load_benchmarks(PROJECT_ID, None) == {})
finally:
    category_store.get_supabase_client = lambda: CLIENT

print("7. Удаление")
category_store.delete_benchmark(PROJECT_ID, "p1")
check("строка периода удалена", category_store.load_benchmark(PROJECT_ID, "p1") is None)
check("другой период проекта на месте", category_store.load_benchmark(PROJECT_ID, "p2") is not None)
check("тот же период чужого проекта на месте", category_store.load_benchmark("proj-other", "p1") is not None)

print("8. Сумма категории за несколько периодов")
check("без периодов — None", category_store.merged_benchmark({}) is None)
check("периоды без брендов — None", category_store.merged_benchmark({"p1": {"brands": []}, "p2": {}}) is None)
two_periods = {
    "p1": {"own_brand": "Кнауф", "brands": [
        {"brand": "Кнауф", "messages": 10, "audience": 1_000, "reach": 100, "engagement": 1, "is_own": True},
        {"brand": "Rockwool", "messages": 30, "audience": 3_000, "reach": 300, "engagement": 3, "is_own": False},
    ]},
    "p2": {"own_brand": "Кнауф", "brands": [
        {"brand": "Кнауф", "messages": 20, "audience": 2_000, "reach": 500, "engagement": 2, "is_own": True},
        {"brand": "Rockwool", "messages": 30, "audience": 3_000, "reach": 400, "engagement": 4, "is_own": False},
    ]},
}
merged = category_store.merged_benchmark(two_periods) or {}
merged_rows = {b["brand"]: b for b in merged.get("brands") or []}
check("бренды сложены по периодам",
      merged_rows.get("Кнауф", {}).get("messages") == 30 and merged_rows.get("Rockwool", {}).get("messages") == 60
      and merged_rows.get("Rockwool", {}).get("reach") == 700 and merged_rows.get("Кнауф", {}).get("engagement") == 3,
      str(merged))
check("число периодов записано", merged.get("periods") == 2, str(merged))
check("свой бренд назван", merged.get("own_brand") == "Кнауф" and merged_rows.get("Кнауф", {}).get("is_own") is True,
      str(merged))
check("SOV за два периода: 30 из 90", close(compute_sov(merged)["value"], 100 * 30 / 90, 0.01),
      str(compute_sov(merged)["value"]))
check("ReachScore за два периода: 600 из 700", close(compute_reach_score(merged)["value"], 100 * 600 / 700, 0.01),
      str(compute_reach_score(merged)["value"]))

# Файлы за разные месяцы выгружены по-разному: колонка бренда называлась то
# «Knauf Insulation», то «KNAUF INSULATION», конкурент — «Rockwool» и
# «ROCKWOOL». Раньше своим считался только бренд первого периода, и второй
# месяц своих упоминаний уходил в конкуренты.
renamed = {
    "p1": {"own_brand": "Knauf Insulation", "brands": [
        {"brand": "Knauf Insulation", "messages": 10, "reach": 100, "is_own": True},
        {"brand": "Rockwool", "messages": 30, "reach": 300, "is_own": False},
    ]},
    "p2": {"own_brand": "KNAUF INSULATION", "brands": [
        {"brand": "KNAUF INSULATION", "messages": 20, "reach": 500, "is_own": True},
        {"brand": "ROCKWOOL", "messages": 30, "reach": 400, "is_own": False},
    ]},
}
renamed_merged = category_store.merged_benchmark(renamed) or {}
renamed_brands = renamed_merged.get("brands") or []
check("Rockwool и ROCKWOOL — один конкурент",
      [b["messages"] for b in renamed_brands if not b["is_own"]] == [60], str(renamed_brands))
check("свой бренд отмечен в обоих периодах",
      [b["messages"] for b in renamed_brands if b["is_own"]] == [30], str(renamed_brands))
check("SOV за два периода не теряет свой второй месяц",
      close(compute_sov(renamed_merged)["value"], 100 * 30 / 90, 0.01), str(compute_sov(renamed_merged)["value"]))
renamed_reach = compute_reach_score(renamed_merged)
check("лидером категории не назван собственный бренд",
      renamed_reach["inputs"].get("Лидер по охвату") == "Rockwool"
      and close(renamed_reach["value"], 100 * 600 / 700, 0.01),
      str(renamed_reach.get("inputs")))

# Отметка своего бренда живёт в каждой строке: в августе своим выбран
# «Кнауф», в сентябре — «Knauf» (кириллица и латиница — разные написания).
mixed_own = category_store.merged_benchmark({
    "p1": {"own_brand": "Кнауф", "brands": [
        {"brand": "Кнауф", "messages": 5, "is_own": True},
        {"brand": "Rockwool", "messages": 5, "is_own": False},
    ]},
    "p2": {"own_brand": "Knauf", "brands": [
        {"brand": "Knauf", "messages": 5, "is_own": True},
        {"brand": "Rockwool", "messages": 5, "is_own": False},
    ]},
}) or {}
check("свой бренд берётся из отметки каждого периода",
      {b["brand"] for b in mixed_own.get("brands") or [] if b["is_own"]} == {"Кнауф", "Knauf"},
      str(mixed_own))
legacy = category_store.merged_benchmark(
    {"p1": {"own_brand": "A", "brands": [{"brand": "A", "messages": 1}, {"brand": "B", "messages": 3}]}}
) or {}
legacy_rows = {b["brand"]: b for b in legacy.get("brands") or []}
check("строка без флага is_own: свой бренд по own_brand",
      legacy_rows.get("A", {}).get("is_own") is True and legacy_rows.get("B", {}).get("is_own") is False,
      str(legacy))
check("недостающие числа — нули, а не ошибка",
      legacy_rows.get("B", {}).get("reach") == 0 and legacy_rows.get("B", {}).get("audience") == 0, str(legacy))
check("загрузки без сообщений проекта — только загрузка",
      close(compute_sov(category_store.resolve_category_benchmark(None, None, two_periods))["value"],
            100 * 30 / 90, 0.01))

print("9. Блок «Выгрузка по категории» в интерфейсе")
from streamlit.testing.v1 import AppTest  # noqa: E402

CLIENT.db[TABLE] = []
csv_buffer = io.StringIO()
BA_RAW.to_csv(csv_buffer, index=False, sep=";")
CSV_BYTES = csv_buffer.getvalue().encode("utf-8-sig")

at = AppTest.from_file(str(REPO / "tests" / "category_upload_app.py"), default_timeout=90)
at.run()
check("блок открывается без исключений", not at.exception, str(at.exception))
check("есть поле загрузки файла", len(at.file_uploader) == 1)
if at.file_uploader:
    at.file_uploader[0].set_value(("cat.csv", CSV_BYTES, "text/csv")).run()
check("файл прочитан без исключений", not at.exception, str(at.exception))
check("сказано, сколько строк прочитано", any("4 строк" in str(s.value) for s in at.success),
      str([s.value for s in at.success]))
picker = [m for m in at.multiselect if m.key == "category_brand_columns"]
check("есть выбор колонок брендов", bool(picker))
if picker:
    # «Сюжет» и «Основная тема» заполнены почти у всех сообщений: отмеченные
    # по умолчанию, они становились «брендами» с наибольшим числом упоминаний
    # и съедали долю голоса своего бренда.
    check("по умолчанию отмечены только теговые колонки", list(picker[0].value) == BRAND_COLUMNS,
          str(picker[0].value))
    check("тематические колонки можно отметить вручную", "Сюжет" in picker[0].options, str(picker[0].options))
own_select = [s for s in at.selectbox if s.key == "category_own_brand"]
check("есть выбор своего бренда", bool(own_select))
if own_select:
    check("в выборе только бренды из отмеченных колонок", set(own_select[0].options) == set(BRAND_COLUMNS),
          str(own_select[0].options))
    own_select[0].set_value("Ruflex").run()
save_buttons = [b for b in at.button if "Сохранить данные категории" in str(b.label)]
check("есть кнопка сохранения", bool(save_buttons))
if save_buttons:
    save_buttons[0].click().run()
    check("сохранение без исключений", not at.exception, str(at.exception))
saved_rows = CLIENT.db.get(TABLE, [])
check("в базу ушла одна строка за первый период",
      len(saved_rows) == 1 and saved_rows[0]["project_id"] == PROJECT_ID and saved_rows[0]["period_id"] == "p1",
      str(saved_rows))
if saved_rows:
    ui_record = saved_rows[0]
    ui_brands = {b["brand"]: b for b in ui_record["brands"]}
    check("режим, колонки, файл и число строк записаны",
          ui_record["brand_mode"] == "columns" and ui_record["brand_source"] == "Docke, Tegola, Ruflex"
          and ui_record["source_filename"] == "cat.csv" and ui_record["messages_total"] == 4,
          str({k: v for k, v in ui_record.items() if k != "brands"}))
    check("свой бренд — выбранный в интерфейсе",
          ui_record["own_brand"] == "Ruflex" and [n for n, b in ui_brands.items() if b["is_own"]] == ["Ruflex"],
          str(ui_record["brands"]))
    check("аудитория из интерфейса без двойного счёта площадки",
          ui_brands.get("Docke", {}).get("audience") == 13_000, str(ui_brands.get("Docke")))
saved_titles = [str(m.value) for m in at.markdown]
check("сохранённая категория показана с названием периода",
      any("Категория за период «Август»" in t for t in saved_titles), str(saved_titles))
delete_buttons = [b for b in at.button if b.key == "del_bench_p1"]
check("у сохранённой категории есть кнопка удаления", bool(delete_buttons))
if delete_buttons:
    delete_buttons[0].click().run()
    check("удаление без исключений", not at.exception, str(at.exception))
    check("строка категории удалена", CLIENT.db.get(TABLE) == [], str(CLIENT.db.get(TABLE)))

# Зритель видит загруженную категорию, но не загружает и не удаляет.
CLIENT.db[TABLE] = [dict(record, project_id=PROJECT_ID, period_id="p2")]
viewer = AppTest.from_file(str(REPO / "tests" / "category_upload_app.py"), default_timeout=90)
viewer.session_state["can_edit"] = False
viewer.run()
check("у зрителя блок без исключений", not viewer.exception, str(viewer.exception))
check("зритель видит сохранённую категорию",
      any("Категория за период «Сентябрь»" in str(m.value) for m in viewer.markdown),
      str([m.value for m in viewer.markdown]))
check("зрителю не предлагается загрузка", len(viewer.file_uploader) == 0)
check("зрителю не предлагается удаление", not [b for b in viewer.button if str(b.key).startswith("del_bench_")])

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Все проверки выгрузки по категории пройдены.")
