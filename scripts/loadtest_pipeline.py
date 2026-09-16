# -*- coding: utf-8 -*-
"""Нагрузочный прогон конвейера обработки: синтетика N сообщений, время и
пиковая память по каждому шагу.

Зачем: конвейер (src/preprocess.py) ни разу не прогонялся на объёме, близком
к боевому, и в нём есть минимум два места, растущих квадратично по памяти —
полная матрица косинусной близости в src/services/story_recovery.py и в
cluster_sparse_greedy (src/preprocess.py). На небольших выгрузках это незаметно,
на 50 000 сообщений может упереться в лимит контейнера (~1 ГБ).

Что делает скрипт:
1. Детерминированно генерирует СЫРУЮ выгрузку Brand Analytics (или обычную,
   без сюжетов — ветка алгоритмической кластеризации), похожую на реальную:
   сообщения распределены по сюжетам/площадкам/авторам по степенному закону,
   часть текстов — дословные повторы, тексты содержат общую лексику (без неё
   TF-IDF не даст пересечений и квадратичный шаг не проявится).
2. Канонизирует её через import_adapters.canonicalize_table — тем же кодом,
   что и настоящая загрузка.
3. Прогоняет конвейер НЕ чёрным ящиком, а по шагам, теми же вызовами, что
   build_processed_tables (src/preprocess.py) — с меткой времени и пиком
   tracemalloc на каждый шаг.
4. Печатает таблицу и, по флагу, сохраняет JSON.
5. По флагу --verify сверяет число строк в manifest с обычным
   run_preprocess_from_dataframe — если шаги разошлись с боевым порядком,
   тест это заметит раньше человека.

Пишет только во временную папку (или в --out-dir, если он не совпадает с
data/processed): build_processed_tables перед записью удаляет из output
прошлые таблицы, и прогон без временной папки снёс бы рабочие данные.

Использование:
    .venv\\Scripts\\python.exe scripts\\loadtest_pipeline.py --messages 50000
    .venv\\Scripts\\python.exe scripts\\loadtest_pipeline.py --messages 2000 --verify
    .venv\\Scripts\\python.exe scripts\\loadtest_pipeline.py --messages 50000 --branch algo
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (ROOT, SRC):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from import_adapters import canonicalize_table  # noqa: E402
from preprocess import (  # noqa: E402
    apply_event_quality_gate,
    cluster_discussions_tfidf,
    detect_tag_columns,
    is_brand_analytics_dataframe,
    make_discussions,
    make_events,
    make_events_from_source_stories,
    normalize_messages,
    refine_labels_by_tag,
    run_preprocess_from_dataframe,
    split_labels_by_fixed_time_window,
    split_labels_by_time_gap,
)
from services.story_recovery import DEFAULT_MIN_AUTHORS, DEFAULT_MIN_MESSAGES, recover_stories
from io_utils import write_table, write_manifest


# ---------------------------------------------------------------------------
# Генерация синтетики
# ---------------------------------------------------------------------------

# Восемь тем со своим словарём-ядром. Слова подобраны так, чтобы естественно
# задевать правила classify_microtopic (preprocess.py) — цена, сертификат,
# доставка, гарантия, качество — без отдельного флага «микротема».
CORE_TOPICS: list[tuple[str, list[str]]] = [
    ("Рост цен на утеплитель", ["утеплитель", "цена", "подорожание", "материал", "стройка", "склад", "поставка", "дилер", "завод", "прайс"]),
    ("Запуск новой линии", ["линия", "запуск", "завод", "производство", "мощность", "инвестиции", "цех", "оборудование", "модернизация", "рязань"]),
    ("Отзывы о монтаже", ["монтаж", "бригада", "качество", "работа", "отзыв", "клиент", "гарантия", "укладка", "специалист", "опыт"]),
    ("Сертификация продукции", ["сертификат", "гост", "документы", "проверка", "лаборатория", "испытание", "стандарт", "соответствие", "качество", "норма"]),
    ("Проблемы с доставкой", ["доставка", "склад", "логистика", "срок", "задержка", "транспорт", "груз", "получатель", "отгрузка", "перевозчик"]),
    ("Экологическая инициатива", ["экология", "утилизация", "переработка", "отходы", "выброс", "чистота", "инициатива", "проект", "компания", "среда"]),
    ("Партнёрство с сетью", ["партнёрство", "сеть", "магазин", "дистрибьютор", "договор", "розница", "продажа", "точка", "регион", "контракт"]),
    ("Изменение упаковки", ["упаковка", "дизайн", "формат", "паллета", "этикетка", "маркировка", "тара", "объём", "логотип", "образец"]),
]

# Общий фон: без разделяемой лексики TF-IDF не даст пересечений между
# текстами разных авторов, и квадратичный шаг (story_recovery.py) не
# проявится — покажет ложно хороший результат.
BACKGROUND_WORDS = [
    "компания", "рынок", "продукт", "клиент", "сервис", "качество", "команда",
    "решение", "проект", "развитие", "результат", "сотрудник", "процесс",
    "регион", "объём", "показатель", "период", "информация", "ситуация",
    "вопрос", "мнение", "обсуждение", "новость", "событие", "план", "цель",
    "задача", "подход", "опыт", "практика", "уровень", "система", "фактор",
    "город", "страна", "область", "направление", "тема", "случай", "пример",
    "деталь", "элемент", "часть", "группа", "участник", "представитель",
    "руководство", "отдел", "подразделение", "филиал", "офис", "площадка",
]

SENTIMENT_WEIGHTS = {"позитив": 0.12, "нейтрал": 0.80, "негатив": 0.08}
KIND_WEIGHTS = {"post": 0.62, "repost": 0.16, "comment": 0.14, "review": 0.08}


def zipf_sizes(rng: np.random.Generator, groups: int, total: int, exponent: float = 1.2) -> np.ndarray:
    """Размеры групп по степенному закону — так распределены сюжеты, площадки
    и авторы в реальных выгрузках: немногие крупные, длинный хвост мелких."""
    groups = max(1, groups)
    weights = 1.0 / (np.arange(1, groups + 1) ** exponent)
    weights = weights / weights.sum()
    counts = rng.multinomial(total, weights)
    # multinomial может дать нулевые группы — не годится, где нужна хотя бы
    # минимальная активность (площадка/автор с нулём сообщений не нужна).
    zero = counts == 0
    if zero.any():
        deficit = int(zero.sum())
        counts = counts.copy()
        counts[zero] = 1
        top = int(np.argmax(counts))
        counts[top] = max(1, counts[top] - deficit)
    return counts


def _weighted_choice(rng: np.random.Generator, weights: dict[str, float], size: int) -> np.ndarray:
    keys = list(weights.keys())
    probs = np.array([weights[k] for k in keys], dtype=float)
    probs = probs / probs.sum()
    idx = rng.choice(len(keys), size=size, p=probs)
    return np.array(keys, dtype=object)[idx]


def make_text(rng: np.random.Generator, core_words: list[str], length_words: int) -> str:
    """Текст сообщения: ядро темы + общий фон + уникальный шум — пропорции
    подобраны так, чтобы и кластеризация, и TF-IDF имели что зацепить."""
    n_core = max(1, int(length_words * 0.35))
    n_bg = max(1, int(length_words * 0.5))
    n_noise = max(0, length_words - n_core - n_bg)
    core = rng.choice(core_words, size=n_core, replace=True)
    bg = rng.choice(BACKGROUND_WORDS, size=n_bg, replace=True)
    noise = [f"слово{rng.integers(0, 100000)}" for _ in range(n_noise)]
    words = list(core) + list(bg) + noise
    rng.shuffle(words)
    sentence = " ".join(words)
    # Заглавная первая буква + точка — чтобы _first_sentence видел законченную
    # фразу, а не бесконечный поток строчных слов.
    return sentence[:1].upper() + sentence[1:] + "."


def make_dates(rng: np.random.Generator, size: int, days: int) -> tuple[np.ndarray, np.ndarray]:
    """Даты со смещением в рабочие часы (10:00-19:00) — как в реальном трафике
    соцсетей, а не равномерно по всем суткам."""
    day_offset = rng.integers(0, days, size=size)
    hour = np.clip(rng.normal(loc=14, scale=3, size=size), 0, 23).astype(int)
    minute = rng.integers(0, 60, size=size)
    base = pd.Timestamp("2026-04-20")
    dates = [base + pd.Timedelta(days=int(d)) for d in day_offset]
    date_str = np.array([d.strftime("%d.%m.%Y") for d in dates], dtype=object)
    time_str = np.array([f"{h:02d}:{m:02d}" for h, m in zip(hour, minute)], dtype=object)
    return date_str, time_str


def format_number(rng: np.random.Generator, value: int, grouped_prob: float = 0.3) -> str:
    """Число с пробелом-разделителем тысяч в части ячеек — как в BA: чистка
    чисел в preprocess.as_int обязана сработать, а не остаться неупражнённой."""
    if value <= 0:
        return ""
    if value >= 1000 and rng.random() < grouped_prob:
        s = f"{value:,}".replace(",", " ")
        return s
    return str(value)


def canonicalize_synthetic(raw: pd.DataFrame, *, branch: str, source_file: str) -> pd.DataFrame:
    """Канонизировать синтетику с явной системой источника.

    detect_source_system (import_adapters.py) узнаёт Brand Analytics по паре
    колонок «ID сообщения» + «Тип источника»/Url — они остаются и в algo-ветке
    (реалистичная выгрузка тоже их не прячет). Auto-детект в этом случае
    определил бы обе ветки как brand_analytics. Раз ветка выбрана явно флагом
    --branch, канонизация должна ей и следовать, а не гадать по колонкам.
    """
    source_system = "brand_analytics" if branch == "ba" else "generic"
    return canonicalize_table(raw, source_file=source_file, source_system=source_system)


def build_raw_export(
    *,
    messages: int,
    stories: int,
    chats: int,
    authors: int,
    days: int,
    seed: int,
    branch: str,
    story_share: float = 0.25,
    duplicate_share: float = 0.10,
    tag_columns: int = 6,
) -> pd.DataFrame:
    """Собрать сырую выгрузку Brand Analytics (branch='ba') или обычную,
    без разметки сюжетов (branch='algo')."""
    rng = np.random.default_rng(seed)
    n = int(messages)

    story_sizes = zipf_sizes(rng, stories, n)
    story_idx_pool = np.repeat(np.arange(stories), story_sizes)[:n]
    rng.shuffle(story_idx_pool)

    chat_sizes = zipf_sizes(rng, chats, n)
    chat_idx_pool = np.repeat(np.arange(chats), chat_sizes)[:n]
    rng.shuffle(chat_idx_pool)

    author_sizes = zipf_sizes(rng, authors, n)
    author_idx_pool = np.repeat(np.arange(authors), author_sizes)[:n]
    rng.shuffle(author_idx_pool)

    date_str, time_str = make_dates(rng, n, days)
    sentiments = _weighted_choice(rng, SENTIMENT_WEIGHTS, n)
    kinds = _weighted_choice(rng, KIND_WEIGHTS, n)
    lengths = np.clip(rng.lognormal(mean=4.4, sigma=0.7, size=n), 15, 260).astype(int)

    has_story = rng.random(n) < story_share if branch == "ba" else np.zeros(n, dtype=bool)
    is_duplicate = rng.random(n) < duplicate_share

    # Тексты: для дублей копируем текст, уже сгенерированный РАНЕЕ для того же
    # сюжета (донор может как иметь, так и не иметь заполненный «Сюжет» —
    # оба вида дубликатов важны для истории восстановления сюжетов).
    texts: list[str] = [""] * n
    per_story_seen: dict[int, list[int]] = {}
    for i in range(n):
        story_idx = int(story_idx_pool[i])
        donors = per_story_seen.get(story_idx)
        if is_duplicate[i] and donors:
            texts[i] = texts[rng.choice(donors)]
        else:
            texts[i] = make_text(rng, CORE_TOPICS[story_idx % len(CORE_TOPICS)][1], int(lengths[i]))
            per_story_seen.setdefault(story_idx, []).append(i)

    rows: dict[str, Any] = {
        "ID сообщения": [f"msg_{i}" for i in range(n)],
        "Hash сообщения": [f"hash_{i}" for i in range(n)],
        "Дата": date_str,
        "Время": time_str,
        "Сообщение": texts,
        "Автор": [f"author_{a}" for a in author_idx_pool],
        "Url": [f"https://vk.com/wall{c}_{i}" for c, i in zip(chat_idx_pool, range(n))],
        "Источник": [f"chat_{c}" for c in chat_idx_pool],
        "Тональность": list(sentiments),
        "Аудитория": [format_number(rng, int(rng.integers(200, 50000))) for _ in range(n)],
        "Просмотры": [format_number(rng, int(rng.integers(50, 20000))) for _ in range(n)],
        "Вовлеченность": [format_number(rng, int(rng.integers(0, 500))) for _ in range(n)],
    }

    # Тип площадки/тип сообщения — от них зависит kind (message_kinds.py) и,
    # значит, кто попадёт в кандидаты на инфоповод (EVENT_CANDIDATE_KINDS).
    platform_type = np.where(kinds == "review", "Отзывы", "Соцсети")
    message_type = np.select(
        [kinds == "repost", kinds == "comment", kinds == "review"],
        ["Репост", "Комментарий", "Оценка"],
        default="Пост",
    )
    rows["Тип источника"] = list(platform_type)
    rows["Тип"] = list(message_type)

    if branch == "ba":
        rows["Сюжет"] = [
            CORE_TOPICS[story_idx_pool[i] % len(CORE_TOPICS)][0] if has_story[i] else ""
            for i in range(n)
        ]
        rows["Обработано"] = "да"
        # Колонки-теги Brand Analytics: разреженное неравномерное заполнение.
        tag_fill_probs = [0.45, 0.30, 0.20, 0.12, 0.07, 0.03][:tag_columns]
        while len(tag_fill_probs) < tag_columns:
            tag_fill_probs.append(0.03)
        for t in range(tag_columns):
            label = f"Тег{t + 1}"
            probs = rng.random(n) < tag_fill_probs[t]
            rows[label] = np.where(probs, label, "")

    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# Измеритель: время + пиковая память по шагам
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    label: str
    seconds: float
    peak_mb: float
    rows: int | None = None


@dataclass
class Meter:
    trace: bool
    steps: list[StepResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.trace and not tracemalloc.is_tracing():
            tracemalloc.start()

    def step(self, label: str, rows: int | None = None):
        return _StepContext(self, label, rows)

    def record(self, label: str, seconds: float, peak_mb: float, rows: int | None) -> None:
        self.steps.append(StepResult(label=label, seconds=seconds, peak_mb=peak_mb, rows=rows))


class _StepContext:
    def __init__(self, meter: Meter, label: str, rows: int | None) -> None:
        self.meter = meter
        self.label = label
        self.rows = rows

    def __enter__(self):
        gc.collect()
        if self.meter.trace:
            tracemalloc.reset_peak()
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed = time.monotonic() - self._start
        peak_mb = 0.0
        if self.meter.trace:
            _, peak = tracemalloc.get_traced_memory()
            peak_mb = peak / (1024 * 1024)
        self.meter.record(self.label, elapsed, peak_mb, self.rows)


def peak_rss_mb() -> float | None:
    """Пиковый resident set size — если получится узнать.

    tracemalloc — нижняя оценка (видит только буферы numpy/python, не
    C-аллокации scipy/sklearn). На Linux VmHWM из /proc — это именно то,
    что видит cgroup-лимит контейнера.
    """
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("VmHWM:"):
                kb = int(line.split()[1])
                return kb / 1024
    except Exception:  # noqa: BLE001 — не Linux или файла нет, это не ошибка
        pass
    try:
        import psutil  # type: ignore

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 — psutil не в requirements.txt, это опция
        return None


# ---------------------------------------------------------------------------
# Инструментированный прогон: те же шаги, что build_processed_tables
# ---------------------------------------------------------------------------


def run_instrumented(
    canonical: pd.DataFrame,
    out_dir: Path,
    meter: Meter,
    *,
    window_minutes: int = 60,
    similarity_threshold: float = 0.28,
    event_gap_hours: float = 3.0,
    event_window_hours: float = 16.0,
) -> dict[str, Any]:
    """Повторяет build_processed_tables (src/preprocess.py) по шагам, каждый —
    в своём meter.step(...). Порядок шагов должен точно следовать оригиналу —
    иначе --verify поймает расхождение."""
    with meter.step("detect_tag_columns", rows=len(canonical)):
        tag_cols = detect_tag_columns(canonical)
    with meter.step("is_brand_analytics_dataframe"):
        is_ba = is_brand_analytics_dataframe(canonical)
    with meter.step("normalize_messages", rows=len(canonical)):
        messages, message_tags = normalize_messages(canonical, tag_cols)

    story_candidates = 0
    if is_ba:
        with meter.step("recover_stories", rows=len(messages)):
            # Кандидаты — то самое N квадратичного шага (story_recovery.py):
            # без сюжета, непустой текст, kind в {post, repost}.
            from services.message_kinds import EVENT_CANDIDATE_KINDS, classify_kinds

            kinds = classify_kinds(messages)
            no_story = messages.get("source_main_topic", pd.Series([""] * len(messages))).fillna("").astype(str).str.strip() == ""
            has_text = messages.get("text_clean", pd.Series([""] * len(messages))).fillna("").astype(str).str.strip() != ""
            story_candidates = int((no_story & has_text & kinds.isin(EVENT_CANDIDATE_KINDS)).sum())
            recovered = recover_stories(messages)
            messages["source_main_topic"] = recovered["story"]
            messages["story_origin"] = recovered["story_origin"]
            if "source_topics" in messages.columns:
                missing = messages["source_topics"].fillna("").astype(str).str.strip() == ""
                messages.loc[missing, "source_topics"] = messages.loc[missing, "source_main_topic"]

    with meter.step("make_discussions", rows=len(messages)):
        discussions, discussion_messages = make_discussions(messages, window_minutes=window_minutes)

    if is_ba:
        with meter.step("make_events_from_source_stories", rows=len(discussions)):
            events, event_discussions = make_events_from_source_stories(discussions)
        cluster_method_used = "brand_analytics_story"
    else:
        with meter.step("select_clusterable", rows=len(discussions)):
            clusterable = discussions[discussions["discussion_text"].fillna("").str.len() > 10].copy()
            non_clusterable = discussions.drop(clusterable.index).copy()
        with meter.step("cluster_discussions_tfidf", rows=len(clusterable)):
            labels = cluster_discussions_tfidf(
                clusterable,
                similarity_threshold=similarity_threshold,
                max_gap_hours=event_gap_hours,
                max_event_span_hours=event_window_hours,
            )
        if len(clusterable):
            with meter.step("refine_labels_by_tag", rows=len(clusterable)):
                labels = refine_labels_by_tag(labels, clusterable)
            with meter.step("split_labels_by_time_gap", rows=len(clusterable)):
                labels = split_labels_by_time_gap(labels, clusterable, max_gap_hours=event_gap_hours)
            with meter.step("split_labels_by_fixed_time_window", rows=len(clusterable)):
                labels = split_labels_by_fixed_time_window(labels, clusterable, window_hours=event_window_hours)
        if len(non_clusterable):
            start_label = int(labels.max()) + 1 if len(labels) else 0
            singleton = pd.Series(range(start_label, start_label + len(non_clusterable)), index=non_clusterable.index)
            all_discussions = pd.concat([clusterable, non_clusterable], axis=0).sort_index()
            all_labels = pd.concat([labels, singleton]).loc[all_discussions.index]
        else:
            all_discussions = clusterable
            all_labels = labels
        with meter.step("apply_event_quality_gate", rows=len(all_discussions)):
            all_labels = apply_event_quality_gate(all_labels, all_discussions, messages, discussion_messages)
        with meter.step("make_events", rows=len(all_discussions)):
            events, event_discussions = make_events(all_discussions, all_labels)
        cluster_method_used = "tfidf"

    with meter.step("write_tables", rows=len(messages)):
        paths = {
            "messages": str(write_table(messages, out_dir, "messages")),
            "message_tags": str(write_table(message_tags, out_dir, "message_tags")),
            "discussions": str(write_table(discussions, out_dir, "discussions")),
            "discussion_messages": str(write_table(discussion_messages, out_dir, "discussion_messages")),
            "events": str(write_table(events, out_dir, "events")),
            "event_discussions": str(write_table(event_discussions, out_dir, "event_discussions")),
        }
        manifest = {
            "rows_source": int(len(canonical)),
            "rows_messages": int(len(messages)),
            "rows_discussions": int(len(discussions)),
            "rows_events": int(len(events)),
            "cluster_method": cluster_method_used,
            "event_source": "brand_analytics_story" if is_ba else "algorithmic_cluster",
            "story_candidates": story_candidates,
            "paths": paths,
        }
        write_manifest(out_dir, manifest)
    return manifest


def verify_against_reference(canonical: pd.DataFrame, manifest: dict[str, Any]) -> list[str]:
    """Сверить инструментированный прогон со штатным run_preprocess_from_dataframe."""
    problems: list[str] = []
    with TemporaryDirectory() as ref_dir:
        ref_manifest = run_preprocess_from_dataframe(canonical, output=ref_dir, source_file="loadtest.xlsx")
    for key in ("rows_messages", "rows_discussions", "rows_events", "cluster_method", "event_source"):
        if manifest.get(key) != ref_manifest.get(key):
            problems.append(f"{key}: инструментированный={manifest.get(key)!r}, штатный={ref_manifest.get(key)!r}")
    return problems


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------


def format_report(meter: Meter, manifest: dict[str, Any], rss_mb: float | None, canon_seconds: float) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append("Нагрузочный прогон конвейера обработки")
    lines.append("=" * 78)
    total_seconds = sum(s.seconds for s in meter.steps)
    lines.append(f"Канонизация входа: {canon_seconds:.2f} с")
    lines.append("")
    lines.append(f"{'Шаг':<40}{'секунды':>10}{'доля,%':>9}{'пик, МБ':>12}{'строк':>10}")
    for s in meter.steps:
        share = (s.seconds / total_seconds * 100) if total_seconds else 0.0
        rows_s = "" if s.rows is None else str(s.rows)
        lines.append(f"{s.label:<40}{s.seconds:>10.2f}{share:>9.1f}{s.peak_mb:>12.1f}{rows_s:>10}")
    lines.append("-" * 78)
    lines.append(f"{'ИТОГО (сумма шагов)':<40}{total_seconds:>10.2f}")
    if meter.trace:
        global_peak = max((s.peak_mb for s in meter.steps), default=0.0)
        lines.append(f"Глобальный пик tracemalloc: {global_peak:.1f} МБ (нижняя оценка)")
    if rss_mb is not None:
        lines.append(f"Пиковый RSS процесса: {rss_mb:.1f} МБ")
    else:
        lines.append("Пиковый RSS процесса: недоступен (не Linux и psutil не установлен)")
    lines.append("")
    lines.append(f"Строк на входе: {manifest.get('rows_source')}")
    lines.append(f"Сообщений: {manifest.get('rows_messages')}")
    lines.append(f"Обсуждений: {manifest.get('rows_discussions')}")
    lines.append(f"Инфоповодов: {manifest.get('rows_events')}")
    lines.append(f"Ветка: {manifest.get('event_source')} / {manifest.get('cluster_method')}")
    if manifest.get("story_candidates") is not None:
        lines.append(f"Кандидатов в восстановление сюжетов (N квадратичного шага): {manifest.get('story_candidates')}")
    lines.append(f"pandas {pd.__version__}, numpy {np.__version__}, python {sys.version.split()[0]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Нагрузочный прогон конвейера обработки")
    parser.add_argument("--messages", type=int, default=50000)
    parser.add_argument("--chats", type=int, default=400)
    parser.add_argument("--authors", type=int, default=6000)
    parser.add_argument("--stories", type=int, default=150)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--branch", choices=["ba", "algo"], default="ba")
    parser.add_argument("--story-share", type=float, default=0.25)
    parser.add_argument("--duplicate-share", type=float, default=0.10)
    parser.add_argument("--tags", type=int, default=6)
    parser.add_argument("--out-dir", type=str, default="")
    parser.add_argument("--json", type=str, default="")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--force", action="store_true", help="разрешить писать в data/processed")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    default_out = (ROOT / "data" / "processed").resolve()
    tmp_ctx = None
    if args.out_dir:
        out_dir = Path(args.out_dir).resolve()
        if out_dir == default_out and not args.force:
            print(
                "Отказ: --out-dir указывает на data/processed, а build_processed_tables "
                "перед записью удаляет оттуда прошлые таблицы. Укажите другую папку или "
                "добавьте --force, если это осознанно.",
                file=sys.stderr,
            )
            return 2
    else:
        tmp_ctx = TemporaryDirectory(prefix="loadtest_")
        out_dir = Path(tmp_ctx.name)

    try:
        print(f"Генерация синтетики: {args.messages} сообщений, ветка «{args.branch}»...")
        t0 = time.monotonic()
        raw = build_raw_export(
            messages=args.messages,
            stories=args.stories,
            chats=args.chats,
            authors=args.authors,
            days=args.days,
            seed=args.seed,
            branch=args.branch,
            story_share=args.story_share,
            duplicate_share=args.duplicate_share,
            tag_columns=args.tags,
        )
        gen_seconds = time.monotonic() - t0

        t0 = time.monotonic()
        canonical = canonicalize_synthetic(
            raw, branch=args.branch, source_file=f"loadtest_{args.branch}.xlsx"
        )
        canon_seconds = time.monotonic() - t0
        print(f"Синтетика готова за {gen_seconds:.1f} с, канонизация за {canon_seconds:.1f} с. Строк: {len(canonical)}.")

        detected_tags = detect_tag_columns(canonical)
        if args.branch == "ba" and not detected_tags:
            print(
                "ВНИМАНИЕ: не найдено ни одной колонки-тега — выгрузка уйдёт в "
                "алгоритмическую ветку вместо Brand Analytics. Проверьте генератор.",
                file=sys.stderr,
            )

        meter = Meter(trace=not args.no_memory)
        manifest = run_instrumented(canonical, out_dir, meter)

        expected_source = "brand_analytics_story" if args.branch == "ba" else "algorithmic_cluster"
        if manifest.get("event_source") != expected_source:
            print(
                f"ВНИМАНИЕ: ожидалась ветка {expected_source!r}, получена "
                f"{manifest.get('event_source')!r}.",
                file=sys.stderr,
            )

        rss_mb = None if args.no_memory else peak_rss_mb()
        report = format_report(meter, manifest, rss_mb, canon_seconds)
        print()
        print(report)

        if args.verify:
            print()
            print("Сверка со штатным run_preprocess_from_dataframe...")
            problems = verify_against_reference(canonical, manifest)
            if problems:
                print("РАСХОЖДЕНИЯ:")
                for p in problems:
                    print(f"  - {p}")
                return 1
            print("Совпадает.")

        if args.json:
            payload = {
                "args": vars(args),
                "manifest": manifest,
                "steps": [
                    {"label": s.label, "seconds": s.seconds, "peak_mb": s.peak_mb, "rows": s.rows}
                    for s in meter.steps
                ],
                "peak_rss_mb": rss_mb,
                "canonicalize_seconds": canon_seconds,
            }
            Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nJSON-отчёт: {args.json}")

        return 0
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
