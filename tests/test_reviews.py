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
    _clean_medialogia_title,
    business_reply_rows,
    complaints,
    parse_review,
    parse_reviews,
    praise_phrases,
    rating_values,
    review_overview,
    review_rows,
    reviews_by_product,
    select_business_replies,
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

print("10. Медиалогия: товар склеен с оценкой и текстом отзыва в заголовке")
# У Медиалогии нет отдельной колонки товара для отзывов с Wildberries/RuStore/
# Otzovik и т.п. — название приезжает в «Заголовке» склеенным с меткой отзыва
# и самой оценкой: «Отзыв о Фонбет – ставки на спорт Оценка: 1 из 5 Не
# загружается видео». Без очистки это целиком показывалось как «Товар».
check(
    "'Отзыв о X Оценка: N из 5 ...' — оставлен только товар",
    _clean_medialogia_title(
        "Отзыв о Фонбет – ставки на спорт Оценка: 1 из 5 Не загружается видео с матча"
    )
    == "Фонбет – ставки на спорт",
)
check(
    "'Ответ на отзыв о X Оценка: N из 5 ...' — тоже только товар",
    _clean_medialogia_title(
        "Ответ на отзыв о Winline: ставки на спорт Оценка: 5 из 5 Спасибо за отзыв!"
    )
    == "Winline: ставки на спорт",
)
check(
    "'Отзыв: X - текст' (Otzovik) — товар до первого ' - '",
    _clean_medialogia_title(
        'Отзыв: Крем EISENBERG для лица - вау эффекта не получила'
    )
    == "Крем EISENBERG для лица",
)
check(
    "заголовок без узнаваемой склейки не трогаем",
    _clean_medialogia_title("Кидалово") == "Кидалово",
)
check(
    "уже чистое название (Brand Analytics) не трогаем",
    _clean_medialogia_title("DOCKE / Гибкая черепица мягкая кровля для крыши дома Серый 3м2")
    == "DOCKE / Гибкая черепица мягкая кровля для крыши дома Серый 3м2",
)
check("пустой заголовок не падает", _clean_medialogia_title("") == "")
check("None не падает", _clean_medialogia_title(None) == "")

medialogia_period = pd.DataFrame(
    [
        review(
            "Не загружается видео с матча КХЛ!",
            rating="1",
            title="Отзыв о Фонбет – ставки на спорт Оценка: 1 из 5 Не загружается видео с матча КХЛ!",
            sentiment="негативная",
        ),
        review(
            "Всё работает",
            rating="5",
            title="Отзыв о Winline: ставки на спорт Оценка: 5 из 5 Всё работает",
        ),
    ]
)
check(
    "'Товар' в разрезе по товарам очищен от оценки и текста",
    set(reviews_by_product(medialogia_period)["Товар"])
    == {"Фонбет – ставки на спорт", "Winline: ставки на спорт"},
    str(reviews_by_product(medialogia_period).to_dict("records")),
)
check(
    "'Товар' в претензиях очищен",
    complaints(medialogia_period).iloc[0]["Товар"] == "Фонбет – ставки на спорт",
    str(complaints(medialogia_period).to_dict("records")),
)
check(
    "'Товар' в общем списке отзывов очищен",
    set(review_rows(medialogia_period)["Товар"])
    == {"Фонбет – ставки на спорт", "Winline: ставки на спорт"},
    str(review_rows(medialogia_period)["Товар"].tolist()),
)

print("11. Ответы продавцов и разработчиков — не отзывы покупателей")
# В выгрузке Медиалогии по букмекерам 70 «отзывов» из 122 оказались ответами
# продавцов и разработчиков: раздел показывал 122 отзыва и 56 товаров вместо 52
# и 20, а три ответа поддержки с негативной разметкой попадали в претензии.
# Мутационные проверки: без признака по типу роняются «Ответ» и «Answer»; без
# признака по заголовку — «ответ с типом Комментарий»; \b вместо явного конца
# слова — все проверки по кириллице (в pandas 3 это pyarrow, а там \b латинский);
# без конца слова вообще — «Ответственный».


def reply(text, *, message_type="Ответ", title="Ответ на отзыв о Гибкая черепица 3м2",
          sentiment="нейтральная", author="Продавец"):
    row = review(text, title=title, sentiment=sentiment)
    row.update({"message_type": message_type, "author": author})
    return row


