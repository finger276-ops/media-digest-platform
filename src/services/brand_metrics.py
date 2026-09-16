# -*- coding: utf-8 -*-
"""Индексы бренда: SES, NSS, ToneVolumeScore, ER, ERR, SOV, ReachScore, BPI.

Модуль не зависит от Streamlit: его используют интерфейс, автосаммари и тесты.
Каждая метрика возвращается карточкой с формулой, входными числами и причиной
недоступности — чтобы аналитик видел не только число, но и из чего оно собрано.

Соглашения по источникам данных платформы:

* охват     — колонка `views` («Просмотры» / «Охват» в выгрузке);
* аудитория — колонка `audience` («Аудитория», по смыслу подписчики площадки);
* реакции   — сумма `likes + comments + reposts`, а если их нет в периоде,
              то готовая колонка `engagement` («Вовлечённость»).

Два расхождения в исходном гайде разрешены так (см. docs/METRICS.md):

1. NSS и ToneVolumeScore по формулам гайда совпадают. NSS считается по
   инфоповодам (расшифровка метрики говорит «% позитивных инфоповодов»),
   ToneVolumeScore — по сообщениям. Тогда метрики показывают разное:
   баланс повестки и баланс потока упоминаний.
2. У SOV формула написана через охват, а пример посчитан по упоминаниям.
   Считаются оба варианта: основной задаётся настройкой `sov_basis`
   (по умолчанию упоминания, как в примере), второй показывается в расшифровке.
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from services.metrics_compute import audience_total, numeric_series

POSITIVE_PATTERN = "позит|positive|полож"
NEGATIVE_PATTERN = "нег|negative|отриц"

REACH_COLUMNS = ["views", "Просмотры", "Просмотров", "reach", "Охват"]
AUDIENCE_COLUMNS = ["audience", "Аудитория"]
ENGAGEMENT_COLUMNS = ["engagement", "Вовлечённость", "Вовлеченность", "engagement_count"]
# Родительный падеж — как эти колонки называет Brand Analytics. Здесь обычно
# приходит уже канонический кадр, но список синонимов должен совпадать с тем,
# что понимает импорт: расхождение однажды уже стоило платформе всех лайков.
LIKE_COLUMNS = ["likes", "Лайки", "Лайков", "Likes"]
COMMENT_COLUMNS = ["comments", "Комментарии", "Комментариев", "Comments"]
REPOST_COLUMNS = ["reposts", "Репосты", "Репостов", "Reposts", "Shares"]

EVENT_TITLE_COLUMNS = ["event_title", "title", "Сюжет / инфоповод", "Сюжет"]

TECHNICAL_EVENT_TITLES = {
    "",
    "без сюжета",
    "без названия",
    "без темы",
    "прочее",
    "nan",
    "none",
}

DEFAULT_BPI_WEIGHTS = {"NSS": 0.4, "SES": 0.4, "TVS": 0.2}

DEFAULT_SETTINGS: dict[str, Any] = {
    "bpi_weights": dict(DEFAULT_BPI_WEIGHTS),
    "nss_basis": "events",      # events | messages
    "sov_basis": "messages",    # messages | reach
}

# Метрики, которые можно включить в BPI. Значение — шкала метрики: метрики
# баланса живут в диапазоне -100..100, доли — в 0..100. Смешивать их можно,
# но интерфейс предупреждает об этом.
BPI_AVAILABLE_METRICS = {
    "NSS": "balance",
    "SES": "balance",
    "TVS": "balance",
    "SOV": "share",
    "ReachScore": "share",
    "ER": "share",
    "ERR": "share",
}

METRIC_TITLES = {
    "SES": ("SES", "Эффективность позитива"),
    "NSS": ("NSS", "Чистый индекс тональности"),
    "TVS": ("ToneVolumeScore", "Тональность с учётом объёма"),
    "ER": ("ER", "Вовлечённость от подписчиков"),
    "ERR": ("ERR", "Вовлечённость от охвата"),
    "SOV": ("SOV", "Доля голоса в категории"),
    "ReachScore": ("ReachScore", "Индекс охвата"),
    "BPI": ("BPI", "Индекс восприятия бренда"),
}


# ---------------------------------------------------------------------------
# Подготовка рядов
# ---------------------------------------------------------------------------


def merge_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Слить настройки проекта с умолчаниями, отбросив мусор."""
    merged: dict[str, Any] = {
        "bpi_weights": dict(DEFAULT_BPI_WEIGHTS),
        "nss_basis": DEFAULT_SETTINGS["nss_basis"],
        "sov_basis": DEFAULT_SETTINGS["sov_basis"],
    }
    source = settings or {}

    weights = source.get("bpi_weights")
    if isinstance(weights, dict):
        cleaned: dict[str, float] = {}
        for key, value in weights.items():
            if key not in BPI_AVAILABLE_METRICS:
                continue
            try:
                weight = float(value)
            except (TypeError, ValueError):
                continue
            if weight > 0:
                cleaned[key] = weight
        if cleaned:
            merged["bpi_weights"] = cleaned

    if str(source.get("nss_basis")) in {"events", "messages"}:
        merged["nss_basis"] = str(source["nss_basis"])
    if str(source.get("sov_basis")) in {"messages", "reach"}:
        merged["sov_basis"] = str(source["sov_basis"])
    return merged


