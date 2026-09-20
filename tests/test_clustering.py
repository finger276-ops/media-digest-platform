# -*- coding: utf-8 -*-
"""Алгоритмическая кластеризация инфоповодов (services/clustering.py).

Применяется, когда в выгрузке нет готовой разметки сюжетов Brand Analytics —
Медиалогия, универсальный формат. Путь боевой: services/ingest.py вызывает
preprocess.run_preprocess_from_dataframe с cluster_method="tfidf" по
умолчанию, ни один вызывающий код это не переопределяет.

cluster_discussions_embeddings в этот файл сознательно не включён: первая же
строка функции — `from sentence_transformers import SentenceTransformer», и
эта библиотека не установлена (requirements-preprocess.txt держит её
опциональной — «установка может быть долгой»). Написать тест, который не
может выполниться в этом окружении, значило бы изобразить покрытие, которого
нет. Как показывает preprocess.py, ни один вызывающий код сейчас не передаёт
cluster_method="embeddings" — путь не задействован в проде.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.clustering import (  # noqa: E402
    cluster_discussions_tfidf,
    cluster_sparse_greedy,
    dynamic_threshold,
    refine_labels_by_tag,
    split_labels_by_fixed_time_window,
    split_labels_by_time_gap,
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


# Токенизатор (services/ru_text.tokenize_ru) не лемматизирует: «рады» и «рад»,
# «городе» и «город» — разные токены. Тексты-почти-дубликаты ниже совпадают
# слово в слово, чтобы косинусное сходство было предсказуемо высоким, а не
# зависело от случайных пересечений форм слова.
NEAR_DUP_A = "Компания открыла новый завод в городе Иваново, жители довольны результатом стройки"
NEAR_DUP_B = "Компания открыла новый завод в городе Иваново, жители очень довольны результатом стройки"
RELATED_DIFFERENT = "Мэрия Иваново подтвердила открытие нового завода компании и поблагодарила строителей"
OFF_TOPIC = "Курс доллара вырос сегодня утром на бирже, аналитики прогнозируют дальнейший рост"
NOISE = "12345 67 89"  # только цифры — токенизатор выбрасывает все токены (длина/стоп-слова)


print("1. dynamic_threshold: широкие/шумные бакеты требуют более строгого порога")
check("general_yandex ужесточает порог", dynamic_threshold("тема::general_yandex::прочее", 0.5) == 0.62)
check("суффикс '::general' тоже ужесточает", dynamic_threshold("src::tag::general", 0.5) == 0.62)
check("суффикс '::other' тоже ужесточает", dynamic_threshold("src::tag::other", 0.5) == 0.62)
check("coeff_priority ужесточает порог", abs(dynamic_threshold("любая::coeff_priority::микро", 0.5) - 0.57) < 1e-9)
check("app_bug ужесточает порог", dynamic_threshold("любая::app_bug::микро", 0.5) == 0.55)
check("app_orders ужесточает порог", dynamic_threshold("любая::app_orders::микро", 0.5) == 0.55)
check("обычный бакет — порог не меняется", dynamic_threshold("любая::обычный_тег::микро", 0.5) == 0.5)
check(
    "порог не может превысить потолок даже при высоком base",
    dynamic_threshold("x::general_yandex::y", 0.7) == 0.72,
    str(dynamic_threshold("x::general_yandex::y", 0.7)),
)

print("2. cluster_sparse_greedy: кластеризация внутри узкого бакета")
check("пустой бакет не падает", list(cluster_sparse_greedy(
    pd.DataFrame({"discussion_text": []}), 0.38, 3, 16, 6000)) == [])
check("один документ получает метку 0", list(cluster_sparse_greedy(
    pd.DataFrame({"discussion_text": ["текст"]}), 0.38, 3, 16, 6000)) == [0])

merge_group = pd.DataFrame({"discussion_text": [NEAR_DUP_A, NEAR_DUP_B, OFF_TOPIC]})
merge_labels = list(cluster_sparse_greedy(merge_group, 0.38, 3, 16, 6000))
check(
    "почти дублирующиеся тексты сливаются в один кластер",
    merge_labels[0] == merge_labels[1],
    str(merge_labels),
)
check(
    "текст на другую тему остаётся отдельным кластером",
    merge_labels[2] != merge_labels[0],
    str(merge_labels),
)

all_noise = pd.DataFrame({"discussion_text": [NOISE, "и в на по", "000 111"]})
check(
    "бакет из одного шума (пустой словарь TF-IDF) не падает — каждый получает свою метку",
    list(cluster_sparse_greedy(all_noise, 0.38, 3, 16, 6000)) == [0, 1, 2],
    str(list(cluster_sparse_greedy(all_noise, 0.38, 3, 16, 6000))),
)

mixed = pd.DataFrame({"discussion_text": [NEAR_DUP_A, NEAR_DUP_B, NOISE]})
mixed_labels = list(cluster_sparse_greedy(mixed, 0.38, 3, 16, 6000))
check(
    "шумовая строка среди настоящих текстов не мешает им слиться и получает отдельную метку",
    mixed_labels[0] == mixed_labels[1] and mixed_labels[2] not in mixed_labels[:2],
    str(mixed_labels),
)

# Особенность реализации: max_df=0.72 в TfidfVectorizer отбрасывает термины,
# встречающиеся более чем в 72% документов бакета. Если в бакете РОВНО два
# документа и они лексически совпадают, их общие слова присутствуют в 100%
# документов — выше порога — и весь словарь обнуляется, оба расходятся по
# разным меткам вместо ожидаемого слияния. С третьим документом в бакете (как
# в реальных данных, где бакет из ровно двух сообщений — редкость) эффект
# уходит: 2 из 3 документов = 67%, ниже порога. Это поведение боевого кода,
# а не баг теста — фиксирую его явно, чтобы будущая правка max_df не изменила
# его незаметно.
exactly_two = pd.DataFrame({"discussion_text": [NEAR_DUP_A, NEAR_DUP_B]})
check(
    "ОСОБЕННОСТЬ: ровно два идентичных документа в бакете max_df разводит по разным меткам",
    list(cluster_sparse_greedy(exactly_two, 0.38, 3, 16, 6000)) == [0, 1],
    str(list(cluster_sparse_greedy(exactly_two, 0.38, 3, 16, 6000))),
)

print("3. cluster_discussions_tfidf: сборка инфоповодов по бакетам темы+времени")
check("пустой вход не падает", list(cluster_discussions_tfidf(
    pd.DataFrame({"discussion_text": []}))) == [])
check("один документ получает метку 0", list(cluster_discussions_tfidf(
    pd.DataFrame({"discussion_text": [NEAR_DUP_A], "start_date": ["2026-04-24 10:00:00"]}))) == [0])

same_bucket = pd.DataFrame(
    {
        "discussion_text": [NEAR_DUP_A, NEAR_DUP_B, RELATED_DIFFERENT],
        "start_date": ["2026-04-24 10:00:00", "2026-04-24 11:00:00", "2026-04-24 12:00:00"],
        "topic_bucket": ["topicA::tag1::micro1"] * 3,
    }
)
same_bucket_labels = list(cluster_discussions_tfidf(same_bucket, similarity_threshold=0.38))
check(
    "почти дублирующиеся сообщения одного бакета темы+времени слились в один инфоповод",
    same_bucket_labels[0] == same_bucket_labels[1],
    str(same_bucket_labels),
)

different_bucket = pd.DataFrame(
    {
        "discussion_text": [NEAR_DUP_A, NEAR_DUP_B, RELATED_DIFFERENT, NEAR_DUP_A],
        "start_date": [
            "2026-04-24 10:00:00", "2026-04-24 11:00:00",
            "2026-04-24 12:00:00", "2026-04-24 10:15:00",
        ],
        "topic_bucket": [
            "topicA::tag1::micro1", "topicA::tag1::micro1",
            "topicA::tag1::micro1", "topicB::tag1::micro1",
        ],
    }
)
db_labels = list(cluster_discussions_tfidf(different_bucket, similarity_threshold=0.38))
check(
    "внутри topicA почти дубликаты всё ещё сливаются",
    db_labels[0] == db_labels[1],
    str(db_labels),
)
check(
    "разбиение по бакету темы сильнее текстового сходства: идентичный текст в другом topic_bucket не сливается",
    db_labels[3] != db_labels[0],
    str(db_labels),
)

far_apart = pd.DataFrame(
    {
        # День 1: A, B и «третий лишний» — втроём, чтобы не попасть в
        # особенность max_df=0.72 на паре из двух документов (см. раздел 2).
        # День 3 (+48ч, за пределами max_event_span_hours=16): копия A.
        "discussion_text": [NEAR_DUP_A, NEAR_DUP_B, RELATED_DIFFERENT, NEAR_DUP_A],
        "start_date": [
            "2026-04-24 10:00:00", "2026-04-24 11:00:00", "2026-04-24 12:00:00",
            "2026-04-26 12:00:00",
        ],
        "topic_bucket": ["topicA::tag1::micro1"] * 4,
    }
)
far_labels = list(cluster_discussions_tfidf(far_apart, similarity_threshold=0.38, max_event_span_hours=16.0))
check(
    "близкие по времени почти дубликаты одного дня слиты",
    far_labels[0] == far_labels[1],
    str(far_labels),
)
check(
    "тот же текст, но на два дня позже — за пределами временного окна, не сливается с первым днём",
    far_labels[3] != far_labels[0],
    str(far_labels),
)

auto_bucket = pd.DataFrame(
    {
        "discussion_text": [NEAR_DUP_A, NEAR_DUP_B, RELATED_DIFFERENT, OFF_TOPIC],
        "start_date": [
            "2026-04-24 10:00:00", "2026-04-24 11:00:00",
            "2026-04-24 12:00:00", "2026-04-24 10:30:00",
        ],
        "main_tags": ["завод|стройка"] * 3 + ["финансы|биржа"],
        "microtopic": ["other"] * 4,
        "source_main_topic": [""] * 4,
    }
)
auto_labels = list(cluster_discussions_tfidf(auto_bucket, similarity_threshold=0.38))
check(
    "topic_bucket вычисляется автоматически (topic_bucket_for), когда колонки нет: почти дубликаты слиты",
    auto_labels[0] == auto_labels[1],
    str(auto_labels),
)
check(
    "и текст на финансовую тему остаётся отдельно",
    auto_labels[3] not in auto_labels[:3],
    str(auto_labels),
)

print("4. refine_labels_by_tag: сырой кластер дробится по главному тегу и теме источника")
same_raw = pd.Series([0, 0, 0])
by_tag = pd.DataFrame(
    {
        "main_tags": ["завод|стройка", "завод|стройка", "финансы|биржа"],
        "source_main_topic": ["", "", ""],
    }
)
tag_labels = list(refine_labels_by_tag(same_raw, by_tag))
check(
    "одинаковый сырой кластер, но разный тег — расщепляется",
    tag_labels[0] == tag_labels[1] and tag_labels[2] != tag_labels[0],
    str(tag_labels),
)
same_tag = pd.DataFrame(
    {
        "main_tags": ["завод|стройка", "завод"],
        "source_main_topic": ["ивановская область", "ивановская область"],
    }
)
check(
    "одинаковый сырой кластер, тег и тема источника совпадают — остаётся один",
    list(refine_labels_by_tag(pd.Series([0, 0]), same_tag)) == [0, 0],
)

print("5. split_labels_by_time_gap: широкий кластер дробится на волны по разрыву во времени")
gap_discussions = pd.DataFrame(
    {
        "start_date": ["2026-04-24 10:00:00", "2026-04-24 11:00:00", "2026-04-26 10:00:00"],
        "end_date": ["2026-04-24 10:30:00", "2026-04-24 11:30:00", "2026-04-26 10:30:00"],
    }
)
gap_labels = list(split_labels_by_time_gap(pd.Series([0, 0, 0]), gap_discussions, max_gap_hours=12.0))
check(
    "разрыв больше max_gap_hours расщепляет кластер на волны",
    gap_labels[0] == gap_labels[1] and gap_labels[2] != gap_labels[0],
    str(gap_labels),
)
close_discussions = pd.DataFrame(
    {
        "start_date": ["2026-04-24 10:00:00", "2026-04-24 11:00:00", "2026-04-24 12:00:00"],
        "end_date": ["2026-04-24 10:30:00", "2026-04-24 11:30:00", "2026-04-24 12:30:00"],
    }
)
check(
    "все обсуждения в пределах разрыва — кластер остаётся одной волной",
    list(split_labels_by_time_gap(pd.Series([0, 0, 0]), close_discussions, max_gap_hours=12.0)) == [0, 0, 0],
)
nat_discussions = pd.DataFrame(
    {
        "start_date": ["2026-04-24 10:00:00", None, "2026-04-24 12:00:00"],
        "end_date": ["2026-04-24 10:30:00", None, "2026-04-24 12:30:00"],
    }
)
check(
    "нераспознанная дата (NaT) не роняет разбиение и не создаёт лишнюю волну",
    list(split_labels_by_time_gap(pd.Series([0, 0, 0]), nat_discussions, max_gap_hours=12.0)) == [0, 0, 0],
)
check(
    "пустой вход не падает",
    list(split_labels_by_time_gap(pd.Series([], dtype=int), pd.DataFrame(columns=["start_date", "end_date"]), max_gap_hours=12.0)) == [],
)

print("6. split_labels_by_fixed_time_window: дробление по фиксированной сетке времени")
window_discussions = pd.DataFrame({"start_date": ["2026-04-24 10:00:00", "2026-04-26 10:00:00"]})
check(
    "разные фиксированные окна расщепляют кластер",
    list(split_labels_by_fixed_time_window(pd.Series([0, 0]), window_discussions, window_hours=16.0)) == [0, 1],
    str(list(split_labels_by_fixed_time_window(pd.Series([0, 0]), window_discussions, window_hours=16.0))),
)
check(
    "одно и то же окно — кластер не дробится",
    list(split_labels_by_fixed_time_window(
        pd.Series([0, 0]),
        pd.DataFrame({"start_date": ["2026-04-24 10:00:00", "2026-04-24 11:00:00"]}),
        window_hours=16.0,
    )) == [0, 0],
)
check(
    "window_hours<=0 — метки не меняются",
    list(split_labels_by_fixed_time_window(pd.Series([0, 0]), window_discussions, window_hours=0)) == [0, 0],
)
check(
    "пустой вход — метки не меняются, не падает",
    list(split_labels_by_fixed_time_window(pd.Series([], dtype=int), pd.DataFrame(), window_hours=16.0)) == [],
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Алгоритмическая кластеризация инфоповодов работает.")
