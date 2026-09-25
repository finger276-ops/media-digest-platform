"""Саммари, комментарий к индексам и блок рисков, написанные моделью.

Разделение ответственности здесь важнее красоты промпта:

* **Платформа считает.** Все числа — сообщения, тональность, динамика,
  индексы бренда, доли негатива по темам — берутся из уже посчитанных таблиц.
* **Модель интерпретирует.** Она получает готовую карточку данных и объясняет,
  что эти числа значат: что выросло, за счёт чего, где риск, на что смотреть.

Поэтому модель не может «ошибиться в арифметике»: она не считает. И поэтому же
в промпте стоит прямой запрет выдумывать числа, которых нет в карточке.

Контекст собирается компактно: карточка на 3–6 тысяч символов вместо десятков
тысяч строк выгрузки. Это и дешевле, и точнее — модель не тонет в данных.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .ai_provider import AIConfig, AIError, complete, estimate_tokens, load_ai_config
from .brand_metrics import METRIC_TITLES
from .metrics_compute import (
    has_sentiment_markup,
    no_sentiment_line,
    numeric_series,
    overview_metrics,
    sentiment_counts,
    sentiment_masks,
    sentiment_unmarked,
)
from .period_comparison import daily_metrics_for_comparison
from .report_highlights import event_title_column, top_report_events, top_report_tags

KIND_SUMMARY = "summary"
KIND_BRAND = "brand"
KIND_RISKS = "risks"

KIND_TITLES = {
    KIND_SUMMARY: "Саммари периода",
    KIND_BRAND: "Комментарий к индексам бренда",
    KIND_RISKS: "Блок рисков для клиента",
}

# Сколько сообщений показывать модели, когда выдержки разрешены. Больше не
# нужно: карточка и так даёт распределение, а выдержки — это примеры
# формулировок, а не выборка для подсчёта.
EXCERPT_LIMIT = 24
EXCERPT_CHARS = 400
TOP_TAGS = 10
TOP_EVENTS = 12

SYSTEM_PROMPT = """Ты аналитик медиамониторинга. Пишешь по-русски, для \
руководителя, который не читал выгрузку.

