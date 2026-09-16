"""Природа сообщения: отзыв, комментарий, репост, публикация.

Три четверти сообщений в выгрузке Brand Analytics приходят без сюжета и раньше
сваливались в один псевдоповод. Корзина неоднородна: отзыв покупателя на
маркетплейсе не инфоповод ни в каком смысле, но именно там лежит почти весь
негатив. Классификатор — первый шаг разбора этой корзины.

Отдельно закрепляется деградация: выгрузка, которая не отдаёт ни тип сообщения,
ни тип площадки, должна вести себя как раньше — все сообщения равноправные
кандидаты, ничего не отсеивается молча.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.message_kinds import (  # noqa: E402
    EVENT_CANDIDATE_KINDS,
    KIND_COMMENT,
    KIND_EMPTY,
    KIND_POST,
    KIND_REPOST,
    KIND_REVIEW,
    classify_kinds,
    event_candidate_mask,
    kind_counts,
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


def rows(*items):
    return pd.DataFrame(
        [
            {"message_type": mt, "platform_type": pt, "text_clean": txt}
            for mt, pt, txt in items
        ]
    )


print("1. Базовые случаи Brand Analytics")
frame = rows(
    ("Пост", "Соцсети", "Запуск новой линии кровли"),
    ("Комментарий", "Отзывы", "Недостатки: слабая клейкость"),
    ("Комментарий", "Соцсети", "Согласен с автором"),
    ("Репост", "Соцсети", "Перепост новости"),
    ("Репост с дополнением", "Соцсети", "Смотрите, что пишут"),
    ("Оценка без текста", "Отзывы", ""),
    ("Пост", "Соцсети", ""),
)
kinds = list(classify_kinds(frame))
check("публикация", kinds[0] == KIND_POST, kinds[0])
check("отзыв на маркетплейсе, хотя оформлен комментарием", kinds[1] == KIND_REVIEW, kinds[1])
check("обычный комментарий", kinds[2] == KIND_COMMENT, kinds[2])
check("репост", kinds[3] == KIND_REPOST, kinds[3])
check("репост с дополнением", kinds[4] == KIND_REPOST, kinds[4])
check("оценка без текста — всё равно отзыв", kinds[5] == KIND_REVIEW, kinds[5])
check("пост без текста", kinds[6] == KIND_EMPTY, kinds[6])

print("2. Отзыв побеждает комментарий, а не наоборот")
# На маркетплейсах отзыв приходит типом «Комментарий»: в выгрузках RUFLEX так
# пришло 113 отзывов из 118. Перепутай приоритет — и весь негатив уедет в
# комментарии, где его никто не ищет.
only_review = rows(("Комментарий", "Отзывы", "Товар пришёл повреждённым"))
check(
    "тип площадки перевешивает тип сообщения",
    classify_kinds(only_review).iloc[0] == KIND_REVIEW,
    str(classify_kinds(only_review).iloc[0]),
)

print("3. Кандидаты в инфоповоды")
mask = event_candidate_mask(frame)
check("публикация — кандидат", bool(mask.iloc[0]))
check("отзыв — не кандидат", not bool(mask.iloc[1]))
check("комментарий — не кандидат", not bool(mask.iloc[2]))
check("репост — кандидат", bool(mask.iloc[3]), "репост может нести текст, которого больше нигде нет")
check("пустое сообщение — не кандидат", not bool(mask.iloc[6]))
check(
    "состав кандидатов зафиксирован",
    EVENT_CANDIDATE_KINDS == frozenset({KIND_POST, KIND_REPOST}),
    str(sorted(EVENT_CANDIDATE_KINDS)),
)

print("4. Регистр, ё и английские значения")
loose = rows(
    ("пост", "отзывы", "текст"),
    ("КОММЕНТАРИЙ", "Соцсети", "текст"),
    ("repost", "social", "text"),
    ("comment", "reviews", "text"),
)
loose_kinds = list(classify_kinds(loose))
check("нижний регистр разбирается", loose_kinds[0] == KIND_REVIEW, loose_kinds[0])
check("верхний регистр разбирается", loose_kinds[1] == KIND_COMMENT, loose_kinds[1])
check("английский repost", loose_kinds[2] == KIND_REPOST, loose_kinds[2])
check("английский reviews на площадке", loose_kinds[3] == KIND_REVIEW, loose_kinds[3])

print("5. Выгрузка без признаков ведёт себя как раньше")
# Медиалогия и универсальный формат не отдают тип площадки. Молча отсеять их
# сообщения из инфоповодов значило бы сломать работающие проекты.
plain = pd.DataFrame([{"text_clean": "раз"}, {"text_clean": "два"}])
plain_kinds = list(classify_kinds(plain))
check("всё считается публикациями", plain_kinds == [KIND_POST, KIND_POST], str(plain_kinds))
check("и всё идёт в кандидаты", bool(event_candidate_mask(plain).all()))

print("6. Канонические названия колонок тоже понимаются")
canon = pd.DataFrame(
    [
        {"Тип": "Комментарий", "Тип площадки": "Отзывы", "Сообщение": "Плохо склеилось"},
        {"Тип": "Пост", "Тип площадки": "Онлайн-СМИ", "Сообщение": "Новость рынка"},
    ]
)
canon_kinds = list(classify_kinds(canon))
check("отзыв по каноническим колонкам", canon_kinds[0] == KIND_REVIEW, canon_kinds[0])
check("публикация по каноническим колонкам", canon_kinds[1] == KIND_POST, canon_kinds[1])

print("7. Пустые входные данные")
check("пустой кадр не падает", len(classify_kinds(pd.DataFrame())) == 0)
check("None не падает", len(classify_kinds(None)) == 0)
check("маска кандидатов пустая", len(event_candidate_mask(pd.DataFrame())) == 0)
check("счётчик отдаёт нули", kind_counts(pd.DataFrame())[KIND_REVIEW] == 0)

print("8. Счётчик по природе")
counts = kind_counts(frame)
check(
    "разложено по корзинам верно",
    counts[KIND_REVIEW] == 2
    and counts[KIND_COMMENT] == 1
    and counts[KIND_REPOST] == 2
    and counts[KIND_EMPTY] == 1
    and counts[KIND_POST] == 1,
    str(counts),
)
check("сумма сходится с числом строк", sum(counts.values()) == len(frame), str(counts))

print("9. Готовая колонка kind используется как есть")
# Конвейер считает природу один раз и кладёт в таблицу сообщений; повторно
# считать её по тем же колонкам незачем.
prepared = frame.assign(kind=[KIND_POST] * len(frame))
check(
    "маска верит готовой колонке",
    bool(event_candidate_mask(prepared).all()),
    str(list(event_candidate_mask(prepared))),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Классификация сообщений работает.")