replies_period = pd.DataFrame(
    [
        review("Недостатки: Пришло в рваном пакете", rating="1", sentiment="негативная"),
        review("Плюсы товара: качество", rating="5", title="Мягкая кровля для беседки"),
        # Ответ поддержки с негативной разметкой: раньше он становился претензией.
        reply("Сожалеем, что пришлось столкнуться с подобным", sentiment="негативная",
              title="Ответ на отзыв о Кровля Про"),
        reply("Спасибо за отзыв!", message_type="Answer", title="Кровля Про"),
        # Только тип, заголовок обычный: так ответ узнаётся лишь по кириллице.
        reply("Благодарим за выбор", message_type="Ответ", title="Черепица Люкс"),
        # Тип не заполнен как надо, но заголовок выдаёт ответ.
        reply("Рады, что понравилось", message_type="Комментарий"),
        # Похоже по началу, но это не ответ.
        review("Ответственный продавец, всё пришло", rating="5", title="Гвозди кровельные")
        | {"message_type": "Ответственный отзыв"},
        post("Новость рынка кровли"),
    ]
)
ov = review_overview(replies_period)
check("в счёт отзывов идут только покупатели", ov["reviews"] == 3, str(ov))
check("ответы посчитаны отдельно", ov["replies"] == 4, str(ov))
check(
    "ответ поддержки с негативом — не претензия",
    ov["negative"] == 1 and len(complaints(replies_period)) == 1,
    str(complaints(replies_period).to_dict("records")),
)
check(
    "товар из ответа не добавляет товаров в разрез",
    ov["products"] == 3 and "Кровля Про" not in set(reviews_by_product(replies_period)["Товар"]),
    str(reviews_by_product(replies_period).to_dict("records")),
)
check("в общем списке отзывов ответов нет", len(review_rows(replies_period)) == 3)
check(
    "«Ответственный …» — не ответ",
    "Гвозди кровельные" in set(review_rows(replies_period)["Товар"]),
    str(review_rows(replies_period)["Товар"].tolist()),
)
check(
    "ответы выделены целиком: по типу «Ответ», «Answer» и по заголовку",
    len(select_business_replies(replies_period)) == 4,
    str(select_business_replies(replies_period)[["message_type", "title"]].to_dict("records")),
)
reply_rows = business_reply_rows(replies_period)
check(
    "в списке ответов видно, кто ответил, товар и сам ответ",
    list(reply_rows.columns) == ["Кто ответил", "Товар", "Ответ", "Дата", "Ссылка"]
    and reply_rows.iloc[0]["Кто ответил"] == "Продавец"
    and reply_rows.iloc[0]["Товар"] == "Кровля Про"
    and reply_rows.iloc[0]["Ответ"] == "Сожалеем, что пришлось столкнуться с подобным",
    str(reply_rows.to_dict("records")),
)
only_replies = pd.DataFrame([reply("Спасибо за отзыв!"), post("Новость")])
check(
    "период только с ответами: отзывов ноль, ответы видны",
    select_reviews(only_replies).empty
    and review_overview(only_replies)["reviews"] == 0
    and review_overview(only_replies)["replies"] == 1,
    str(review_overview(only_replies)),
)
check("без ответов список ответов пустой", business_reply_rows(period).empty)

print("12. Шапка Медиалогии не съедает текст отзыва")
# Медиалогия начинает текст отзыва из магазина приложений шапкой: «Отзыв о X»,
# «Оценка: N из 5» — отдельными строками. Заголовок — это та же шапка вместе с
# текстом в одну строку, и срезание заголовка с начала текста стирало отзыв
# целиком: в выгрузке по букмекерам пустыми были 31 отзыв из 52 и 7 претензий
# из 11. Мутационные проверки: без разбора шапки роняются «текст отзыва» и
# «претензия»; без пропуска строки с оценкой — «строка с оценкой не в тексте»;
# товар из заголовка вместо первой строки — «товар ответа из первой строки».
rustore = pd.DataFrame(
    [
        review(
            "Отзыв о Фонбет – ставки на спорт\nОценка: 1 из 5\nНе загружается видео с матча КХЛ!",
            rating="1",
            title="Отзыв о Фонбет – ставки на спорт Оценка: 1 из 5 Не загружается видео с матча КХЛ!",
            sentiment="негативная",
        ),
        review(
            "Отзыв о Фонбет – ставки на спорт\nОценка: 5 из 5",
            rating="5",
            title="Отзыв о Фонбет – ставки на спорт Оценка: 5 из 5",
        ),
        reply(
            "Ответ на отзыв о BETBOOM — ставки на спорт\nПривет! Напиши, пожалуйста, на почту",
            title="Ответ на отзыв о BETBOOM — ставки на спорт Привет! Напиши, пожалуйста, на почту",
            author="BETBOOM — ставки на спорт",
        ),
    ]
)
rows12 = review_rows(rustore)
check(
    "текст отзыва на месте",
    "Не загружается видео с матча КХЛ!" in set(rows12["Отзыв"]),
    str(rows12["Отзыв"].tolist()),
)
check(
    "строка с оценкой не в тексте",
    not any("Оценка:" in str(t) for t in rows12["Отзыв"]),
    str(rows12["Отзыв"].tolist()),
)
check(
    "отзыв из одной оценки остаётся без текста, а не с шапкой",
    "" in set(rows12["Отзыв"]),
    str(rows12["Отзыв"].tolist()),
)
claims12 = complaints(rustore)
check(
    "претензия не пустая",
    len(claims12) == 1 and claims12.iloc[0]["Претензия"] == "Не загружается видео с матча КХЛ!",
    str(claims12.to_dict("records")),
)
rr12 = business_reply_rows(rustore)
check(
    "товар ответа из первой строки, а не весь заголовок",
    len(rr12) == 1 and rr12.iloc[0]["Товар"] == "BETBOOM — ставки на спорт",
    str(rr12.to_dict("records")),
)
check(
    "текст ответа без шапки",
    len(rr12) == 1 and rr12.iloc[0]["Ответ"] == "Привет! Напиши, пожалуйста, на почту",
    str(rr12.to_dict("records")),
)
inline = pd.DataFrame(
    [
        review(
            "Отзыв о Фонбет – ставки на спорт Оценка: 2 из 5 Вылетает при входе",
            rating="2",
            title="Отзыв о Фонбет – ставки на спорт Оценка: 2 из 5 Вылетает при входе",
        )
    ]
)
check(
    "шапка в одну строку (переносы потерялись) тоже срезается",
    review_rows(inline).iloc[0]["Отзыв"] == "Вылетает при входе",
    str(review_rows(inline).to_dict("records")),
)

