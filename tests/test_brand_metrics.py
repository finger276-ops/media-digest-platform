"""Проверка индексов бренда на примерах расчёта из гайда.

Каждый блок воспроизводит пример из Metric Calculation Guide и сверяет
результат платформы с числом, которое указано в гайде.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.brand_metrics import (  # noqa: E402
    compute_bpi,
    compute_brand_metrics,
    compute_er,
    compute_err,
    compute_nss,
    compute_reach_score,
    compute_ses,
    compute_sov,
    compute_tone_volume_score,
    merge_settings,
    metrics_to_frame,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def close(actual, expected, tolerance=0.05):
    return actual is not None and abs(float(actual) - float(expected)) <= tolerance


def make_messages(rows):
    """rows: список (тональность, охват, аудитория, реакции, тема)."""
    return pd.DataFrame(
        [
            {
                "message_id": f"m{i}",
                "sentiment": sentiment,
                "views": views,
                "audience": audience,
                "engagement": engagement,
                "event_title": theme,
            }
            for i, (sentiment, views, audience, engagement, theme) in enumerate(rows)
        ]
    )


print("1. SES — пример гайда: позитивный охват 40 млн, негативный 60 млн, общий 120 млн")
ses_messages = make_messages(
    [
        ("позитив", 40_000_000, 0, 0, "Тема A"),
        ("негатив", 60_000_000, 0, 0, "Тема B"),
        ("нейтрал", 20_000_000, 0, 0, "Тема C"),
    ]
)
ses = compute_ses(ses_messages)
check("SES = −16,67 %", close(ses["value"], -16.67), str(ses["value"]))
check("в расшифровке виден общий охват", ses["inputs"]["Общий охват"] == 120_000_000)

print("2. NSS — пример гайда: 200 упоминаний, 40 % позитив, 50 % негатив")
nss_rows = (
    [("позитив", 0, 0, 0, f"Тема {i}") for i in range(80)]
    + [("негатив", 0, 0, 0, f"Тема {100 + i}") for i in range(100)]
    + [("нейтрал", 0, 0, 0, f"Тема {200 + i}") for i in range(20)]
)
nss_messages = make_messages(nss_rows)
nss_by_messages = compute_nss(nss_messages, basis="messages")
check("NSS по сообщениям = −10 %", close(nss_by_messages["value"], -10.0), str(nss_by_messages["value"]))

print("3. NSS по инфоповодам — темы, а не сообщения")
# Две темы: одна с преобладанием позитива, вторая — негатива. Плюс нейтральная.
theme_rows = (
    [("позитив", 0, 0, 0, "Запуск завода")] * 5
    + [("негатив", 0, 0, 0, "Запуск завода")] * 2
    + [("негатив", 0, 0, 0, "Жалобы на монтаж")] * 8
    + [("позитив", 0, 0, 0, "Жалобы на монтаж")] * 1
    + [("нейтрал", 0, 0, 0, "Отраслевая статистика")] * 4
)
nss_events = compute_nss(make_messages(theme_rows), basis="events")
check(
    "одна позитивная и одна негативная тема из трёх дают 0 %",
    close(nss_events["value"], 0.0),
    str(nss_events["value"]),
)
check("считается по инфоповодам", "Всего инфоповодов" in nss_events["inputs"])
check("тем всего три", nss_events["inputs"].get("Всего инфоповодов") == 3)

print("4. ToneVolumeScore — пример гайда: 500 упоминаний, 200 позитив, 300 негатив")
tvs_rows = [("позитив", 0, 0, 0, "Тема")] * 200 + [("негатив", 0, 0, 0, "Тема")] * 300
tvs = compute_tone_volume_score(make_messages(tvs_rows))
check("ToneVolumeScore = −20 %", close(tvs["value"], -20.0), str(tvs["value"]))

print("5. ER — пример гайда: 650 реакций, 10 000 подписчиков")
er_messages = make_messages(
    [
        ("нейтрал", 0, 10_000, 650, "Тема"),
    ]
)
er = compute_er(er_messages)
check("ER = 6,5 %", close(er["value"], 6.5), str(er["value"]))

print("6. ER по отдельным колонкам реакций: 500 лайков + 100 комментариев + 50 репостов")
er_parts = pd.DataFrame(
    [
        {
            "message_id": "m1",
            "sentiment": "нейтрал",
            "audience": 10_000,
            "views": 20_000,
            "engagement": 0,
            "likes": 500,
            "comments": 100,
            "reposts": 50,
        }
    ]
)
er_split = compute_er(er_parts)
check("ER из лайков, комментариев и репостов = 6,5 %", close(er_split["value"], 6.5), str(er_split["value"]))
check(
    "в расшифровке указан источник реакций",
    er_split["inputs"]["Источник реакций"] == "лайки + комментарии + репосты",
)

print("7. ERR — пример гайда: 650 реакций, охват 20 000")
err = compute_err(er_parts)
check("ERR = 3,25 %", close(err["value"], 3.25), str(err["value"]))

print("8. SOV — пример гайда: 1 000 упоминаний бренда из 10 000 в категории")
benchmark = {
    "own_brand": "Наш бренд",
    "brands": [
        {"brand": "Наш бренд", "messages": 1_000, "reach": 50_000_000, "audience": 0, "engagement": 0, "is_own": True},
        {"brand": "Конкурент А", "messages": 6_000, "reach": 200_000_000, "audience": 0, "engagement": 0, "is_own": False},
        {"brand": "Конкурент Б", "messages": 3_000, "reach": 90_000_000, "audience": 0, "engagement": 0, "is_own": False},
    ],
}
sov = compute_sov(benchmark, basis="messages")
check("SOV = 10 %", close(sov["value"], 10.0), str(sov["value"]))
check("в расшифровке есть оба варианта", "SOV по охвату" in sov["inputs"])

print("9. ReachScore — пример гайда: охват бренда 50 млн, максимальный 200 млн")
reach_score = compute_reach_score(benchmark)
check("ReachScore = 25 %", close(reach_score["value"], 25.0), str(reach_score["value"]))
check("указан лидер категории", reach_score["inputs"]["Лидер по охвату"] == "Конкурент А")

print("10. BPI — пример гайда: 0,4 × (−10) + 0,4 × (−16,6) + 0,2 × (−20) = −14,64 %")
cards = {
    "NSS": {"key": "NSS", "code": "NSS", "value": -10.0, "available": True},
    "SES": {"key": "SES", "code": "SES", "value": -16.6, "available": True},
    "TVS": {"key": "TVS", "code": "ToneVolumeScore", "value": -20.0, "available": True},
}
bpi = compute_bpi(cards, {"NSS": 0.4, "SES": 0.4, "TVS": 0.2})
check("BPI = −14,64 %", close(bpi["value"], -14.64, 0.01), str(bpi["value"]))

print("11. BPI пересчитывает веса, когда метрики не хватает")
partial = dict(cards)
partial["SES"] = {"key": "SES", "code": "SES", "value": None, "available": False}
bpi_partial = compute_bpi(partial, {"NSS": 0.4, "SES": 0.4, "TVS": 0.2})
# Остаются NSS (0.4) и TVS (0.2) → нормализация даёт 2/3 и 1/3.
expected = (-10.0 * (0.4 / 0.6)) + (-20.0 * (0.2 / 0.6))
check("веса нормализованы по доступным метрикам", close(bpi_partial["value"], expected, 0.01), str(bpi_partial["value"]))
check("в расшифровке сказано, чего не хватило", "Не хватило данных" in bpi_partial["inputs"])

print("12. Метрики без данных не считаются молча")
empty_cards = compute_brand_metrics(pd.DataFrame())
check("SES недоступна на пустом периоде", not empty_cards["SES"]["available"])
check("причина объяснена словами", bool(empty_cards["SES"]["reason"]))
check("SOV просит выгрузку по категории", "категории" in empty_cards["SOV"]["reason"])

print("13. Полный расчёт по периоду с выгрузкой категории")
full = compute_brand_metrics(
    make_messages(
        [
            ("позитив", 40_000_000, 10_000, 400, "Запуск завода"),
            ("негатив", 60_000_000, 10_000, 200, "Жалобы"),
            ("нейтрал", 20_000_000, 10_000, 50, "Статистика"),
        ]
    ),
    benchmark=benchmark,
    settings={"bpi_weights": {"NSS": 0.4, "SES": 0.4, "TVS": 0.2}},
)
check("посчитаны все восемь метрик", all(full[k]["available"] for k in ["SES", "NSS", "TVS", "ER", "ERR", "SOV", "ReachScore", "BPI"]),
      str({k: v["available"] for k, v in full.items()}))
check("SES совпадает с примером", close(full["SES"]["value"], -16.67), str(full["SES"]["value"]))

print("14. Настройки проекта переопределяют умолчания")
settings = merge_settings({"bpi_weights": {"NSS": 1.0, "мусор": 5}, "nss_basis": "messages", "sov_basis": "reach"})
check("мусорные метрики отброшены", settings["bpi_weights"] == {"NSS": 1.0}, str(settings["bpi_weights"]))
check("основа NSS сохранена", settings["nss_basis"] == "messages")
check("основа SOV сохранена", settings["sov_basis"] == "reach")

print("Таблица метрик: формулы убраны, вывод аналитика добавлен")
# Формулы — собственная методика платформы. Заказчик получает цифру вместе с
# выводом, а устройство расчёта — предмет отдельного разговора, если спросит.
frame_cards = compute_brand_metrics(make_messages([("позитив", 100, 1000, 10, "тема")]))
frame = metrics_to_frame(frame_cards)
check("колонки «Формула» в таблице нет", "Формула" not in frame.columns, str(list(frame.columns)))
check("колонка «Вывод» есть", "Вывод" in frame.columns, str(list(frame.columns)))
check("статус остался", "Статус" in frame.columns, str(list(frame.columns)))
check("вывод по умолчанию пустой", (frame["Вывод"] == "").all(), str(list(frame["Вывод"])))

with_notes = metrics_to_frame(frame_cards, {"NSS": "Тональность выровнялась после мая"})
nss_row = with_notes[with_notes["Метрика"] == "NSS"]
check(
    "вывод подставлен к своей метрике",
    not nss_row.empty and nss_row.iloc[0]["Вывод"] == "Тональность выровнялась после мая",
    str(nss_row.to_dict("records")),
)
others = with_notes[with_notes["Метрика"] != "NSS"]
check("чужие метрики не задеты", (others["Вывод"] == "").all(), str(list(others["Вывод"])))

print("Ключ периода для выводов")
from services.metric_notes import period_key  # noqa: E402

check("порядок периодов не влияет", period_key(["b", "a"]) == period_key(["a", "b"]))
check("пустые значения отбрасываются", period_key(["a", "", None]) == "a", period_key(["a", "", None]))
check("повторы схлопываются", period_key(["a", "a"]) == "a", period_key(["a", "a"]))
check("пустой список даёт пустой ключ", period_key([]) == "" and period_key(None) == "")

print("Направление метрики задаёт цвет изменения")
# Цвет показывает направление движения, а не приговор: толкование даёт аналитик
# в столбце «Вывод», он же смотрит тональность рядом. Доля голоса красится
# наравне с остальными, хотя рост громкости бывает и скандальным.
from services.brand_metrics import METRIC_DIRECTION, metric_direction  # noqa: E402

for code in ("BPI", "NSS", "SES", "TVS", "SOV", "ReachScore", "ER", "ERR"):
    check(f"у {code} направление вверх", metric_direction(code) == "up", metric_direction(code))
check(
    "направление задано у всех восьми метрик",
    len(METRIC_DIRECTION) == 8,
    str(sorted(METRIC_DIRECTION)),
)
# Метрика без заданного направления не красится: платформа не берётся судить о
# том, чего не знает.
check("незнакомая метрика не красится", metric_direction("WHATEVER") == "neutral")

print("Бренды категории берутся из тегов самой выгрузки")
# SOV и ReachScore сравнивают бренд с категорией, и до сих пор для этого
# требовалась отдельная выгрузка по всей категории. В категорийном мониторинге
# она избыточна: конкуренты уже размечены тегами в той же выгрузке. Не хватало
# знания, какой тег бренд, а какой аналитический разрез.
from services import category_store  # noqa: E402
from services.project_settings import (  # noqa: E402
    category_brands_from_project_settings,
)

tagged = pd.DataFrame(
    [
        {"tags": "Ruflex|Монтаж", "audience": 100, "views": 50, "engagement": 5,
         "author": "a", "chat_profile": "https://vk.com/1"},
        {"tags": "Ruflex", "audience": 100, "views": 30, "engagement": 3,
         "author": "b", "chat_profile": "https://vk.com/2"},
        {"tags": "Quiet Tile|PR", "audience": 200, "views": 20, "engagement": 1,
         "author": "c", "chat_profile": "https://vk.com/3"},
        {"tags": "Docke", "audience": 700, "views": 400, "engagement": 40,
         "author": "d", "chat_profile": "https://vk.com/4"},
        {"tags": "Docke|Tegola", "audience": 300, "views": 100, "engagement": 10,
         "author": "e", "chat_profile": "https://vk.com/5"},
        {"tags": "Монтаж", "audience": 50, "views": 10, "engagement": 1,
         "author": "f", "chat_profile": "https://vk.com/6"},
    ]
)

counts = category_store.brand_counts_from_tags(tagged)
check("теги посчитаны", int(counts.get("Docke", 0)) == 2 and int(counts.get("Ruflex", 0)) == 2,
      str(dict(counts)))
check("аналитический разрез тоже виден в списке", int(counts.get("Монтаж", 0)) == 2, str(dict(counts)))

brands = category_store.brands_from_messages(tagged, ["Ruflex", "Quiet Tile"], ["Docke", "Tegola"])
check("собраны только отмеченные бренды", set(brands["brand"]) == {"Ruflex", "Quiet Tile", "Docke", "Tegola"},
      str(list(brands["brand"])))
check("неотмеченный тег брендом не стал", "Монтаж" not in set(brands["brand"]), str(list(brands["brand"])))
own_rows = brands[brands["is_own"]]
check("свои бренды отмечены оба", set(own_rows["brand"]) == {"Ruflex", "Quiet Tile"}, str(list(own_rows["brand"])))
# Одна и та же марка приходит в тегах то «Knauf Nord», то «knauf nord».
# Точное сравнение засчитало бы только выбранное написание, и часть упоминаний
# бренда молча выпала бы из доли голоса.
mixed_case = pd.DataFrame(
    [
        {"tags": "Knauf Nord", "audience": 100, "views": 10, "engagement": 1,
         "author": "a", "chat_profile": "https://vk.com/1"},
        {"tags": "knauf nord", "audience": 100, "views": 10, "engagement": 1,
         "author": "b", "chat_profile": "https://vk.com/2"},
        {"tags": "КНАУФ НОРД", "audience": 100, "views": 10, "engagement": 1,
         "author": "c", "chat_profile": "https://vk.com/3"},
        {"tags": "Rockwool", "audience": 100, "views": 10, "engagement": 1,
         "author": "d", "chat_profile": "https://vk.com/4"},
    ]
)
case_brands = category_store.brands_from_messages(mixed_case, ["knauf nord"], ["Rockwool"])
knauf = case_brands[case_brands["brand"] == "knauf nord"]
check(
    "разное написание считается одним брендом",
    not knauf.empty and int(knauf.iloc[0]["messages"]) == 2,
    str(case_brands.to_dict("records")),
)
check("и остаётся своим", not knauf.empty and bool(knauf.iloc[0]["is_own"]),
      str(case_brands.to_dict("records")))
# Кириллическое написание — другой бренд, а не то же слово латиницей.
check(
    "кириллица латиницей не подменяется",
    int(case_brands["messages"].sum()) == 3,
    str(case_brands.to_dict("records")),
)

check(
    "сообщение с двумя брендами засчитано обоим",
    int(brands[brands["brand"] == "Docke"].iloc[0]["messages"]) == 2
    and int(brands[brands["brand"] == "Tegola"].iloc[0]["messages"]) == 1,
    str(brands.to_dict("records")),
)

benchmark = category_store.benchmark_from_messages(tagged, ["Ruflex", "Quiet Tile"], ["Docke", "Tegola"])
check("бенчмарк собран", benchmark is not None)
check("группа своих брендов сохранена", benchmark["own_brands"] == ["Ruflex", "Quiet Tile"],
      str(benchmark.get("own_brands")))

sov = compute_sov(benchmark)
# Свои: Ruflex 2 + Quiet Tile 1 = 3. Вся категория: 3 + Docke 2 + Tegola 1 = 6.
check("SOV считает группу целиком", close(sov["value"], 50.0, 0.01), str(sov["value"]))
check(
    "в разборе показаны оба своих бренда",
    "Ruflex" in str(sov["inputs"]["Бренд"]) and "Quiet Tile" in str(sov["inputs"]["Бренд"]),
    str(sov["inputs"]["Бренд"]),
)

reach = compute_reach_score(benchmark)
# Свой охват 50+30+20 = 100, максимум в категории — Docke 400+100 = 500.
check("ReachScore считает группу целиком", close(reach["value"], 20.0, 0.01), str(reach["value"]))
check("лидер категории назван", reach["inputs"]["Лидер по охвату"] == "Docke", str(reach["inputs"]))

print("Группа своих брендов сравнивается с категорией как один участник")
# На выгрузке Кнауфа это и сломалось: охват группы (19 539) делился на охват
# одного чужого бренда (18 899), и индекс охвата выходил 103,39% — величина,
# которой не бывает.
group_bench = {
    "brands": [
        {"brand": "Knauf Insulation", "messages": 60, "reach": 18899,
         "audience": 0, "engagement": 0, "is_own": True},
        {"brand": "ТИСМА", "messages": 17, "reach": 640,
         "audience": 0, "engagement": 0, "is_own": True},
        {"brand": "ROCKWOOL", "messages": 90, "reach": 15000,
         "audience": 0, "engagement": 0, "is_own": False},
    ]
}
lead = compute_reach_score(group_bench)
check("индекс охвата не превышает ста процентов", lead["value"] <= 100.0, str(lead["value"]))
check("своя группа громче — значит сто процентов", close(lead["value"], 100.0, 0.01), str(lead["value"]))
check(
    "лидером названа вся группа, а не один бренд",
    "Knauf Insulation" in lead["inputs"]["Лидер по охвату"]
    and "ТИСМА" in lead["inputs"]["Лидер по охвату"],
    str(lead["inputs"]["Лидер по охвату"]),
)

behind = compute_reach_score(
    {
        "brands": [
            {"brand": "Knauf Insulation", "messages": 60, "reach": 18899,
             "audience": 0, "engagement": 0, "is_own": True},
            {"brand": "ТИСМА", "messages": 17, "reach": 640,
             "audience": 0, "engagement": 0, "is_own": True},
            {"brand": "ROCKWOOL", "messages": 90, "reach": 40000,
             "audience": 0, "engagement": 0, "is_own": False},
        ]
    }
)
check("отставание считается от конкурента", close(behind["value"], 48.85, 0.01), str(behind["value"]))
check("лидер назван верно", behind["inputs"]["Лидер по охвату"] == "ROCKWOOL",
      str(behind["inputs"]["Лидер по охвату"]))
check("число конкурентов показано", behind["inputs"]["Конкурентов в категории"] == 1,
      str(behind["inputs"]))

print("Без конкурентов доля голоса не считается")
# Сто процентов по определению — это не измерение, а его отсутствие.
own_only = {
    "brands": [
        {"brand": "Knauf Insulation", "messages": 60, "reach": 18899,
         "audience": 0, "engagement": 0, "is_own": True},
        {"brand": "ТИСМА", "messages": 17, "reach": 640,
         "audience": 0, "engagement": 0, "is_own": True},
    ]
}
sov_own = compute_sov(own_only)
reach_own = compute_reach_score(own_only)
check("SOV не выдаёт сто процентов", sov_own["value"] is None, str(sov_own["value"]))
check("ReachScore не выдаёт сто процентов", reach_own["value"] is None, str(reach_own["value"]))
check(
    "причина объясняет, чего не хватает",
    "отметьте их" in sov_own["reason"].lower(),
    sov_own["reason"],
)

# Отмеченный конкурент, которого нет в сообщениях периода, выпадает из
# агрегатов. Сказать человеку «конкуренты не отмечены», когда он их отметил,
# значит отправить его искать ошибку не там.
marked_but_absent = dict(own_only)
marked_but_absent["configured_competitors"] = 2
marked_but_absent["missing_competitors"] = ["ROCKWOOL", "ТЕХНОНИКОЛЬ"]
absent = compute_sov(marked_but_absent)
check("метрика всё равно не считается", absent["value"] is None, str(absent["value"]))
check(
    "причина другая: конкуренты отмечены, но не встретились",
    "упоминаний нет" in absent["reason"] and "ROCKWOOL" in absent["reason"],
    absent["reason"],
)
check(
    "не советует отмечать то, что уже отмечено",
    "отметьте их" not in absent["reason"].lower(),
    absent["reason"],
)

print("Без отмеченных брендов метрики честно молчат")
empty_benchmark = category_store.benchmark_from_messages(tagged, [], ["Docke"])
check("без своих брендов бенчмарк не собирается", empty_benchmark is None)
silent = compute_sov(None)
check("SOV без данных пуст", silent["value"] is None)
check(
    "причина подсказывает, что делать",
    "Отметьте бренды категории" in silent["reason"],
    silent["reason"],
)

print("Разметка брендов в настройках проекта")
parsed = category_brands_from_project_settings(
    {"category_brands": {"own": ["Кнауф", " Тисма ", "Кнауф", ""],
                         "competitors": ["Rockwool", "Кнауф"]}}
)
check("свои бренды очищены от пустых и повторов", parsed["own"] == ["Кнауф", "Тисма"], str(parsed["own"]))
check(
    "бренд не может быть одновременно своим и конкурентом",
    parsed["competitors"] == ["Rockwool"],
    str(parsed["competitors"]),
)
check("пустые настройки дают пустые списки",
      category_brands_from_project_settings({}) == {"own": [], "competitors": []})
check("None не падает",
      category_brands_from_project_settings(None) == {"own": [], "competitors": []})

print("Подписи изменений на карточках")
# Числа на платформе пишутся с запятой, но замена по всей строке заодно
# съедала точки в единице измерения: на карточке стояло «+1,14 п,п,».
from brand_metrics_ui import _metric_delta  # noqa: E402

card = {"code": "NSS", "value": 4.35, "available": True}
text, _color = _metric_delta(card, {"NSS": 3.21})
check("десятичная запятая в числе", text is not None and "1,14" in text, str(text))
check("единица измерения не искажена", text is not None and text.endswith(" п.п."), str(text))
check("в подписи нет «п,п,»", text is not None and "п,п," not in text, str(text))
down, _ = _metric_delta({"code": "NSS", "value": 3.0, "available": True}, {"NSS": 4.5})
check("падение показывается со знаком минус", down == "-1,50 п.п.", str(down))
same, _ = _metric_delta({"code": "NSS", "value": 4.35, "available": True}, {"NSS": 4.35})
check("изменение меньше сотой не показывается", same is None, str(same))
check(
    "без предыдущего периода изменения нет",
    _metric_delta(card, None)[0] is None,
    str(_metric_delta(card, None)),
)

# Отсутствие данных и законный ноль — разные ответы. Раньше выгрузка без
# колонки «Тональность» давала SES, NSS и ToneVolumeScore ровно 0, а выгрузка
# без реакций — ER и ERR 0: аналитик видел «нейтральный бренд без
# вовлечённости» там, где платформе просто нечего было считать.
from brand_metrics_ui import format_metric  # noqa: E402
from services.brand_metrics import metrics_by_period  # noqa: E402
from services.metrics_compute import prepare_dashboard_messages  # noqa: E402


def frame(sentiments, **extra):
    rows = []
    for i, sentiment in enumerate(sentiments):
        row = {"message_id": f"m{i}", "sentiment": sentiment, "views": 1000, "audience": 5000,
               "author": f"a{i}", "chat_profile": f"https://vk.com/{i}", "event_title": f"Тема {i % 2}"}
        for key, value in extra.items():
            row[key] = value[i] if isinstance(value, list) else value
        rows.append(row)
    return pd.DataFrame(rows)


print("Нет разметки тональности — прочерк, а не ноль")
# Канонический кадр без «Тональности» приходит с пустой строкой в sentiment
# (import_adapters._normalize_sentiment), а не без колонки.
unmarked = frame(["", "", "", ""], engagement=10)
for variant, data in (("сырой кадр", unmarked), ("кадр дашборда", prepare_dashboard_messages(unmarked)),
                      ("нет колонки sentiment", unmarked.drop(columns=["sentiment"]))):
    cards = compute_brand_metrics(data)
    for key in ("SES", "NSS", "TVS"):
        check(f"{variant}: {key} недоступна", not cards[key]["available"], str(cards[key]["value"]))
        check(f"{variant}: {key} объясняет причину", "тональност" in cards[key]["reason"].lower(), cards[key]["reason"])
        check(f"{variant}: {key} на карточке прочерк", format_metric(cards[key]) == "—", format_metric(cards[key]))
    check(f"{variant}: BPI по умолчанию недоступен", not cards["BPI"]["available"], str(cards["BPI"]["value"]))
    check(f"{variant}: ER при этом считается", cards["ER"]["available"], cards["ER"]["reason"])

nss_messages_basis = compute_brand_metrics(unmarked, settings={"nss_basis": "messages"})
check("NSS по сообщениям тоже недоступна", not nss_messages_basis["NSS"]["available"])

print("Всё нейтральное — законный ноль, не прочерк")
neutral = frame(["нейтральная"] * 4, engagement=10)
cards = compute_brand_metrics(neutral)
for key in ("SES", "NSS", "TVS", "BPI"):
    check(f"{key} доступна", cards[key]["available"], cards[key]["reason"])
    check(f"{key} = 0", close(cards[key]["value"], 0.0), str(cards[key]["value"]))
balanced = compute_brand_metrics(frame(["позитивная", "негативная", "позитивная", "негативная"], engagement=10))
check("позитив = негатив даёт TVS 0, а не прочерк",
      balanced["TVS"]["available"] and close(balanced["TVS"]["value"], 0.0), str(balanced["TVS"]["value"]))
partial = compute_brand_metrics(frame(["позитивная", "", "", ""], engagement=10))
check("частичная разметка считается как раньше (TVS 25%)", close(partial["TVS"]["value"], 25.0),
      str(partial["TVS"]["value"]))

print("BPI пересчитывает веса, если тональности нет, но есть ER")
mixed_weights = compute_brand_metrics(unmarked, settings={"bpi_weights": {"NSS": 0.4, "SES": 0.4, "ER": 0.2}})
check("BPI доступен", mixed_weights["BPI"]["available"], mixed_weights["BPI"]["reason"])
check("BPI равен ER (единственная доступная)", close(mixed_weights["BPI"]["value"], mixed_weights["ER"]["value"], 0.01),
      f'{mixed_weights["BPI"]["value"]} vs {mixed_weights["ER"]["value"]}')
missing = str(mixed_weights["BPI"]["inputs"].get("Не хватило данных", ""))
check("в расшифровке названы выпавшие NSS и SES", "NSS" in missing and "SES" in missing,
      str(mixed_weights["BPI"]["inputs"]))

print("Нет реакций — прочерк у ER и ERR")
for variant, data in (
    ("нет колонок реакций", frame(["нейтральная"] * 4)),
    ("колонки пустые (NaN)", frame(["нейтральная"] * 4, likes=None, comments=None, reposts=None, engagement=None)),
    ("канонические нули после импорта", frame(["нейтральная"] * 4, likes=0, comments=0, reposts=0, engagement=0)),
    ("кадр дашборда",
     prepare_dashboard_messages(frame(["нейтральная"] * 4, likes=0, comments=0, reposts=0, engagement=0))),
):
    cards = compute_brand_metrics(data)
    for key in ("ER", "ERR"):
        check(f"{variant}: {key} недоступна", not cards[key]["available"], str(cards[key]["value"]))
        check(f"{variant}: {key} объясняет причину", "реакц" in cards[key]["reason"].lower(), cards[key]["reason"])
    check(f"{variant}: тональность при этом считается", cards["TVS"]["available"])

some = compute_brand_metrics(frame(["нейтральная"] * 4, likes=[0, 0, 0, 50], comments=0, reposts=0, engagement=0))
check("реакции хоть у одного сообщения — ER считается",
      some["ER"]["available"] and close(some["ER"]["value"], 50 / 20000 * 100, 0.01), str(some["ER"]["value"]))
fallback = compute_brand_metrics(frame(["нейтральная"] * 4, likes=0, comments=0, reposts=0, engagement=[5, 0, 0, 0]))
check("нули в лайках, но есть «Вовлечённость» — ER по ней", fallback["ER"]["available"], fallback["ER"]["reason"])

print("Таблица, CSV и динамика показывают пустое значение с причиной")
table = metrics_to_frame(compute_brand_metrics(unmarked))
tvs_row = table[table["Метрика"] == "ToneVolumeScore"].iloc[0]
check("в таблице значение пустое", pd.isna(tvs_row["Значение"]), str(tvs_row["Значение"]))
check("в статусе причина", "тональност" in str(tvs_row["Статус"]).lower(), str(tvs_row["Статус"]))
csv_text = table.to_csv(index=False)
check("в CSV нет «0.0» у ToneVolumeScore", "ToneVolumeScore,Тональность с учётом объёма,," in csv_text, csv_text)
dynamics = metrics_by_period(
    pd.concat([unmarked.assign(period_id="p1"), neutral.assign(period_id="p2")], ignore_index=True), ["p1", "p2"]
)
check("динамика: период без разметки пуст", pd.isna(dynamics.loc[0, "ToneVolumeScore"]),
      str(dynamics.to_dict("records")))
check("динамика: нейтральный период — ноль", close(dynamics.loc[1, "ToneVolumeScore"], 0.0),
      str(dynamics.to_dict("records")))

print("Источник SOV один для всех экранов")
# Раздел «Индексы бренда» брал бренды из тегов, а карточки для ИИ — только из
# загруженной категории: ИИ писал «доля голоса не посчитана» там, где на экране
# она была.
brand_map = {"own": ["Бренд А"], "competitors": ["Бренд Б"]}
tag_messages = frame(["нейтральная"] * 4, tags=["Бренд А", "Бренд А", "Бренд Б", "Прочее"])
from_tags = category_store.resolve_category_benchmark(tag_messages, brand_map, None)
check("без загруженной категории — бренды из тегов", bool(from_tags) and from_tags.get("source") == "project_tags",
      str(from_tags))
check("из тегов SOV считается", close(compute_sov(from_tags)["value"], 66.67), str(compute_sov(from_tags)["value"]))
uploaded_record = {
    "p1": {"own_brand": "Бренд А", "brands": [
        {"brand": "Бренд А", "messages": 10, "is_own": True},
        {"brand": "Бренд В", "messages": 30, "is_own": False},
    ]}
}
from_upload = category_store.resolve_category_benchmark(tag_messages, brand_map, uploaded_record)
check("загруженная категория главнее тегов", close(compute_sov(from_upload)["value"], 25.0),
      str(compute_sov(from_upload)["value"]))
empty_upload = {"p1": {"own_brand": "Бренд А", "brands": []}}
fallback_benchmark = category_store.resolve_category_benchmark(tag_messages, brand_map, empty_upload)
check("пустая загрузка не заслоняет теги", bool(fallback_benchmark)
      and fallback_benchmark.get("source") == "project_tags", str(fallback_benchmark))
check("без разметки и загрузки бенчмарка нет",
      category_store.resolve_category_benchmark(tag_messages, None, None) is None)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Все проверки индексов бренда пройдены.")
