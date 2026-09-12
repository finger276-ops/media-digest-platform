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

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Все проверки индексов бренда пройдены.")
