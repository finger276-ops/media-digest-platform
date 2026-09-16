"""Аудитория считается по площадкам, а не по строкам.

Аудитория — свойство площадки: у поста и десяти комментариев под ним одно и то
же число подписчиков. Суммирование по строкам завышало её в 1,5–2,3 раза против
сводки Brand Analytics, а заказчик держит в руках отчёт BA и сверяет числа.

Обратная сторона тоже важна: выгрузки без колонок площадки (Медиалогия,
универсальный формат) не должны потерять аудиторию из-за дедупликации.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.metrics_compute import (  # noqa: E402
    audience_by_group,
    audience_place_key,
    audience_total,
    overview_metrics,
    prepare_dashboard_messages,
)

failures = []


def check(label, condition, detail=""):
    print(
        ("  ✓ " if condition else "  ✗ ")
        + label
        + (f" — {detail}" if detail and not condition else "")
    )
    if not condition:
        failures.append(label)


def msg(place="", place_title="", author="", author_url="", audience=0, **extra):
    row = {
        "chat_profile": place,
        "chat_title": place_title,
        "author": author,
        "author_profile": author_url,
        "audience": audience,
        "views": 0,
        "engagement": 0,
        "sentiment": "нейтральная",
    }
    row.update(extra)
    return row


print("1. Одна площадка считается один раз")
# Типовой случай из выгрузки: пост сообщества и два комментария под ним.
# У всех трёх строк одна аудитория — это подписчики сообщества, а не сумма.
same_place = pd.DataFrame(
    [
        msg(place="https://vk.com/club1", audience=5000),
        msg(place="https://vk.com/club1", audience=5000),
        msg(place="https://vk.com/club1", audience=5000),
    ]
)
check("три сообщения одной площадки дают её аудиторию", audience_total(same_place) == 5000,
      str(audience_total(same_place)))

print("2. Разные площадки складываются")
many = pd.DataFrame(
    [
        msg(place="https://vk.com/club1", audience=5000),
        msg(place="https://vk.com/club1", audience=5000),
        msg(place="https://t.me/channel", audience=1200),
        msg(place="https://ok.ru/group", audience=300),
    ]
)
check("три площадки сложились", audience_total(many) == 6500, str(audience_total(many)))

print("3. Берётся максимум, а не первое значение")
# Brand Analytics записывает аудиторию на момент публикации: у разных сообщений
# одной площадки числа расходятся. Занижать до первого попавшегося нельзя.
drifting = pd.DataFrame(
    [
        msg(place="https://vk.com/club1", audience=4800),
        msg(place="https://vk.com/club1", audience=5200),
        msg(place="https://vk.com/club1", audience=5000),
    ]
)
check("взят максимум по площадке", audience_total(drifting) == 5200, str(audience_total(drifting)))

print("4. Ключ площадки: адрес важнее названия")
# Названия сообществ совпадают и меняются между периодами, адрес — нет.
same_title = pd.DataFrame(
    [
        msg(place="https://vk.com/club1", place_title="Кровля", audience=5000),
        msg(place="https://vk.com/club2", place_title="Кровля", audience=3000),
    ]
)
check("одинаковые названия при разных адресах не слились",
      audience_total(same_title) == 8000, str(audience_total(same_title)))

fallback = pd.DataFrame(
    [
        msg(author="Иван", author_url="https://vk.com/ivan", audience=700),
        msg(author="Иван", author_url="https://vk.com/ivan", audience=700),
    ]
)
check("без площадки ключом становится профиль автора",
      audience_total(fallback) == 700, str(audience_total(fallback)))

by_name = pd.DataFrame(
    [
        msg(place_title="Кровля и фасады", audience=900),
        msg(place_title="Кровля и фасады", audience=900),
    ]
)
check("когда адресов нет, работает название площадки",
      audience_total(by_name) == 900, str(audience_total(by_name)))

print("5. Выгрузки без колонок площадки ничего не теряют")
# Медиалогия и универсальный формат: опознать площадку нечем. Каждая строка
# считается своей — это возврат к прежнему поведению, а не потеря данных.
anonymous = pd.DataFrame([{"audience": 100}, {"audience": 250}, {"audience": 50}])
check("аудитория сохранилась полностью", audience_total(anonymous) == 400,
      str(audience_total(anonymous)))

blank_keys = pd.DataFrame(
    [msg(audience=100), msg(audience=250)]
)
check("пустые ключи не слиплись в одну площадку", audience_total(blank_keys) == 350,
      str(audience_total(blank_keys)))

print("6. Пустые входные данные")
check("пустой кадр даёт ноль", audience_total(pd.DataFrame()) == 0)
check("None даёт ноль", audience_total(None) == 0)
check("кадр без аудитории даёт ноль",
      audience_total(pd.DataFrame([{"chat_profile": "https://vk.com/c"}])) == 0)

print("7. Метрика периода использует дедупликацию")
metrics = overview_metrics(prepare_dashboard_messages(many))
check("audience в overview_metrics сдедуплицирована", metrics["audience"] == 6500,
      str(metrics["audience"]))
check("сообщения считаются по-прежнему по строкам", metrics["messages"] == 4,
      str(metrics["messages"]))

print("8. Подготовленный кадр не меняет результат")
prepared = prepare_dashboard_messages(many)
check("колонка _audience_place добавлена", "_audience_place" in prepared.columns)
check("результат тот же, что без подготовки", audience_total(prepared) == audience_total(many),
      f"{audience_total(prepared)} против {audience_total(many)}")

print("9. Разрез по группам")
# Площадка, писавшая про два бренда, честно считается в обоих: так же устроены
# срезы Brand Analytics. Поэтому сумма по группам больше общей аудитории.
tagged = pd.DataFrame(
    [
        msg(place="https://vk.com/club1", audience=5000, tag="Docke"),
        msg(place="https://vk.com/club1", audience=5000, tag="Docke"),
        msg(place="https://vk.com/club1", audience=5000, tag="Tegola"),
        msg(place="https://t.me/channel", audience=1200, tag="Docke"),
    ]
)
grouped = audience_by_group(tagged, tagged["tag"])
check("внутри группы площадка одна", int(grouped["Docke"]) == 6200, str(grouped.to_dict()))
check("та же площадка попала и во вторую группу", int(grouped["Tegola"]) == 5000,
      str(grouped.to_dict()))
check("общая аудитория меньше суммы по группам",
      audience_total(tagged) == 6200 and grouped.sum() == 11200,
      f"общая {audience_total(tagged)}, по группам {grouped.sum()}")

print("10. Ключ площадки как таковой")
keys = audience_place_key(
    pd.DataFrame(
        [
            msg(place="https://vk.com/club1", place_title="Кровля", author="Иван"),
            msg(place_title="Кровля", author="Иван"),
            msg(author="Иван", author_url="https://vk.com/ivan"),
            msg(),
        ]
    )
)
check("первым берётся адрес площадки", keys.iloc[0] == "https://vk.com/club1", str(keys.iloc[0]))
check("затем профиль автора", keys.iloc[2] == "https://vk.com/ivan", str(keys.iloc[2]))
check("затем название площадки", keys.iloc[1] == "Кровля", str(keys.iloc[1]))
check("пустая строка получает собственный ключ", keys.iloc[3] not in set(keys.iloc[:3]),
      str(keys.iloc[3]))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Аудитория считается по площадкам.")
