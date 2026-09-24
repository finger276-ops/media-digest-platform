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

# Признак разметки тональности и маски — общие для всей платформы и живут в
# metrics_compute; здесь реэкспорт под прежними именами.
from services.metrics_compute import (  # noqa: F401  реэкспорт
    EMPTY_SENTIMENT_VALUES,
    NEGATIVE_PATTERN,
    NO_SENTIMENT_REASON,
    POSITIVE_PATTERN,
    audience_total,
    has_sentiment_markup,
    numeric_series,
    sentiment_masks,
    sentiment_text as _sentiment_text,
)

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

# Метрика без данных показывает прочерк и причину, а не ноль. Для тональности
# и реакций это нужно проверять явно: пустая «Тональность» даёт маски «всё
# False», и (0 − 0) / N выглядело бы измеренным нулём.
# «Пусты или нулевые»: импорт хранит пустую ячейку реакций как 0, поэтому
# отсутствие колонки и честные нули после загрузки не различить.
NO_REACTIONS_REASON = (
    "В выгрузке нет реакций: лайки, комментарии, репосты и «Вовлечённость» "
    "пусты или нулевые у всех сообщений периода."
)

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

    if not has_sentiment_markup(messages):
        return _card("SES", value=None, formula=formula, hint=hint, reason=NO_SENTIMENT_REASON)

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

    if not has_sentiment_markup(messages):
        return _card("NSS", value=None, formula=formula, hint=hint, reason=NO_SENTIMENT_REASON)

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

    if not has_sentiment_markup(messages):
        return _card("TVS", value=None, formula=formula, hint=hint, reason=NO_SENTIMENT_REASON)

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
    if total_reactions <= 0:
        return _card("ER", value=None, formula=formula, hint=hint, reason=NO_REACTIONS_REASON)

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
    if total_reactions <= 0:
        return _card("ERR", value=None, formula=formula, hint=hint, reason=NO_REACTIONS_REASON)

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


def _no_rivals_reason(benchmark: dict[str, Any] | None, what: str) -> str:
    """Почему сравнивать не с кем — по-разному для двух разных причин.

    Отмеченный конкурент, которого нет в сообщениях периода, выпадает из
    агрегатов. Сказать человеку «конкуренты не отмечены», когда он их отметил,
    значит отправить его искать ошибку не там.
    """
    configured = int((benchmark or {}).get("configured_competitors", 0) or 0)
    if configured:
        missing = [str(x) for x in (benchmark or {}).get("missing_competitors", [])]
        names = ", ".join(missing[:5]) if missing else "ни один"
        return (
            f"{what} считается на фоне конкурентов, но в выбранном периоде их "
            f"упоминаний нет ({names}). Проверьте период или разметку брендов."
        )
    return (
        f"Отмечены только свои бренды. {what} считается на фоне конкурентов — "
        "отметьте их в блоке «Бренды категории»."
    )


def compute_sov(benchmark: dict[str, Any] | None, basis: str = "messages") -> dict[str, Any]:
    """SOV = показатель бренда / сумма по всем брендам категории × 100%."""
    formula = "Упоминания наших брендов / Упоминания всех брендов категории × 100%"
    if basis == "reach":
        formula = "Охват наших брендов / Охват всех брендов категории × 100%"
    hint = "Громкость бренда в категории. Высокий SOV не всегда хорош — смотрите вместе с тональностью."
    no_data = (
        "Не с чем сравнивать. Отметьте бренды категории в блоке «Бренды "
        "категории» или загрузите выгрузку по всей категории."
    )

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
            reason="Не отмечен ни один свой бренд — считать долю не от чего.",
        )
    # Без конкурентов в знаменателе доля голоса равна ста процентам по
    # определению. Показать это как результат значило бы выдать за измерение
    # то, что измерением не является.
    if not bool((~frame["is_own"]).any()):
        return _card(
            "SOV",
            value=None,
            formula=formula,
            hint=hint,
            reason=_no_rivals_reason(benchmark, "Доля голоса"),
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
            # Своих брендов может быть несколько — головной, дочерние, марки.
            # Показать только первый значило бы соврать: доля посчитана по всем.
            "Бренд": " + ".join(str(x) for x in own["brand"]),
            "Показатель бренда": int(own_value),
            "Показатель категории": int(total_value),
            "Брендов в категории": int(len(frame)),
            "SOV по упоминаниям": None if by_messages is None else round(by_messages, 2),
            "SOV по охвату": None if by_reach is None else round(by_reach, 2),
        },
    )