def sentiment_masks(messages: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Маски позитивных и негативных сообщений."""
    empty = pd.Series([False] * len(messages), index=messages.index)
    if messages is None or messages.empty:
        return empty, empty

    if "_sentiment_lower" in messages.columns:
        sentiment = messages["_sentiment_lower"].fillna("").astype(str)
    else:
        sentiment = (
            messages.get("sentiment", pd.Series([""] * len(messages), index=messages.index))
            .fillna("")
            .astype(str)
            .str.lower()
            .str.replace("ё", "е", regex=False)
        )

    positive = sentiment.str.contains(POSITIVE_PATTERN, regex=True, na=False)
    negative = sentiment.str.contains(NEGATIVE_PATTERN, regex=True, na=False)

    if "_is_negative_bool" in messages.columns:
        negative = negative | messages["_is_negative_bool"].astype(bool)
    elif "is_negative" in messages.columns:
        negative = negative | messages["is_negative"].astype(str).str.lower().isin(
            ["true", "1", "yes", "да", "негатив", "negative"]
        )
    # Сообщение не может быть одновременно позитивным и негативным:
    # при грязной разметке приоритет у негатива, он важнее для рисков.
    positive = positive & ~negative
    return positive, negative


def reaction_series(messages: pd.DataFrame) -> tuple[pd.Series, str]:
    """Реакции по сообщениям и описание того, откуда они взяты."""
    if messages is None or messages.empty:
        return pd.Series(dtype=float), "нет данных"

    has_parts = any(
        col in messages.columns
        for col in LIKE_COLUMNS + COMMENT_COLUMNS + REPOST_COLUMNS
    )
    if has_parts:
        likes = numeric_series(messages, LIKE_COLUMNS)
        comments = numeric_series(messages, COMMENT_COLUMNS)
        reposts = numeric_series(messages, REPOST_COLUMNS)
        parts = likes.add(comments, fill_value=0).add(reposts, fill_value=0)
        if float(parts.sum()) > 0:
            return parts, "лайки + комментарии + репосты"

    engagement = numeric_series(messages, ENGAGEMENT_COLUMNS)
    return engagement, "колонка «Вовлечённость» выгрузки"


def event_title_column(messages: pd.DataFrame) -> str | None:
    for col in EVENT_TITLE_COLUMNS:
        if col in messages.columns:
            return col
    if "event_id" in messages.columns:
        return "event_id"
    return None


def _is_technical_title(value: Any) -> bool:
    return str(value or "").strip().lower() in TECHNICAL_EVENT_TITLES


def _card(
    key: str,
    *,
    value: float | None,
    formula: str,
    inputs: dict[str, Any] | None = None,
    hint: str = "",
    reason: str = "",
    unit: str = "%",
) -> dict[str, Any]:
    code, title = METRIC_TITLES.get(key, (key, key))
    return {
        "key": key,
        "code": code,
        "title": title,
        "value": None if value is None else round(float(value), 2),
        "available": value is not None,
        "unit": unit,
        "formula": formula,
        "inputs": inputs or {},
        "hint": hint,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Метрики тональности
# ---------------------------------------------------------------------------


def compute_ses(messages: pd.DataFrame) -> dict[str, Any]:
    """SES = (позитивный охват − негативный охват) / общий охват × 100%."""
    formula = "(Позитивный охват − Негативный охват) / Общий охват × 100%"
    hint = "Показывает, компенсирует ли позитивная повестка негативную по охвату."

    if messages is None or messages.empty:
        return _card("SES", value=None, formula=formula, hint=hint, reason="Нет сообщений за период.")

    reach = numeric_series(messages, REACH_COLUMNS)
    basis = "просмотры"
    if float(reach.sum()) <= 0:
        # Частый случай для СМИ-выгрузок: просмотров нет, но есть аудитория.
        reach = numeric_series(messages, AUDIENCE_COLUMNS)
        basis = "аудитория (просмотров в выгрузке нет)"

    total = float(reach.sum())
    if total <= 0:
        return _card(
            "SES",
            value=None,
            formula=formula,
            hint=hint,
            reason="В выгрузке нет ни просмотров, ни аудитории — охват неизвестен.",
        )

    positive_mask, negative_mask = sentiment_masks(messages)
    positive_reach = float(reach[positive_mask].sum())
    negative_reach = float(reach[negative_mask].sum())
    value = (positive_reach - negative_reach) / total * 100

    return _card(
        "SES",
        value=value,
        formula=formula,
        hint=hint,
        inputs={
            "Позитивный охват": int(positive_reach),
            "Негативный охват": int(negative_reach),
            "Общий охват": int(total),
            "Основа расчёта": basis,
        },
    )


def _theme_sentiment_counts(messages: pd.DataFrame) -> tuple[int, int, int]:
    """Разложить инфоповоды на позитивные, негативные и нейтральные.

    Тональность темы определяется большинством: если негативных сообщений в
    теме больше позитивных — тема негативная, и наоборот. Ничья и отсутствие
    окраски дают нейтральную тему.
    """
    title_col = event_title_column(messages)
    if not title_col:
        return 0, 0, 0

    positive_mask, negative_mask = sentiment_masks(messages)
    work = pd.DataFrame(
        {
            "theme": messages[title_col].fillna("").astype(str),
            "positive": positive_mask.astype(int),
            "negative": negative_mask.astype(int),
        }
    )
    work = work[~work["theme"].apply(_is_technical_title)]
    if work.empty:
        return 0, 0, 0

    grouped = work.groupby("theme")[["positive", "negative"]].sum()
    positive_themes = int((grouped["positive"] > grouped["negative"]).sum())
    negative_themes = int((grouped["negative"] > grouped["positive"]).sum())
    total_themes = int(len(grouped))
    return positive_themes, negative_themes, total_themes


def compute_nss(messages: pd.DataFrame, basis: str = "events") -> dict[str, Any]:
    """NSS = % позитивных инфоповодов − % негативных инфоповодов."""
    formula = "% позитивных инфоповодов − % негативных инфоповодов"
    hint = "Эмоциональный баланс повестки: каких тем больше — хороших или плохих."

    if messages is None or messages.empty:
        return _card("NSS", value=None, formula=formula, hint=hint, reason="Нет сообщений за период.")

    if basis == "events":
        positive, negative, total = _theme_sentiment_counts(messages)
        unit_name = "инфоповодов"
        if total == 0:
            # Инфоповодов нет (например, период без кластеризации) — честно
            # считаем по сообщениям и подписываем это в расшифровке.
            basis = "messages"
    if basis != "events":
        positive_mask, negative_mask = sentiment_masks(messages)
        positive = int(positive_mask.sum())
        negative = int(negative_mask.sum())
        total = int(len(messages))
        unit_name = "сообщений"
        formula = "% позитивных сообщений − % негативных сообщений"

    if total == 0:
        return _card("NSS", value=None, formula=formula, hint=hint, reason="Не из чего считать доли.")

    value = (positive - negative) / total * 100
    return _card(
        "NSS",
        value=value,
        formula=formula,
        hint=hint,
        inputs={
            f"Позитивных {unit_name}": positive,
            f"Негативных {unit_name}": negative,
            f"Всего {unit_name}": total,
        },
    )


def compute_tone_volume_score(messages: pd.DataFrame) -> dict[str, Any]:
    """ToneVolumeScore = (позитивные − негативные) / все упоминания × 100%."""
    formula = "(Позитивные − Негативные) / Все упоминания × 100%"
    hint = "Баланс тональности всего потока упоминаний, а не только заметных тем."

    if messages is None or messages.empty:
        return _card("TVS", value=None, formula=formula, hint=hint, reason="Нет сообщений за период.")

    positive_mask, negative_mask = sentiment_masks(messages)
    positive = int(positive_mask.sum())
    negative = int(negative_mask.sum())
    total = int(len(messages))
    value = (positive - negative) / total * 100

    return _card(
        "TVS",
        value=value,
        formula=formula,
        hint=hint,
        inputs={
            "Позитивных сообщений": positive,
            "Негативных сообщений": negative,
            "Всего сообщений": total,
        },
    )


# ---------------------------------------------------------------------------
# Метрики вовлечённости
# ---------------------------------------------------------------------------


def compute_er(messages: pd.DataFrame) -> dict[str, Any]:
    """ER = (лайки + комментарии + репосты) / подписчики × 100%."""
    formula = "(Лайки + Комментарии + Репосты) / Подписчики × 100%"
    hint = "Активность аудитории относительно её размера."

    if messages is None or messages.empty:
        return _card("ER", value=None, formula=formula, hint=hint, reason="Нет сообщений за период.")

    reactions, source = reaction_series(messages)
    # Площадка считается один раз: иначе знаменатель раздут числом публикаций,
    # и ER выходит во столько же раз заниженным.
    audience = float(audience_total(messages))
    total_reactions = float(reactions.sum())

    if audience <= 0:
        return _card(
            "ER",
            value=None,
            formula=formula,
            hint=hint,
            reason="В выгрузке нет колонки «Аудитория» — не от чего считать долю.",
        )

    return _card(
        "ER",
        value=total_reactions / audience * 100,
        formula=formula,
        hint=hint,
        inputs={
            "Реакции": int(total_reactions),
            "Источник реакций": source,
            "Аудитория (подписчики площадок)": int(audience),
        },
    )


def compute_err(messages: pd.DataFrame) -> dict[str, Any]:
    """ERR = (лайки + комментарии + репосты) / охват × 100%."""
    formula = "(Лайки + Комментарии + Репосты) / Охват × 100%"
    hint = "Точнее ER: считается от тех, кто реально увидел публикации."

    if messages is None or messages.empty:
        return _card("ERR", value=None, formula=formula, hint=hint, reason="Нет сообщений за период.")

    reactions, source = reaction_series(messages)
    reach = float(numeric_series(messages, REACH_COLUMNS).sum())
    total_reactions = float(reactions.sum())

    if reach <= 0:
        return _card(
            "ERR",
            value=None,
            formula=formula,
            hint=hint,
            reason="В выгрузке нет просмотров — охват неизвестен.",
        )

    return _card(
        "ERR",
        value=total_reactions / reach * 100,
        formula=formula,
        hint=hint,
        inputs={
            "Реакции": int(total_reactions),
            "Источник реакций": source,
            "Охват": int(reach),
        },
    )


# ---------------------------------------------------------------------------
# Метрики доли в категории (нужна выгрузка по конкурентам)
# ---------------------------------------------------------------------------


def benchmark_frame(benchmark: dict[str, Any] | None) -> pd.DataFrame:
    """Привести сохранённый бенчмарк категории к таблице брендов."""
    if not benchmark:
        return pd.DataFrame()
    brands = benchmark.get("brands") or []
    if isinstance(brands, str):
        return pd.DataFrame()
    frame = pd.DataFrame(brands)
    if frame.empty:
        return frame
    for col in ["messages", "audience", "reach", "engagement"]:
        if col not in frame.columns:
            frame[col] = 0
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).astype(int)
    if "brand" not in frame.columns:
        return pd.DataFrame()
    frame["brand"] = frame["brand"].astype(str)
    if "is_own" not in frame.columns:
        own_brand = str(benchmark.get("own_brand") or "")
        frame["is_own"] = frame["brand"] == own_brand
    frame["is_own"] = frame["is_own"].astype(bool)
    return frame


def compute_sov(benchmark: dict[str, Any] | None, basis: str = "messages") -> dict[str, Any]:
    """SOV = показатель бренда / сумма по всем брендам категории × 100%."""
    formula = "Упоминания бренда / Упоминания всех брендов × 100%"
    if basis == "reach":
        formula = "Охват бренда / Охват всех брендов × 100%"
    hint = "Громкость бренда в категории. Высокий SOV не всегда хорош — смотрите вместе с тональностью."
    no_data = "Нет выгрузки по категории: загрузите её в разделе «Индексы бренда»."

    frame = benchmark_frame(benchmark)
    if frame.empty:
        return _card("SOV", value=None, formula=formula, hint=hint, reason=no_data)

    own = frame[frame["is_own"]]
    if own.empty:
        return _card(
            "SOV",
            value=None,
            formula=formula,
            hint=hint,
            reason="В выгрузке по категории не отмечен собственный бренд.",
        )

    column = "reach" if basis == "reach" else "messages"
    own_value = float(own[column].sum())
    total_value = float(frame[column].sum())
    if total_value <= 0:
        return _card(
            "SOV",
            value=None,
            formula=formula,
            hint=hint,
            reason="В выгрузке по категории нет данных по выбранному показателю.",
        )

    by_messages = (
        float(own["messages"].sum()) / float(frame["messages"].sum()) * 100
        if float(frame["messages"].sum()) > 0
        else None
    )
    by_reach = (
        float(own["reach"].sum()) / float(frame["reach"].sum()) * 100
        if float(frame["reach"].sum()) > 0
        else None
    )

    return _card(
        "SOV",
        value=own_value / total_value * 100,
        formula=formula,
        hint=hint,
        inputs={
            "Бренд": str(own["brand"].iloc[0]),
            "Показатель бренда": int(own_value),
            "Показатель категории": int(total_value),
            "Брендов в категории": int(len(frame)),
            "SOV по упоминаниям": None if by_messages is None else round(by_messages, 2),
            "SOV по охвату": None if by_reach is None else round(by_reach, 2),
        },
    )


def compute_reach_score(benchmark: dict[str, Any] | None) -> dict[str, Any]:
    """ReachScore = охват бренда / максимальный охват в категории × 100%."""
    formula = "Охват бренда / Максимальный охват в категории × 100%"
    hint = "Заметность бренда на фоне самого громкого игрока категории."
    no_data = "Нет выгрузки по категории: загрузите её в разделе «Индексы бренда»."

    frame = benchmark_frame(benchmark)
    if frame.empty:
        return _card("ReachScore", value=None, formula=formula, hint=hint, reason=no_data)

    own = frame[frame["is_own"]]
    if own.empty:
        return _card(
            "ReachScore",
            value=None,
            formula=formula,
            hint=hint,
            reason="В выгрузке по категории не отмечен собственный бренд.",
        )

    own_reach = float(own["reach"].sum())
    max_reach = float(frame["reach"].max())
    if max_reach <= 0:
        return _card(
            "ReachScore",
            value=None,
            formula=formula,
            hint=hint,
            reason="В выгрузке по категории нет данных об охвате.",
        )

    leader_row = frame.loc[frame["reach"].idxmax()]
    return _card(
        "ReachScore",
        value=own_reach / max_reach * 100,
        formula=formula,
        hint=hint,
        inputs={
            "Охват бренда": int(own_reach),
            "Максимальный охват в категории": int(max_reach),
            "Лидер по охвату": str(leader_row.get("brand", "")),
        },
    )


# ---------------------------------------------------------------------------
# Композитный индекс
# ---------------------------------------------------------------------------


def compute_bpi(cards: dict[str, dict[str, Any]], weights: dict[str, float]) -> dict[str, Any]:
    """BPI = Σ (вес × метрика). Веса нормализуются по доступным метрикам."""
    formula = "(w₁ × NSS) + (w₂ × SES) + (w₃ × ToneVolumeScore), сумма весов = 1"
    hint = "Сводная оценка восприятия бренда. Состав и веса настраиваются под проект."

    usable = {
        key: float(weight)
        for key, weight in (weights or {}).items()
        if weight and cards.get(key, {}).get("available")
    }
    if not usable:
        missing = ", ".join(sorted(weights or {})) or "не выбраны"
        return _card(
            "BPI",
            value=None,
            formula=formula,
            hint=hint,
            reason=f"Нет доступных метрик для расчёта (ожидались: {missing}).",
        )

    weight_sum = sum(usable.values())
    parts: dict[str, Any] = {}
    value = 0.0
    for key, weight in usable.items():
        normalized = weight / weight_sum
        metric_value = float(cards[key]["value"])
        value += normalized * metric_value
        parts[f"{cards[key]['code']} × {normalized:.2f}"] = round(
            normalized * metric_value, 2
        )

    skipped = [key for key in (weights or {}) if key not in usable]
    if skipped:
        parts["Не хватило данных"] = ", ".join(
            cards.get(key, {}).get("code", key) for key in skipped
        )
    if abs(weight_sum - 1.0) > 0.001:
        parts["Веса нормализованы"] = f"сумма была {weight_sum:.2f}"

    scales = {BPI_AVAILABLE_METRICS.get(key) for key in usable}
    if len(scales) > 1:
        parts["Внимание"] = (
            "в индекс смешаны метрики баланса (−100…100) и доли (0…100) — "
            "сравнивайте значения только между периодами одного проекта"
        )

    return _card("BPI", value=value, formula=formula, hint=hint, inputs=parts)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def compute_brand_metrics(
    messages: pd.DataFrame,
    *,
    benchmark: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Посчитать все индексы бренда за период."""
    config = merge_settings(settings)

    cards: dict[str, dict[str, Any]] = {
        "SES": compute_ses(messages),
        "NSS": compute_nss(messages, basis=config["nss_basis"]),
        "TVS": compute_tone_volume_score(messages),
        "ER": compute_er(messages),
        "ERR": compute_err(messages),
        "SOV": compute_sov(benchmark, basis=config["sov_basis"]),
        "ReachScore": compute_reach_score(benchmark),
    }
    cards["BPI"] = compute_bpi(cards, config["bpi_weights"])
    return cards


def metrics_to_frame(cards: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Таблица метрик для показа и выгрузки."""
    rows = []
    for key in ["BPI", "NSS", "SES", "TVS", "SOV", "ReachScore", "ER", "ERR"]:
        card = cards.get(key)
        if not card:
            continue
        rows.append(
            {
                "Метрика": card["code"],
                "Название": card["title"],
                "Значение": card["value"],
                "Формула": card["formula"],
                "Статус": "рассчитана" if card["available"] else card["reason"],
            }
        )
    return pd.DataFrame(rows)


def metrics_by_period(
    messages: pd.DataFrame,
    period_ids: Iterable[str],
    *,
    benchmarks: dict[str, dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Значения метрик по каждому периоду — для графика динамики."""
    if messages is None or messages.empty or "period_id" not in messages.columns:
        return pd.DataFrame()

    benchmarks = benchmarks or {}
    rows = []
    for period_id in period_ids:
        subset = messages[messages["period_id"].astype(str) == str(period_id)]
        if subset.empty:
            continue
        cards = compute_brand_metrics(
            subset,
            benchmark=benchmarks.get(str(period_id)),
            settings=settings,
        )
        row: dict[str, Any] = {"period_id": str(period_id)}
        for key, card in cards.items():
            row[card["code"]] = card["value"]
        rows.append(row)
    return pd.DataFrame(rows)