Жёсткие правила:
1. Используй только числа из карточки данных. Не вычисляй новые проценты, не \
округляй по-своему, не придумывай значений, которых в карточке нет.
2. Если данных для вывода не хватает — так и напиши, не достраивай догадку.
3. Никаких вводных оборотов «в данном отчёте», «как мы видим», «стоит \
отметить». Сразу по делу.
4. Не пересказывай карточку списком. Объясняй: что изменилось, за счёт чего и \
что это значит.
5. Выдержки сообщений — это примеры формулировок, а не статистика. Опираясь на \
них, не делай количественных выводов.
6. Без эмодзи, без обращений к читателю, без общих советов вроде «важно \
следить за репутацией»."""


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _share(part: Any, whole: Any) -> str:
    try:
        part_value, whole_value = float(part), float(whole)
    except (TypeError, ValueError):
        return "0%"
    if whole_value <= 0:
        return "0%"
    return f"{part_value / whole_value * 100:.1f}%".replace(".", ",")


def _clean_text(value: Any, limit: int = EXCERPT_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _period_label(periods: pd.DataFrame, period_ids: list[str]) -> str:
    if periods is None or periods.empty:
        return ", ".join(str(x) for x in period_ids)
    subset = periods[
        periods["period_id"].astype(str).isin([str(x) for x in period_ids])
    ]
    names = [str(x) for x in subset.get("period_name", pd.Series(dtype=str)).tolist()]
    return " + ".join(n for n in names if n) or ", ".join(str(x) for x in period_ids)


def _tags_block(messages: pd.DataFrame) -> str:
    # top_report_tags - та же выборка (сортировка + топ-N), что и превью
    # «Что включить в отчёт» на «Обзоре» и PNG/DOCX/PDF-экспорт. Раньше здесь
    # был свой, третий по счёту способ выбрать теги (без пересортировки).
    stats = top_report_tags(messages, limit=TOP_TAGS)
    if stats is None or stats.empty:
        return "Теги: в выгрузке нет теговых колонок."
    # «негатив 0» у каждого тега без разметки тональности модель прочитала бы
    # как «по тегам негатива нет».
    marked = has_sentiment_markup(messages)
    lines = []
    for _, row in stats.iterrows():
        piece = (
            f"- {row.get('Тег')}: {_fmt_int(row.get('Сообщений'))} сообщ., "
            f"охват {_fmt_int(row.get('Охват'))}"
        )
        if marked:
            piece += f", негатив {_fmt_int(row.get('Негатив', 0))}"
        lines.append(piece)
    return "Топ тегов:\n" + "\n".join(lines)


def _events_block(events_agg: pd.DataFrame) -> str:
    # top_report_events - та же выборка, что и на «Обзоре»/в экспорте: без
    # служебных заголовков («Без сюжета» и варианты), отсортирована по числу
    # сообщений и значимости. Раньше здесь не было ни фильтра, ни сортировки
    # вообще - «Без сюжета» мог попасть в карточку для модели как есть.
    work = top_report_events(events_agg, limit=TOP_EVENTS)
    if work.empty:
        return "Инфоповоды: не найдены."
    title_col = event_title_column(work) or "title"
    lines = []
    for _, row in work.iterrows():
        count = int(row.get("message_count") or 0)
        negative = int(row.get("negative_count") or 0)
        title = _clean_text(row.get(title_col), 160)
        piece = f"- «{title}»: {_fmt_int(count)} сообщ."
        if negative:
            piece += f", из них негативных {_fmt_int(negative)} ({_share(negative, count)})"
        merged = int(row.get("merged_titles") or 0)
        if merged:
            piece += f"; собран из {merged + 1} формулировок заголовка"
        lines.append(piece)
    return "Крупнейшие инфоповоды:\n" + "\n".join(lines)


def metrics_block(messages: pd.DataFrame, metrics: dict[str, Any] | None) -> str:
    base = dict(metrics or overview_metrics(messages))
    sentiment = base.get("sentiment") or sentiment_counts(messages)
    total = int(base.get("messages") or 0)
    # Без разметки «нейтрал 100 %» — не измерение: модели и читателю отчёта
    # говорится прямо, что тональности нет.
    if sentiment_unmarked(sentiment, messages):
        tone = no_sentiment_line("Тональность")
    else:
        tone = (
            "Тональность: "
            f"позитив {_fmt_int(sentiment.get('positive'))} ({_share(sentiment.get('positive'), total)}), "
            f"нейтрал {_fmt_int(sentiment.get('neutral'))} ({_share(sentiment.get('neutral'), total)}), "
            f"негатив {_fmt_int(sentiment.get('negative'))} ({_share(sentiment.get('negative'), total)})"
        )
    lines = [
        f"Сообщений: {_fmt_int(total)}",
        f"Суммарная аудитория площадок: {_fmt_int(base.get('audience'))}",
        f"Суммарный охват: {_fmt_int(base.get('reach'))}",
        f"Суммарная вовлечённость: {_fmt_int(base.get('engagement'))}",
        tone,
    ]
    return "Метрики периода:\n" + "\n".join(lines)


def comparison_block(metrics: dict[str, Any] | None) -> str:
    """Динамика к предыдущему периоду, если она посчитана платформой."""
    comparison = (metrics or {}).get("comparison") or {}
    previous = comparison.get("previous") or {}
    current = comparison.get("current") or {}
    if not previous or not current:
        return "Динамика: выбран один период, сравнивать не с чем."

    def _value(source: dict[str, Any], field: str) -> float:
        if field == "negative":
            return float((source.get("sentiment") or {}).get("negative") or 0)
        try:
            return float(source.get(field) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _delta(field: str, label: str) -> str | None:
        was, now = _value(previous, field), _value(current, field)
        if was == 0 and now == 0:
            return None
        percent = ((now - was) / was * 100) if was else None
        tail = f" ({percent:+.1f}%)".replace(".", ",") if percent is not None else ""
        return f"- {label}: было {_fmt_int(was)}, стало {_fmt_int(now)}{tail}"

    previous_label = str(previous.get("label") or "предыдущий период")
    current_label = str(current.get("label") or "текущий период")
    # «было 0, стало 5» между неразмеченным и размеченным периодом — это
    # появившаяся разметка, а не рост негатива. Если не размечены оба,
    # об этом уже сказал блок метрик.
    unmarked_labels = [
        label
        for source, label in ((previous, previous_label), (current, current_label))
        if sentiment_unmarked(source.get("sentiment"))
    ]
    if not unmarked_labels:
        negative_line = _delta("negative", "негативные сообщения")
    elif len(unmarked_labels) == 1:
        negative_line = (
            "- негативные сообщения: нет данных для сравнения — в периоде "
            f"«{unmarked_labels[0]}» нет разметки тональности"
        )
    else:
        negative_line = None
    lines = [
        line
        for line in (
            _delta("messages", "сообщения"),
            _delta("audience", "аудитория"),
            _delta("reach", "охват"),
            _delta("engagement", "вовлечённость"),
            negative_line,
        )
        if line
    ]
    if not lines:
        return "Динамика: изменений в основных метриках нет."
    return (
        f"Динамика ({previous_label} → {current_label}):\n" + "\n".join(lines)
    )


def _daily_highlight_block(messages: pd.DataFrame) -> str | None:
    """Пиковые дни внутри периода — календарная разбивка, а не сам период.

    comparison_block выше — период к периоду, это и есть заголовочная
    динамика отчёта, её менять нельзя (на ней держатся цифры в PNG/DOCX/
    PDF). Этот блок — дополнение: даёт модели повод сказать «пик негатива
    пришёлся на 27.04», а не только «негатив вырос на 12%». Возвращает
    None, если дней меньше трёх — на двух «пик» это и так весь диапазон,
    не наблюдение.
    """
    daily = daily_metrics_for_comparison(messages)
    if len(daily) < 3:
        return None

    def _negative(day: dict[str, Any]) -> int:
        return int((day.get("sentiment") or {}).get("negative") or 0)

    busiest = max(daily, key=lambda d: d.get("messages", 0))
    worst = max(daily, key=_negative)
    lines = [
        f"- больше всего сообщений: {busiest.get('label')} "
        f"({_fmt_int(busiest.get('messages'))})",
    ]
    if _negative(worst) > 0:
        lines.append(
            f"- больше всего негативных сообщений: {worst.get('label')} "
            f"({_fmt_int(_negative(worst))})"
        )
    return f"По дням внутри периода ({len(daily)} дн.):\n" + "\n".join(lines)


def _brand_metrics_block(cards: dict[str, dict[str, Any]] | None) -> str:
    if not cards:
        return "Индексы бренда: не посчитаны."
    lines = []
    for key in ["BPI", "NSS", "SES", "TVS", "SOV", "ReachScore", "ER", "ERR"]:
        card = cards.get(key) or {}
        code, title = METRIC_TITLES.get(key, (key, key))
        if not card.get("available"):
            reason = _clean_text(card.get("reason"), 120) or "нет данных"
            lines.append(f"- {code} ({title}): нет значения — {reason}")
            continue
        unit = str(card.get("unit") or "")
        lines.append(f"- {code} ({title}): {card.get('value')}{unit}")
    return "Индексы бренда:\n" + "\n".join(lines)


def _excerpts_block(
    messages: pd.DataFrame, *, negative_only: bool = False, limit: int = EXCERPT_LIMIT
) -> str:
    """Выдержки самых заметных сообщений — примеры формулировок, не выборка."""
    if messages is None or messages.empty:
        return ""
    work = messages.copy()
    if negative_only:
        # Без разметки «Негативных сообщений в периоде нет» подтолкнуло бы
        # модель написать клиенту, что рисков нет.
        if not has_sentiment_markup(work):
            return no_sentiment_line("Выдержки негативных сообщений", "выделить нельзя")
        work = work[sentiment_masks(work)[1].to_numpy()]
        if work.empty:
            return "Негативных сообщений в периоде нет."

    text_col = None
    for candidate in ("text_clean", "text", "Текст", "message_text"):
        if candidate in work.columns:
            text_col = candidate
            break
    if not text_col:
        return ""

    weight = numeric_series(work, ["engagement", "Вовлечённость", "Вовлеченность"])
    if float(weight.sum()) <= 0:
        weight = numeric_series(work, ["views", "Просмотры", "Просмотров", "audience"])
    work = work.assign(_weight=weight).sort_values("_weight", ascending=False)

    lines = []
    for _, row in work.head(limit).iterrows():
        text = _clean_text(row.get(text_col))
        if not text:
            continue
        source = _clean_text(row.get("chat_title") or row.get("platform"), 60)
        tone = _clean_text(row.get("sentiment"), 24)
        head = " · ".join(x for x in (source, tone) if x)
        lines.append(f"- [{head}] {text}" if head else f"- {text}")
    if not lines:
        return ""
    label = "Выдержки негативных сообщений" if negative_only else "Выдержки заметных сообщений"
    return f"{label} (примеры формулировок, не статистика):\n" + "\n".join(lines)


def build_data_card(
    *,
    project_name: str,
    periods: pd.DataFrame,
    period_ids: list[str],
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    metrics: dict[str, Any] | None = None,
    brand_cards: dict[str, dict[str, Any]] | None = None,
    include_excerpts: bool = True,
    negative_excerpts: bool = False,
) -> str:
    """Собрать компактную карточку данных периода для модели."""
    blocks = [
        f"Проект: {project_name}",
        f"Период: {_period_label(periods, period_ids)}",
        metrics_block(messages, metrics),
        comparison_block(metrics),
        _tags_block(messages),
        _events_block(events_agg),
        _brand_metrics_block(brand_cards),
    ]
    daily_highlight = _daily_highlight_block(messages)
    if daily_highlight:
        blocks.append(daily_highlight)
    if include_excerpts:
        excerpts = _excerpts_block(messages, negative_only=negative_excerpts)
        if excerpts:
            blocks.append(excerpts)
    return "\n\n".join(block for block in blocks if block)


TASK_PROMPTS = {
    KIND_SUMMARY: """Напиши саммари периода: 4–6 абзацев сплошным текстом, без \