print("13. Ответы в Brand Analytics, App Store и крайние случаи шапки")
# Brand Analytics ставит ответу продавца тот же тип «Комментарий», что и отзыву:
# в выгрузке Knauf так пришли 598 «отзывов» из 1208. На Wildberries автор —
# «Ответ представителя», на Озоне заголовок «Комментарий к отзыву о …» или
# «Ответ на вопрос о "…"...». Заголовок BA приклеен к тексту первой строкой.
# Мутационные проверки: без признака по автору роняется «WB»; без новых
# заголовков — «Озон»; без снятия кавычек — «товар без кавычек»; без
# подтверждения шапки — «Отзыв о доставке»; без заголовка-фразы App Store —
# «Кидалово в тексте»; без запасного пути для ответа в одну строку — «ответ в
# одну строку»; без хвоста « - ответ» — «карты».
ba = pd.DataFrame(
    [
        reply(
            "ТЕХНОНИКОЛЬ / Утеплитель LOGICPIR 30мм\nЗдравствуйте! Спасибо за отзыв.",
            message_type="Комментарий",
            title="ТЕХНОНИКОЛЬ / Утеплитель LOGICPIR 30мм",
            author="Ответ представителя",
        ),
        reply(
            "Комментарий к отзыву о Утеплитель Vetonit 50 мм\nДобрый день! Благодарим за отзыв",
            message_type="Комментарий",
            title="Комментарий к отзыву о Утеплитель Vetonit 50 мм",
            author="Ozon.ru - Онлайн-мегамаркет",
        ),
        reply(
            'Ответ на вопрос о "Техноплекс 30 мм (12 упаковок)"...\nЗдравствуйте, изготовлен из XPS',
            message_type="Комментарий",
            title='Ответ на вопрос о "Техноплекс 30 мм (12 упаковок)"...',
            author="Ozon.ru - Онлайн-мегамаркет",
        ),
        review(
            "ТЕХНОНИКОЛЬ / Утеплитель LOGICPIR 30мм\nНедостатки: крошится",
            rating="2",
            title="ТЕХНОНИКОЛЬ / Утеплитель LOGICPIR 30мм",
            sentiment="негативная",
        ),
    ]
)
ba_replies = business_reply_rows(ba)
check("WB: «Ответ представителя» — ответ", len(select_reviews(ba)) == 1, str(select_reviews(ba)[["author", "title"]].to_dict("records")))
check(
    "Озон: «Комментарий к отзыву о» и «Ответ на вопрос о» — ответы",
    len(ba_replies) == 3,
    str(ba_replies.to_dict("records")),
)
check(
    "у ответа BA текст без карточки товара",
    "Здравствуйте! Спасибо за отзыв." in set(ba_replies["Ответ"]),
    str(ba_replies["Ответ"].tolist()),
)
check(
    "товар ответа Озона без приставки и без кавычек",
    set(ba_replies["Товар"])
    == {"ТЕХНОНИКОЛЬ / Утеплитель LOGICPIR 30мм", "Утеплитель Vetonit 50 мм", "Техноплекс 30 мм (12 упаковок)"},
    str(ba_replies["Товар"].tolist()),
)

