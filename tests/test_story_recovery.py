"""Восстановление сюжетов там, где Brand Analytics их не проставил.

Сюжетом размечена примерно четверть выгрузки, остальное сваливалось в один
псевдоповод. Восстановление идёт двумя приёмами: точным наследованием по
дословному совпадению текста и кластеризацией остатка с порогом по числу
авторов.

Порог именно по авторам, а не по числу сообщений: инфоповод — это когда о чём-то
пишут разные люди. Без него первым «инфоповодом» августовской выгрузки RUFLEX
становились 66 сообщений одного бота недвижимости.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.story_recovery import (  # noqa: E402
    ORIGIN_CLUSTERED,
    ORIGIN_INHERITED,
    ORIGIN_NONE,
    ORIGIN_SOURCE,
    recover_stories,
    recovery_counts,
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


def msg(text, story="", author="автор", kind="post", title=""):
    return {
        "text_clean": text,
        "source_main_topic": story,
        "author": author,
        "title": title,
        "message_type": {"post": "Пост", "repost": "Репост",
                         "comment": "Комментарий"}.get(kind, "Пост"),
        "platform_type": "Отзывы" if kind == "review" else "Соцсети",
    }


ROOF = "Компания открыла завод гибкой черепицы в Рязани, запуск линии состоялся"

print("1. Сюжет из выгрузки не переписывается никогда")
frame = pd.DataFrame([msg("Что-то своё", story="Запуск завода", author="а")])
rec = recover_stories(frame)
check("сюжет сохранён", rec.loc[0, "story"] == "Запуск завода", str(rec.loc[0, "story"]))
check("происхождение — выгрузка", rec.loc[0, "story_origin"] == ORIGIN_SOURCE)

print("2. Наследование по дословному совпадению")
frame = pd.DataFrame(
    [
        msg(ROOF, story="Запуск завода", author="СМИ"),
        msg(ROOF, author="Канал-1"),
        msg("  КОМПАНИЯ ОТКРЫЛА завод гибкой черепицы в Рязани, запуск линии состоялся ",
            author="Канал-2"),
        msg("Совсем другой текст про водостоки и фасады дома", author="Канал-3"),
    ]
)
rec = recover_stories(frame)
check("перепечатка получила сюжет донора", rec.loc[1, "story"] == "Запуск завода",
      str(rec.loc[1, "story"]))
check("происхождение — та же публикация", rec.loc[1, "story_origin"] == ORIGIN_INHERITED)
check(
    "регистр, пробелы и ё не мешают совпадению",
    rec.loc[2, "story"] == "Запуск завода",
    str(rec.loc[2, "story"]),
)
check("посторонний текст сюжет не получил", rec.loc[3, "story"] == "", str(rec.loc[3, "story"]))

print("3. Кластеризация: нужны разные авторы, а не разные публикации")
# Один магазин, двадцать объявлений — это не инфоповод. Ровно так выглядела
# крупнейшая «новость» августа до появления порога.
spam = pd.DataFrame(
    [msg(f"Огромный выбор гибкой черепицы всё в наличии, доставим по России {i}",
         author="Магазин") for i in range(8)]
)
rec = recover_stories(spam, min_authors=3)
check(
    "одноавторская рассылка сюжетом не стала",
    set(rec["story_origin"]) == {ORIGIN_NONE},
    str(dict(rec["story_origin"].value_counts())),
)

crowd = pd.DataFrame(
    [
        msg("На фестивале архитекторов обсудили кровельные решения и фасады", author="а"),
        msg("Фестиваль архитекторов: обсудили кровельные решения и фасады домов", author="б"),
        msg("Архитекторы на фестивале обсудили кровельные решения, фасады", author="в"),
    ]
)
rec = recover_stories(crowd, min_authors=3)
check(
    "три автора об одном — это сюжет",
    set(rec["story_origin"]) == {ORIGIN_CLUSTERED},
    str(dict(rec["story_origin"].value_counts())),
)
check(
    "у всех троих один непустой сюжет",
    rec["story"].nunique() == 1 and bool(str(rec.loc[0, "story"]).strip()),
    str(list(rec["story"])),
)

print("4. Порог по авторам настраивается")
rec = recover_stories(crowd, min_authors=5)
check(
    "при пороге 5 авторов эти трое не проходят",
    set(rec["story_origin"]) == {ORIGIN_NONE},
    str(dict(rec["story_origin"].value_counts())),
)

print("5. Отзывы и комментарии в сюжеты не собираются")
# Это реакция на чужую публикацию, а не событие. Негатив из отзывов должен
# попасть в раздел репутации товара, а не в ленту инфоповодов.
reactions = pd.DataFrame(
    [
        msg("Товар пришёл повреждённым, упаковка разорвана полностью", author="а", kind="review"),
        msg("Товар пришёл повреждённым, упаковка разорвана совершенно", author="б", kind="review"),
        msg("Товар пришёл повреждённым, упаковка была разорвана", author="в", kind="review"),
        msg("Согласен с автором полностью, кровля отличная вещь", author="г", kind="comment"),
        msg("Согласен с автором целиком, кровля отличная вещь", author="д", kind="comment"),
        msg("Согласен с автором, кровля отличная вещь совсем", author="е", kind="comment"),
    ]
)
rec = recover_stories(reactions, min_authors=3)
check(
    "ни отзывы, ни комментарии сюжетом не стали",
    set(rec["story_origin"]) == {ORIGIN_NONE},
    str(dict(rec["story_origin"].value_counts())),
)

print("6. Заголовок предпочитается первой строке текста")
titled = pd.DataFrame(
    [
        msg("Сегодня расскажем о том, как устроена вентиляция подкровельного пространства",
            author="а", title="Анализ рынка вентиляции кровли"),
        msg("Сегодня расскажем, как устроена вентиляция подкровельного пространства дома",
            author="б", title="Анализ рынка вентиляции кровли"),
        msg("Сегодня расскажем про то, как устроена вентиляция подкровельного пространства",
            author="в", title="Анализ рынка вентиляции кровли"),
    ]
)
rec = recover_stories(titled, min_authors=3)
check(
    "названием стал заголовок выгрузки",
    rec.loc[0, "story"] == "Анализ рынка вентиляции кровли",
    str(rec.loc[0, "story"]),
)

print("7. Название не превращается в приветствие")
# «Добрый день!» ровно так и попало в сюжеты при первом прогоне на RUFLEX.
greeting = pd.DataFrame(
    [
        msg("Добрый день! Открыли новый склад кровельных материалов в Рязани", author="а"),
        msg("Добрый день! Открыли новый склад кровельных материалов в Рязань", author="б"),
        msg("Добрый день! Открыли новый склад кровельных материалов в Рязани!", author="в"),
    ]
)
rec = recover_stories(greeting, min_authors=3)
title = str(rec.loc[0, "story"])
check("приветствие не стало названием", title.strip().lower() != "добрый день!", title)
check("в названии есть суть", "склад" in title.lower(), title)

print("8. Пустые и вырожденные входные данные")
check("пустой кадр", len(recover_stories(pd.DataFrame())) == 0)
check("None", len(recover_stories(None)) == 0)
single = pd.DataFrame([msg("Одно сообщение про кровлю и фасады", author="а")])
rec = recover_stories(single, min_authors=3)
check("одно сообщение сюжетом не становится", rec.loc[0, "story_origin"] == ORIGIN_NONE,
      str(rec.loc[0, "story_origin"]))
blank = pd.DataFrame([msg("", author="а"), msg("", author="б"), msg("", author="в")])
rec = recover_stories(blank, min_authors=3)
check("пустые тексты не сцепляются в один сюжет",
      set(rec["story_origin"]) == {ORIGIN_NONE},
      str(dict(rec["story_origin"].value_counts())))

print("9. Выгрузка без сюжетов вовсе")
# Медиалогия и универсальный формат: доноров для наследования нет, но
# кластеризация обязана работать.
no_source = pd.DataFrame(
    [
        msg("Открылся завод по производству гибкой черепицы в городе", author="а"),
        msg("Открылся завод по производству гибкой черепицы в городах", author="б"),
        msg("Открылся завод по производству гибкой черепицы в городке", author="в"),
    ]
)
rec = recover_stories(no_source, min_authors=3)
check("сюжеты собрались без единого донора",
      set(rec["story_origin"]) == {ORIGIN_CLUSTERED},
      str(dict(rec["story_origin"].value_counts())))

print("10. Сводка по происхождению")
mixed = pd.DataFrame(
    [
        msg(ROOF, story="Запуск завода", author="СМИ"),
        msg(ROOF, author="Канал"),
        msg("Одиночное сообщение ни на что не похожее совершенно", author="я"),
    ]
)
counts = recovery_counts(recover_stories(mixed))
check(
    "посчитано по способам",
    counts[ORIGIN_SOURCE] == 1 and counts[ORIGIN_INHERITED] == 1 and counts[ORIGIN_NONE] == 1,
    str(counts),
)
check("пустая сводка не падает", recovery_counts(pd.DataFrame())[ORIGIN_SOURCE] == 0)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Восстановление сюжетов работает.")