списков и заголовков.

Что должно быть в тексте:
- что происходило в информационном поле бренда за период;
- какие темы дали основной объём и почему они появились;
- как изменилась картина к предыдущему периоду, если динамика есть в карточке;
- где сосредоточен негатив (если в карточке сказано, что разметки \
тональности нет, — одной фразой скажи, что тональность не оценивалась);
- если в карточке есть блок «По дням внутри периода» — укажи конкретный \
день пика (сообщений или негатива), это конкретнее, чем «негатив вырос»;
- одно-два наблюдения, которые не видны из голых цифр.""",
    KIND_BRAND: """Напиши комментарий к индексам бренда: 2–4 абзаца.

Что должно быть в тексте:
- что означает текущий уровень BPI и какие метрики его вытянули или просадили;
- расхождения между метриками и что они говорят (например, положительный NSS \
при отрицательном ToneVolumeScore значит, что негатив собран в одной-двух \
крупных темах);
- метрики без значения — назови их и скажи, каких данных не хватает;
- на что смотреть в следующем периоде.

Не объясняй формулы — они есть в интерфейсе. Объясняй смысл чисел.""",
    KIND_RISKS: """Напиши блок рисков для клиента: сначала одно предложение с \
общей оценкой, затем 3–5 пунктов списка.