app_store = pd.DataFrame(
    [
        review(
            "Отзыв о Фонбет – ставки на спорт\nОценка: 1 из 5\nДаете рекламу и кидаете",
            rating="1",
            title="Кидалово",
            sentiment="негативная",
        ),
        review(
            "Отзыв о доставке: привезли на неделю позже\nКлей не держит",
            rating="1",
            title="Гибкая черепица 3м2",
        ),
    ]
)
app_rows = review_rows(app_store).set_index("Оценка", drop=False)
check(
    "App Store: товар из шапки, а не заголовок покупателя",
    "Фонбет – ставки на спорт" in set(reviews_by_product(app_store)["Товар"])
    and "Кидалово" not in set(reviews_by_product(app_store)["Товар"]),
    str(reviews_by_product(app_store).to_dict("records")),
)
check(
    "App Store: заголовок покупателя остаётся в тексте",
    "Кидалово. Даете рекламу и кидаете" in set(review_rows(app_store)["Отзыв"]),
    str(review_rows(app_store)["Отзыв"].tolist()),
)
check(
    "«Отзыв о доставке: …» без оценки и без такого заголовка — не шапка, строка на месте",
    "Отзыв о доставке: привезли на неделю позже Клей не держит" in set(review_rows(app_store)["Отзыв"]),
    str(review_rows(app_store)["Отзыв"].tolist()),
)
one_line_reply = pd.DataFrame(
    [
        reply(
            "Ответ на отзыв о BETBOOM — ставки на спорт Привет! Напиши на почту",
            title="Ответ на отзыв о BETBOOM — ставки на спорт Привет! Напиши на почту",
        )
    ]
)
check(
    "ответ в одну строку не остаётся пустым",
    business_reply_rows(one_line_reply).iloc[0]["Ответ"]
    == "BETBOOM — ставки на спорт Привет! Напиши на почту",
    str(business_reply_rows(one_line_reply).to_dict("records")),
)
maps = pd.DataFrame(
    [reply("Спасибо, что выбрали нас", title="Шоколадница, кофейня, Советская площадь, 5 - ответ")]
)
check(
    "карты: у товара ответа нет хвоста « - ответ»",
    business_reply_rows(maps).iloc[0]["Товар"] == "Шоколадница, кофейня, Советская площадь, 5",
    str(business_reply_rows(maps).to_dict("records")),
)

print("14. Раздел «Отзывы»: ответы продавцов показаны отдельно")
from streamlit.testing.v1 import AppTest  # noqa: E402


def _reviews_app():
    import streamlit as st

    from reviews_ui import render_reviews

    render_reviews(st.session_state["frame"])


def _render(frame):
    app = AppTest.from_function(_reviews_app, default_timeout=60)
    app.session_state["frame"] = frame
    return app.run()


app = _render(replies_period)
check("раздел с ответами открылся без исключений", not app.exception, str(app.exception))
cards = {str(m.label): str(m.value) for m in app.metric}
check("в карточке только отзывы покупателей", cards.get("Отзывов") == "3", str(cards))
check("претензия от ответа поддержки не добавилась", cards.get("Претензий") == "1", str(cards))
captions = [str(c.value) for c in app.caption]
check(
    "под карточками сказано, что ответы не считаются",
    any("Ответы продавцов на отзывы (4)" in c and "не входят" in c and "внизу раздела" in c for c in captions),
    str(captions),
)
blocks = [str(m.value) for m in app.markdown]
check(
    "внизу отдельный блок ответов",
    any("Ответы продавцов на отзывы (4)" in b for b in blocks),
    str(blocks),
)
check(
    "ответы таблицей с автором",
    any("Кто ответил" in list(getattr(d.value, "columns", [])) for d in app.dataframe),
    str([list(getattr(d.value, "columns", [])) for d in app.dataframe]),
)

app = _render(only_replies)
check("период только с ответами не падает", not app.exception, str(app.exception))
check(
    "сказано, что отзывов покупателей нет, а ответы есть",
    any("отзывов покупателей не найдено" in str(i.value) for i in app.info),
    str([str(i.value) for i in app.info]),
)
check(
    "ответы всё равно показаны",
    any("Кто ответил" in list(getattr(d.value, "columns", [])) for d in app.dataframe),
    str([list(getattr(d.value, "columns", [])) for d in app.dataframe]),
)

app = _render(period)
check("без ответов раздел как раньше", not app.exception, str(app.exception))
check(
    "без ответов нет ни подписи, ни блока ответов",
    not any("Ответы продавцов" in str(c.value) for c in app.caption)
    and not any("Ответы продавцов" in str(m.value) for m in app.markdown),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Раздел отзывов работает.")
