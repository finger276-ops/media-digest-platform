# -*- coding: utf-8 -*-
"""
Фильтр шума маркетплейсов для выгрузок медиамониторинга.

Основано на анализе РЕАЛЬНЫХ выгрузок Knauf (Brand Analytics):
среди сообщений встречается шум, не несущий смысла для аналитики:
  - ответы продавцов на отзывы («спасибо за отзыв», «добавляйте в избранное»);
  - пустые отзывы без содержания о продукте («всё отлично», «рекомендую»).

Философия — ОСТОРОЖНОСТЬ: лучше пропустить немного шума, чем удалить
содержательный отзыв. Поэтому:
  - уверенно помечаем только ответы продавцов (это точно не мнение потребителя);
  - пустые отзывы помечаем, ТОЛЬКО если в них нет слов о свойствах продукта;
  - ничего не удаляем — только помечаем (is_noise), решение за человеком.

Без LLM — быстрые эвристики. Настройки под клиента можно расширять.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class NoiseFilterConfig:
    """Настройки фильтра шума (можно подстроить под клиента)."""
    # Фразы-маркеры ответов продавца/магазина
    seller_patterns: list[str] = field(default_factory=lambda: [
        "спасибо за отзыв", "спасибо за заказ", "спасибо, что нашли время",
        "спасибо за оценку", "благодарим за выбор", "добавляйте нас в избранное",
        "рады, что вы остались довольны", "приятного использования",
        "будем рады новым заказам", "с уважением, команда", "с уважением, магазин",
        "будем рады видеть вас снова", "ждём вас снова",
    ])
    # Пустые оценочные фразы (шум, если нет слов о продукте)
    empty_review_phrases: list[str] = field(default_factory=lambda: [
        "все отлично", "всё отлично", "хороший товар", "рекомендую",
        "всем советую", "отличный продавец", "быстрая доставка",
        "спасибо продавцу", "все супер", "всё супер", "все хорошо", "всё хорошо",
    ])
    # Слова о свойствах продукта — если есть, отзыв СОДЕРЖАТЕЛЬНЫЙ, не трогаем
    product_words: list[str] = field(default_factory=lambda: [
        "плотн", "удобн", "тёпл", "тепл", "мягк", "жёстк", "жестк",
        "пылит", "крошит", "держит форму", "толщин", "режет", "монтаж",
        "укладк", "упаковк", "куб", "рулон", "плита", "вата", "утеплит",
        "изоляц", "шумоизол", "звукоизол", "не чешет", "чешет",
    ])
    # Максимальная длина «пустого» отзыва (длинные не считаем пустыми)
    empty_review_max_len: int = 100


@dataclass
class NoiseResult:
    """Результат проверки одного сообщения."""
    is_noise: bool = False
    noise_type: str = ""     # 'seller_reply' | 'empty_review' | ''


def _is_seller_reply(text: str, cfg: NoiseFilterConfig) -> bool:
    low = text.lower()
    return any(p in low for p in cfg.seller_patterns)


def _is_empty_review(text: str, cfg: NoiseFilterConfig) -> bool:
    if len(text) > cfg.empty_review_max_len:
        return False
    low = text.lower().strip()
    has_empty = any(p in low for p in cfg.empty_review_phrases)
    has_product = any(p in low for p in cfg.product_words)
    return has_empty and not has_product


def classify_noise(text: str, cfg: NoiseFilterConfig | None = None) -> NoiseResult:
    """Проверить одно сообщение на шум. Возвращает тип шума или пусто."""
    cfg = cfg or NoiseFilterConfig()
    if not text or not text.strip():
        return NoiseResult(is_noise=False)
    if _is_seller_reply(text, cfg):
        return NoiseResult(is_noise=True, noise_type="seller_reply")
    if _is_empty_review(text, cfg):
        return NoiseResult(is_noise=True, noise_type="empty_review")
    return NoiseResult(is_noise=False)


def filter_dataframe(df, text_column: str = "Сообщение", cfg: NoiseFilterConfig | None = None):
    """
    Пометить шум в DataFrame выгрузки.

    Добавляет две колонки:
      - is_noise (bool) — помечено как шум;
      - noise_type (str) — тип шума.
    НИЧЕГО не удаляет. Возвращает (df_с_пометками, статистика).
    """
    cfg = cfg or NoiseFilterConfig()
    if text_column not in df.columns:
        return df, {"error": f"колонка '{text_column}' не найдена"}

    results = df[text_column].fillna("").astype(str).apply(
        lambda t: classify_noise(t, cfg)
    )
    df = df.copy()
    df["is_noise"] = [r.is_noise for r in results]
    df["noise_type"] = [r.noise_type for r in results]

    stats = {
        "total": len(df),
        "noise_total": int(df["is_noise"].sum()),
        "seller_replies": int((df["noise_type"] == "seller_reply").sum()),
        "empty_reviews": int((df["noise_type"] == "empty_review").sum()),
    }
    stats["noise_percent"] = (
        round(stats["noise_total"] / stats["total"] * 100, 1) if stats["total"] else 0
    )
    return df, stats