Каждый пункт: в чём риск, насколько он заметен по цифрам, что с ним делать. \
Формулировки конкретные, без «усилить коммуникацию» и «мониторить ситуацию».

Если серьёзного негатива в периоде нет — так и напиши одним абзацем и не \
выдумывай риски.

Если в карточке сказано, что разметки тональности нет, не делай вывода об \
отсутствии негатива: прямо напиши, что тональность в выгрузке не размечена, и \
оцени риски по объёму, темам и динамике.""",
}


def build_prompt(kind: str, data_card: str, extra_instructions: str = "") -> str:
    task = TASK_PROMPTS.get(kind)
    if not task:
        raise AIError(f"Неизвестный тип текста: {kind}")
    parts = [task, "", "Карточка данных:", data_card]
    if extra_instructions.strip():
        parts.extend(["", "Дополнительные указания заказчика:", extra_instructions.strip()])
    return "\n".join(parts)


def generate_text(
    kind: str,
    data_card: str,
    *,
    extra_instructions: str = "",
    config: AIConfig | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Сгенерировать один текст и вернуть его вместе с метаданными запуска."""
    config = config or load_ai_config()
    prompt = build_prompt(kind, data_card, extra_instructions)
    text = complete(SYSTEM_PROMPT, prompt, config, session=session)
    return {
        "kind": kind,
        "text": text,
        "provider": config.provider,
        "model": config.model,
        "prompt_tokens_estimate": estimate_tokens(SYSTEM_PROMPT + prompt),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


ACCESS_OWNER = "owner"
ACCESS_EDITOR = "editor"
ACCESS_OPTIONS = {
    ACCESS_OWNER: "Только владелец платформы",
    ACCESS_EDITOR: "Владелец и аналитики проекта",
}
DEFAULT_ACCESS = ACCESS_OWNER


def ai_access_level(project_settings: dict[str, Any] | None) -> str:
    """Кому в этом проекте разрешена генерация.

    Значение живёт в `platform_projects.settings.ai_access` и действует только
    для своего проекта. Мусор и незнакомые значения трактуются как «только
    владелец»: расширение доступа должно быть явным решением, а не следствием
    опечатки в настройках.
    """
    raw = str((project_settings or {}).get("ai_access") or DEFAULT_ACCESS)
    raw = raw.strip().lower()
    return raw if raw in ACCESS_OPTIONS else DEFAULT_ACCESS


def can_generate_ai(
    role: str, project_settings: dict[str, Any] | None, *, is_platform_owner: bool
) -> bool:
    """Владелец платформы — всегда; редактор — если владелец это разрешил.

    Роль «просмотр» не получает генерацию ни при каких настройках: клиент
    ничего не редактирует, а каждый запуск стоит платного запроса к модели.
    """
    if is_platform_owner:
        return True
    if ai_access_level(project_settings) != ACCESS_EDITOR:
        return False
    return str(role or "").strip().lower() in {"editor", "owner"}


def ai_text_storage_key(kind: str, period_ids: list[str]) -> str:
    """Ключ ручной записи: свой текст на каждый набор периодов."""
    periods = "__".join(sorted(str(x) for x in (period_ids or []) if str(x).strip()))
    return f"ai_text::{kind}::{periods}"