def compute_reach_score(benchmark: dict[str, Any] | None) -> dict[str, Any]:
    """ReachScore = охват бренда / максимальный охват в категории × 100%."""
    formula = "Охват наших брендов / Охват самого громкого игрока категории × 100%"
    hint = (
        "Заметность бренда на фоне самого громкого игрока категории. Своя "
        "группа брендов сравнивается как один участник."
    )
    no_data = (
        "Не с чем сравнивать. Отметьте бренды категории в блоке «Бренды "
        "категории» или загрузите выгрузку по всей категории."
    )

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
            reason="Не отмечен ни один свой бренд — сравнивать нечего.",
        )
    if not bool((~frame["is_own"]).any()):
        return _card(
            "ReachScore",
            value=None,
            formula=formula,
            hint=hint,
            reason=_no_rivals_reason(benchmark, "Заметность"),
        )

    # Своя группа сравнивается с игроками категории как один участник. Иначе
    # сумма нескольких своих брендов делилась бы на охват одного чужого, и
    # индекс выходил больше ста процентов: на выгрузке Кнауфа так и вышло —
    # 103,39% при охвате группы 19 539 против 18 899 у лидера.
    own_reach = float(own["reach"].sum())
    rivals = frame[~frame["is_own"]]
    rival_reach = float(rivals["reach"].max()) if not rivals.empty else 0.0
    max_reach = max(own_reach, rival_reach)
    if max_reach <= 0:
        return _card(
            "ReachScore",
            value=None,
            formula=formula,
            hint=hint,
            reason="Нет данных об охвате ни у одного бренда категории.",
        )

    if own_reach >= rival_reach:
        leader = " + ".join(str(x) for x in own["brand"])
    else:
        leader = str(rivals.loc[rivals["reach"].idxmax()].get("brand", ""))
    return _card(
        "ReachScore",
        value=own_reach / max_reach * 100,
        formula=formula,
        hint=hint,
        inputs={
            "Охват бренда": int(own_reach),
            "Максимальный охват в категории": int(max_reach),
            "Лидер по охвату": leader,
            "Конкурентов в категории": int(len(rivals)),
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


# Куда «лучше» для каждой метрики — этим задаётся цвет изменения.
#
# Доля голоса тоже красится, хотя рост громкости не всегда хорошая новость:
# бывает и скандальный. Цвет здесь показывает направление движения, а не
# приговор — толкование даёт аналитик в столбце «Вывод», он же смотрит
# тональность рядом. Прятать движение ради того, чтобы никого не ввести в
# заблуждение, значит прятать половину смысла метрики.
#
# Метрика, которой здесь нет, не красится: по умолчанию платформа не берётся
# судить о направлении.
METRIC_DIRECTION = {
    "BPI": "up",
    "NSS": "up",
    "SES": "up",
    "TVS": "up",
    "SOV": "up",
    "ReachScore": "up",
    "ER": "up",
    "ERR": "up",
}


def metric_direction(code: str) -> str:
    return METRIC_DIRECTION.get(str(code), "neutral")


def metrics_to_frame(
    cards: dict[str, dict[str, Any]], notes: dict[str, str] | None = None
) -> pd.DataFrame:
    """Таблица метрик для показа и выгрузки.

    Формулы в таблицу не идут. Это собственная методика платформы, а не то, что
    заказчик должен читать с экрана: цифру он получает вместе с выводом, а как
    она устроена — предмет отдельного разговора, если спросит.

    Вместо формулы — вывод аналитика: что эта метрика означает для бренда.
    Число без толкования заказчику ничего не говорит, а толкование зависит от
    рынка и от того, что происходило в периоде, — машине его не составить.
    """
    notes = notes or {}
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
                "Статус": "рассчитана" if card["available"] else card["reason"],
                "Вывод": str(notes.get(card["code"], "") or ""),
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
