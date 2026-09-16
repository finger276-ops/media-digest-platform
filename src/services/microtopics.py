"""Классификация микротемы сообщения по регекс-правилам.

Вынесено из preprocess.py при распиле монолита.
"""

from __future__ import annotations

import re
from typing import Iterable

from .tag_parsing import label_microtopic, normalize_label, tag_set
from .text_cleaning import normalize_spaces


def regex_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def classify_microtopic(text: str, tags: str | Iterable[str] = "") -> str:
    """
    Rule-based microtopic layer.

    Правила универсальные и работают для любой выгрузки мониторинга. Если в
    исходном файле уже есть темы или теги, самая конкретная метка источника
    становится устойчивой технической микротемой.
    """
    t = normalize_spaces(text).lower().replace("ё", "е")
    if isinstance(tags, str):
        tag_values = tag_set(tags)
    else:
        tag_values = {str(x).strip() for x in tags if str(x).strip()}
    tag_values_clean = {normalize_label(x) for x in tag_values}
    tag_values_clean = {x for x in tag_values_clean if x}
    # Универсальные правила: работают для любой выгрузки мониторинга.
    if regex_any(
        t,
        [
            r"\bпроблем\w*",
            r"\bжалоб\w*",
            r"\bнедоволь\w*",
            r"не\s+работа\w*",
            r"\bошибк\w*",
            r"\bсбой\w*",
            r"\bдефект\w*",
            r"\bбрак\w*",
            r"\bповрежд\w*",
            r"\bплох\w*",
            r"\bнегатив\w*",
        ],
    ):
        return "issue_problem"
    if regex_any(
        t,
        [
            r"\bцен\w*",
            r"\bстоимост\w*",
            r"\bпрайс\w*",
            r"тариф\w*",
            r"\bскидк\w*",
            r"\bакци(?:я|и|ю|ей|ями|онн\w*)?\b",
            r"\bдешев\w*",
            r"\bдорог\w*",
            r"\bоплат\w*",
            r"\bсчет\w*",
            r"\bсчёт\w*",
        ],
    ):
        return "price_terms"
    if regex_any(
        t,
        [
            r"\bкачеств\w*",
            r"\bхарактерист\w*",
            r"\bматериал\w*",
            r"\bпрочност\w*",
            r"\bплотност\w*",
            r"\bтолщин\w*",
            r"\bразмер\w*",
            r"\bупаковк\w*",
            r"плесен\w*",
            r"плесён\w*",
        ],
    ):
        return "product_quality"
    if regex_any(
        t,
        [
            r"\bналич\w*",
            r"\bсклад\w*",
            r"\bпоставк\w*",
            r"\bдоставк\w*",
            r"\bлогист\w*",
            r"\bотгруз\w*",
            r"\bсрок\w*",
            r"\bзаказ\w*",
            r"\bдефицит\w*",
        ],
    ):
        return "availability_supply"
    if regex_any(
        t,
        [
            r"\bмонтаж\w*",
            r"\bустанов\w*",
            r"\bприменен\w*",
            r"\bиспользован\w*",
            r"\bэксплуатац\w*",
            r"\bстроитель\w*",
            r"\bутепл\w*",
            r"\bизоляц\w*",
            r"\bкровл\w*",
            r"\bфасад\w*",
        ],
    ):
        return "installation_usage"
    if regex_any(
        t,
        [
            r"сертификат\w*",
            r"документ\w*",
            r"деклараци\w*",
            r"гост\w*",
            r"снип\w*",
            r"требован\w*",
            r"регламент\w*",
            r"стандарт\w*",
        ],
    ):
        return "documents_certificates"
    if regex_any(
        t,
        [
            r"пожар\w*",
            r"огне\w*",
            r"горюч\w*",
            r"негорюч\w*",
            r"безопасност\w*",
            r"опасн\w*",
            r"токсич\w*",
        ],
    ):
        return "safety_fire"
    if regex_any(
        t,
        [
            r"эколог\w*",
            r"переработ\w*",
            r"углерод\w*",
            r"энергоэффектив\w*",
            r"энергосбереж\w*",
            r"теплопотер\w*",
            r"устойчив\w*",
        ],
    ):
        return "sustainability_energy"
    if regex_any(
        t,
        [
            r"конкурент\w*",
            r"аналог\w*",
            r"сравнен\w*",
            r"рынок\w*",
            r"бренд\w*",
            r"rockwool",
            r"роквул",
            r"технониколь",
            r"ursa",
            r"изовер",
        ],
    ):
        return "competitors_market"
    if regex_any(
        t,
        [
            r"поддержк\w*",
            r"сервис\w*",
            r"менеджер\w*",
            r"дилер\w*",
            r"продавец\w*",
            r"магазин\w*",
            r"клиент\w*",
        ],
    ):
        return "customer_service"

    # Source-provided labels are the strongest universal signal. Use them as
    # stable buckets after generic risk/problem buckets have had a chance to
    # catch operational issues.
    if tag_values_clean:
        preferred = sorted(tag_values_clean, key=lambda x: (len(x), x.lower()))[0]
        return label_microtopic(preferred)

    return "general"
