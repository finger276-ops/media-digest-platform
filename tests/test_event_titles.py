"""Проверка склейки инфоповодов с близкими заголовками.

Тест закрывает два риска сразу:
* переформулировки одного сюжета должны собираться в один инфоповод;
* разные события с общим брендом в заголовке склеиваться не должны.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from services.event_titles import (  # noqa: E402
    DEFAULT_SIMILARITY,
    group_similar_titles,
    merge_similar_events,
    normalize_event_title,
    preview_merge_levels,
    title_tokens,
)

# Переформулировки одного и того же сюжета от периода к периоду.
SAME_STORY = [
    (
        "ТЕХНОНИКОЛЬ открыла новый завод по производству каменной ваты в Рязани",
        "Открытие завода каменной ваты ТЕХНОНИКОЛЬ в Рязани",
    ),
    (
        "Компания ТЕХНОНИКОЛЬ инвестирует 5 млрд рублей в модернизацию производства",
        "ТЕХНОНИКОЛЬ инвестирует 5,2 млрд руб. в модернизацию производства",
    ),
    (
        "Более 100 сотрудников приняли участие в экологической акции ТЕХНОНИКОЛЬ",
        "Более 150 сотрудников приняли участие в экологической акции ТЕХНОНИКОЛЬ",
    ),
    (
        "ТЕХНОНИКОЛЬ представила новую линейку кровельных материалов на выставке",
        "ТЕХНОНИКОЛЬ представила новую линейку кровельных материалов на выставке в Москве",
    ),
    (
        "«ТЕХНОНИКОЛЬ» запустила образовательную программу для студентов",
        "ТЕХНОНИКОЛЬ запустила образовательную программу для студентов...",
    ),
]

# Разные события: общий бренд и общая конструкция фразы не повод их сливать.
DIFFERENT_STORIES = [
    (
        "ТЕХНОНИКОЛЬ открыла новый завод в Рязани",
        "ТЕХНОНИКОЛЬ закрыла завод в Хабаровске",
    ),
    (
        "ТЕХНОНИКОЛЬ представила новую линейку кровельных материалов",
        "ТЕХНОНИКОЛЬ представила новую линейку теплоизоляции для фасадов",
    ),
    (
        "Пожар на складе строительных материалов в Подмосковье",
        "Пожар на заводе минеральной ваты в Рязанской области",
    ),
    (
        "ТЕХНОНИКОЛЬ вошла в рейтинг лучших работодателей России",
        "ТЕХНОНИКОЛЬ вошла в топ экспортеров стройматериалов",
    ),
    (
        "Суд отклонил иск подрядчика к ТЕХНОНИКОЛЬ",
        "Подрядчик подал иск к ТЕХНОНИКОЛЬ на 30 млн рублей",
    ),
    (
        "ТЕХНОНИКОЛЬ повысила зарплаты сотрудникам завода",
        "ТЕХНОНИКОЛЬ сократила сотрудников завода в Юрге",
    ),
]


def events_frame(titles, counts=None):
    counts = counts or [10] * len(titles)
    return pd.DataFrame(
        {
            "group_key": [normalize_event_title(t) for t in titles],
            "title": titles,
            "description": [f"Описание: {t}" for t in titles],
            "tags": ["Строительство" for _ in titles],
            "start_date": pd.to_datetime(["2026-04-01"] * len(titles)),
            "end_date": pd.to_datetime(["2026-04-05"] * len(titles)),
            "message_count": counts,
            "chat_count": [3] * len(titles),
            "negative_count": [1] * len(titles),
            "importance_score": [float(c) for c in counts],
            "negative_share": [0.1] * len(titles),
            "event_ids": [[f"e_{i}"] for i in range(len(titles))],
            "title_variants": [[t] for t in titles],
            "merged_titles": [0] * len(titles),
        }
    )


def test_normalization_merges_cosmetic_differences():
    """Кавычки, ё/е, многоточия и пробелы в числах — не разные сюжеты."""
    variants = [
        "«ТЕХНОНИКОЛЬ» подвела итоги 1 000 проверок",
        "ТЕХНОНИКОЛЬ подвела итоги 1000 проверок",
        "Технониколь подвела итоги 1000 проверок…",
        "ТЕХНОНИКОЛЬ подвела итоги 1 000 проверок!",
    ]
    keys = {normalize_event_title(v) for v in variants}
    assert len(keys) == 1, keys


def test_yo_and_dashes_normalized():
    assert normalize_event_title("Всё о заводе — итоги") == normalize_event_title(
        "Все о заводе - итоги"
    )


def test_same_story_pairs_merge():
    """Переформулировки одного сюжета собираются в один инфоповод."""
    missed = []
    for left, right in SAME_STORY:
        frame = events_frame([left, right], counts=[20, 5])
        merged, report = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
        if len(merged) != 1:
            missed.append((left, right))
        else:
            assert report and len(report[0]["variants"]) == 2
            assert merged.iloc[0]["message_count"] == 25
            assert set(merged.iloc[0]["event_ids"]) == {"e_0", "e_1"}
    # Одна морфологически трудная пара может не склеиться — это допустимо,
    # промах безопаснее ложной склейки. Больше одной — регрессия.
    assert len(missed) <= 1, missed


def test_different_stories_never_merge():
    """Ложная склейка дороже пропуска: разные события должны остаться разными."""
    for left, right in DIFFERENT_STORIES:
        frame = events_frame([left, right])
        merged, report = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
        assert len(merged) == 2, (left, right, report)
        assert report == []


def test_short_titles_need_exact_match():
    """У коротких заголовков слишком мало сигнала для нечёткой склейки."""
    frame = events_frame(["Пожар на складе", "Пожар на заводе"])
    merged, _ = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
    assert len(merged) == 2


def test_no_chaining_between_distant_titles():
    """Слияние идёт к лидеру, а не по цепочке: A~B, B~C не делают A~C."""
    titles = [
        "ТЕХНОНИКОЛЬ открыла завод каменной ваты в Рязани",
        "ТЕХНОНИКОЛЬ открыла завод каменной ваты в Рязани и Хабаровске",
        "ТЕХНОНИКОЛЬ открыла завод в Хабаровске и наняла сотрудников",
    ]
    mapping = group_similar_titles(titles, [30, 20, 10], threshold=DEFAULT_SIMILARITY)
    leaders = {mapping[normalize_event_title(t)] for t in titles}
    assert len(leaders) >= 2


def test_threshold_off_keeps_every_title():
    frame = events_frame([left for left, _ in SAME_STORY] + [r for _, r in SAME_STORY])
    merged, report = merge_similar_events(frame, threshold=0.0)
    assert len(merged) == len(frame)
    assert report == []


def test_blocked_title_stays_separate():
    """Аналитик может запретить склейку конкретного заголовка."""
    left, right = SAME_STORY[3]
    frame = events_frame([left, right], counts=[20, 5])
    merged, _ = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
    assert len(merged) == 1
    blocked, _ = merge_similar_events(
        frame, threshold=DEFAULT_SIMILARITY, blocked={right}
    )
    assert len(blocked) == 2


def test_leader_is_the_biggest_topic():
    left, right = SAME_STORY[4]
    frame = events_frame([left, right], counts=[3, 40])
    merged, report = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
    assert merged.iloc[0]["title"] == right
    assert report[0]["variants"][0] == right


def test_merge_sums_counts_and_spans_dates():
    left, right = SAME_STORY[1]
    frame = events_frame([left, right], counts=[10, 4])
    frame.loc[0, "start_date"] = pd.Timestamp("2026-04-02")
    frame.loc[1, "start_date"] = pd.Timestamp("2026-03-28")
    frame.loc[1, "end_date"] = pd.Timestamp("2026-04-09")
    frame.loc[1, "tags"] = "Производство"
    merged, _ = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
    row = merged.iloc[0]
    assert row["message_count"] == 14
    assert row["negative_count"] == 2
    assert pd.Timestamp(row["start_date"]) == pd.Timestamp("2026-03-28")
    assert pd.Timestamp(row["end_date"]) == pd.Timestamp("2026-04-09")
    assert row["tags"] == "Производство | Строительство"


def test_empty_frame_is_safe():
    merged, report = merge_similar_events(pd.DataFrame())
    assert merged.empty if isinstance(merged, pd.DataFrame) else merged is not None
    assert report == []


def test_preview_levels_monotonic():
    """Диагностика: чем ниже порог, тем меньше остаётся инфоповодов."""
    titles = [left for left, _ in SAME_STORY] + [r for _, r in SAME_STORY]
    frame = events_frame(titles)
    preview = preview_merge_levels(frame, levels=(0.9, 0.75, 0.62, 0.5))
    counts = preview["Инфоповодов"].tolist()
    assert counts == sorted(counts, reverse=True)


def test_tokens_drop_numbers_and_stopwords():
    tokens = title_tokens(normalize_event_title("Более 150 сотрудников на заводе"))
    assert all(not t.isdigit() for t in tokens)
    assert "более" not in tokens


def test_residual_bucket_sorts_last_after_merge():
    """Остаточная корзина не возвращается наверх после склейки заголовков.

    «Без сюжета» собирает всё, что не сложилось в инфоповод, и её вес выходит
    наибольшим просто потому, что сообщений там сотни. Склейка пересортировывает
    список заново, и без отдельного правила мешок снова встал бы первым.
    """
    frame = events_frame(
        ["Без сюжета", "Запуск линии кровельных материалов", "Акция на профлист"],
        counts=[577, 124, 10],
    )
    frame["is_residual"] = [True, False, False]

    merged, _ = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
    assert "is_residual" in merged.columns, merged.columns.tolist()
    assert not bool(merged.iloc[0]["is_residual"]), merged["title"].tolist()
    assert bool(merged.iloc[-1]["is_residual"]), merged["title"].tolist()
    # Вес не подменяется: он честно показывает объём остатка.
    residual = merged[merged["is_residual"]].iloc[0]
    assert float(residual["importance_score"]) == 577.0, residual["importance_score"]


def test_residual_flag_survives_a_merge_group():
    """Если остаток попал в склейку, результат остаётся остатком."""
    frame = events_frame(["Без сюжета", "без сюжета…"], counts=[300, 200])
    frame["is_residual"] = [True, False]
    merged, _ = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
    assert len(merged) == 1, merged["title"].tolist()
    assert bool(merged.iloc[0]["is_residual"])


def test_merge_without_residual_column_still_works():
    """Периоды, обработанные до появления колонки, не должны падать."""
    frame = events_frame(["Запуск завода", "Акция на профлист"], counts=[20, 5])
    merged, _ = merge_similar_events(frame, threshold=DEFAULT_SIMILARITY)
    assert len(merged) == 2, merged["title"].tolist()
    assert merged.iloc[0]["message_count"] == 20


if __name__ == "__main__":
    failures = []
    for name, func in sorted(dict(globals()).items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
    print(f"\n{'ПРОВАЛЕНО: ' + str(len(failures)) if failures else 'Все проверки прошли'}")
    sys.exit(1 if failures else 0)
