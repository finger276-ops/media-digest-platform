"""Репутация товара: отзывы покупателей, оценки и претензии.

Раздел появился из наблюдения на реальных выгрузках: почти весь негатив периода
лежит в отзывах на маркетплейсах — 18 из 20 за июль и 11 из 13 за август.
В ленте инфоповодов его не видно и не может быть видно.

Отзывы приходят по шаблону («Плюсы товара», «Недостатки», «Комментарий»), и
разбор шаблона — суть модуля: без него похвала и претензия из одного отзыва
слипаются в одну строку.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.reviews import (  # noqa: E402
    complaints,
    parse_review,
    parse_reviews,
    praise_phrases,
    rating_values,
    review_overview,
    review_rows,
    reviews_by_product,
    select_reviews,
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


def review(text, rating="", title="Гибкая черепица 3м2", sentiment="нейтральная"):
    return {
        "text_clean": text,
        "rating": rating,
        "title": title,
        "sentiment": sentiment,
        "is_negative": "негатив" in sentiment,
        "platform_type": "Отзывы",
        "message_type": "Комментарий",
        "message_link": "https://wildberries.ru/1",
        "date": "24.04.2026",
    }


def post(text):
    return {
        "text_clean": text,
        "rating": "",
        "title": "",
        "sentiment": "нейтральная",
        "is_negative": False,
        "platform_type": "Соцсети",
        "message_type": "Пост",
        "message_link": "",
        "date": "24.04.2026",
    }


print("1. Разбор шаблона отзыва")
parsed = parse_review(
    "Плюсы товара: хорошее качество, внешний вид "
    "Недостатки: слабая клейкость "
    "Комментарий: часть гонтов сломана"
)
check("плюсы выделены", parsed["pros"] == "хорошее качество, внешний вид", parsed["pros"])
check("недостатки выделены", parsed["cons"] == "слабая клейкость", parsed["cons"])
check("комментарий выделен", parsed["comment"] == "часть гонтов сломана", parsed["comment"])

check(
    "«Достоинства» — тоже плюсы",
    parse_review("Достоинства: Хорошие гвозди")["pros"] == "Хорошие гвозди",
    str(parse_review("Достоинства: Хорошие гвозди")),
)
# Примерно каждый четвёртый отзыв приходит без единого маркера — потерять его
# нельзя, иначе претензия просто исчезнет.
free = parse_review("Пришло в рваном грязном пакете, все гонты сломаны")
check(
    "отзыв без маркеров целиком идёт в комментарий",
    free["comment"] == "Пришло в рваном грязном пакете, все гонты сломаны",
    str(free),
)
check("пустой текст не падает", parse_review("") == {"pros": "", "cons": "", "comment": ""})
check("None не падает", parse_review(None)["comment"] == "")
head = parse_review("Свободный текст сначала. Недостатки: мало клея")
check("текст до первого маркера не теряется", head["comment"] == "Свободный текст сначала.", str(head))
check("и маркер после него разобран", head["cons"] == "мало клея", str(head))

print("2. Карточка товара не попадает в претензию")
# Для Brand Analytics заголовок приклеен к тексту сообщения. Без чистки
# претензией покупателя оказывается название товара.
frame = pd.DataFrame(
    [
        review(
            "Гибкая черепица 3м2 Недостатки: вся крошка сыпется",
            rating="1",
            title="Гибкая черепица 3м2",
            sentiment="негативная",
        )
    ]
)
parsed_frame = parse_reviews(frame)
check(
    "название товара срезано с начала",
    parsed_frame.iloc[0]["cons"] == "вся крошка сыпется",
    str(parsed_frame.iloc[0].to_dict()),
)

print("3. Отбор отзывов")
mixed = pd.DataFrame(
    [review("Плюсы товара: качество", rating="5"), post("Новость рынка кровли")]
)
check("посты в отзывы не попадают", len(select_reviews(mixed)) == 1, str(len(select_reviews(mixed))))
check("пустой кадр не падает", select_reviews(pd.DataFrame()).empty)

print("4. Оценки")
ratings = pd.DataFrame(
    [
        review("а", rating="5"),
        review("б", rating="4,8"),  # запятая как разделитель
        review("в", rating=""),
        review("г", rating="0"),  # вне диапазона 1–5
        review("д", rating="хорошо"),
    ]
)
values = rating_values(ratings)
check("целая оценка разобрана", float(values.iloc[0]) == 5.0, str(values.iloc[0]))
check("дробная оценка через запятую разобрана", abs(float(values.iloc[1]) - 4.8) < 1e-9, str(values.iloc[1]))
check("пустая оценка — не ноль, а отсутствие", pd.isna(values.iloc[2]), str(values.iloc[2]))
check("ноль отброшен как выход за диапазон", pd.isna(values.iloc[3]), str(values.iloc[3]))
check("нечисловая оценка отброшена", pd.isna(values.iloc[4]), str(values.iloc[4]))

print("5. Сводка периода")
period = pd.DataFrame(
    [
        review("Плюсы товара: хорошее качество", rating="5"),
        review("Плюсы товара: хорошее качество, внешний вид", rating="5"),
        review("Недостатки: слабая клейкость", rating="2", sentiment="негативная"),
        review("Недостатки: доставка", rating="3", title="Гвозди кровельные"),
        post("Новость рынка"),
    ]
)
ov = review_overview(period)
check("посты в сводку не попали", ov["reviews"] == 4, str(ov["reviews"]))
check("средняя оценка посчитана", abs(ov["rating_avg"] - 3.75) < 1e-9, str(ov["rating_avg"]))
check("распределение оценок собрано", ov["rating_counts"] == {5: 2, 2: 1, 3: 1}, str(ov["rating_counts"]))
check("товаров посчитано два", ov["products"] == 2, str(ov["products"]))
# Претензия — это низкая оценка ИЛИ негативная тональность. Одного признака
# мало: на выгрузках RUFLEX они расходились в обе стороны.
check("три звезды считаются претензией без разметки тональности", ov["negative"] == 2, str(ov["negative"]))
tone_only = pd.DataFrame([review("Плохо", rating="5", sentiment="негативная")])
check(
    "негативная разметка при высокой оценке тоже претензия",
    review_overview(tone_only)["negative"] == 1,
    str(review_overview(tone_only)),
)
check("пустой период даёт нули", review_overview(pd.DataFrame())["reviews"] == 0)

print("6. Претензии показываются поштучно")
rows = complaints(period)
check("две претензии", len(rows) == 2, str(len(rows)))
check("сначала самая низкая оценка", float(rows.iloc[0]["Оценка"]) == 2.0, str(rows.iloc[0].to_dict()))
check(
    "в претензии суть, а не весь отзыв",
    rows.iloc[0]["Претензия"] == "слабая клейкость",
    str(rows.iloc[0]["Претензия"]),
)
check("товар указан", rows.iloc[0]["Товар"] == "Гибкая черепица 3м2", str(rows.iloc[0]["Товар"]))
check("период без претензий даёт пустую таблицу",
      complaints(pd.DataFrame([review("Плюсы товара: качество", rating="5")])).empty)

print("7. Плюсы складываются в счёт, недостатки — нет")
# Плюсы на маркетплейсе выбираются галочками из готового списка, поэтому
# частотный счёт по ним осмыслен. Недостатки пишутся свободным текстом.
praise = dict(praise_phrases(period, top_n=5))
check("повторяющийся плюс посчитан дважды", praise.get("хорошее качество") == 2, str(praise))
check("одиночный плюс тоже в списке", praise.get("внешний вид") == 1, str(praise))
check("недостатки в список плюсов не попали", "слабая клейкость" not in praise, str(praise))
check("пустой период не падает", praise_phrases(pd.DataFrame()) == [])

print("8. Разрез по товарам")
products = reviews_by_product(period)
check("два товара", len(products) == 2, str(products.to_dict("records")))
check(
    "сначала товар с претензиями",
    int(products.iloc[0]["Претензий"]) >= int(products.iloc[-1]["Претензий"]),
    str(products.to_dict("records")),
)
check(
    "средняя оценка по товару посчитана",
    abs(float(products[products["Товар"] == "Гвозди кровельные"].iloc[0]["Средняя оценка"]) - 3.0) < 1e-9,
    str(products.to_dict("records")),
)
no_title = reviews_by_product(pd.DataFrame([review("Отзыв", rating="5", title="")]))
check(
    "отзыв без товара не теряется",
    len(no_title) == 1 and no_title.iloc[0]["Товар"] == "Товар не указан",
    str(no_title.to_dict("records")),
)

print("8.5. Полный список отзывов, а не только претензии")
# Поштучно раздел показывал лишь претензии. Когда их ноль, блок выглядел пустым
# при непустом счётчике: «Отзывов 14», а прочитать их негде.
rows = review_rows(period)
check("в список попали все отзывы, посты — нет", len(rows) == 4, str(len(rows)))
check(
    "сначала худшая оценка",
    float(rows.iloc[0]["Оценка"]) == 2.0,
    str(rows["Оценка"].tolist()),
)
check(
    "плюсы и минусы не теряются, а собираются в одну строку",
    "Плюсы: хорошее качество, внешний вид" in rows["Отзыв"].tolist(),
    str(rows["Отзыв"].tolist()),
)
only_positive = pd.DataFrame([review("Плюсы товара: качество", rating="5")])
check(
    "отзыв без претензий всё равно виден в списке",
    len(review_rows(only_positive)) == 1,
    str(review_rows(only_positive).to_dict("records")),
)
# Ровно случай владельца: отзывы есть, оценок нет — раздел обязан их показать.
no_rating = pd.DataFrame(
    [review("Пришло вовремя, качество среднее", rating=""), review("Без оценки", rating="")]
)
no_rating_rows = review_rows(no_rating)
check(
    "отзывы без оценки не выпадают из списка",
    len(no_rating_rows) == 2,
    str(no_rating_rows.to_dict("records")),
)
check(
    "у отзыва без оценки в списке пусто, а не ноль",
    not no_rating_rows.empty and pd.isna(no_rating_rows.iloc[0]["Оценка"]),
    str(no_rating_rows["Оценка"].tolist()),
)
check("пустой период не падает", review_rows(pd.DataFrame()).empty)

print("8.7. Источник товара: отдельная колонка главнее заголовка, но пустая не в счёт")
# Импорт заводит «Товар» всегда, даже когда в файле такой колонки не было.
# Если брать первую существующую, пустой «Товар» заслонит «Заголовок», где у
# Brand Analytics и лежит название товара.
with_empty_product = pd.DataFrame(
    [dict(review("Отзыв", rating="5", title="Гибкая черепица 3м2"), product="")]
)
check(
    "пустая колонка товара не заслоняет заголовок",
    reviews_by_product(with_empty_product).iloc[0]["Товар"] == "Гибкая черепица 3м2",
    str(reviews_by_product(with_empty_product).to_dict("records")),
)
with_product = pd.DataFrame(
    [
        dict(
            review("Отзыв", rating="5", title="Карточка на маркетплейсе"),
            product="Гвозди кровельные",
        )
    ]
)
check(
    "заполненная колонка товара главнее заголовка",
    reviews_by_product(with_product).iloc[0]["Товар"] == "Гвозди кровельные",
    str(reviews_by_product(with_product).to_dict("records")),
)

print("9. Выгрузка без признака отзывов")
# Медиалогия и универсальный формат не отдают тип площадки: раздел просто
# останется пустым, но ничего не сломает и не выдумает.
plain = pd.DataFrame([{"text_clean": "Обычный пост", "message_type": "Пост"}])
check("отзывов не найдено", select_reviews(plain).empty)
check("сводка пустая, но корректная", review_overview(plain)["reviews"] == 0)
check("претензий нет", complaints(plain).empty)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Раздел отзывов работает.")
