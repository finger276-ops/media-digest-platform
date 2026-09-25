"""Классификация микротемы сообщения по регекс-правилам.

Вынесено из preprocess.py при распиле монолита.
"""

from __future__ import annotations

import functools
import re
from typing import Iterable

from .tag_parsing import label_microtopic, normalize_label, tag_set
from .text_cleaning import normalize_spaces


def regex_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


# Правила микротемы: первое сработавшее правило задаёт микротему, порядок
# важен — проблемы и претензии проверяются раньше всего остального.
# Правило срабатывает, если в тексте нашёлся хотя бы один из шаблонов.
_RULE_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "issue_problem",
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
    ),
    (
        "price_terms",
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
    ),
    (
        "product_quality",
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
    ),
    (
        "availability_supply",
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
    ),
    (
        "installation_usage",
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
    ),
    (
        "documents_certificates",
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
    ),
    (
        "safety_fire",
        [
            r"пожар\w*",
            r"огне\w*",
            r"горюч\w*",
            r"негорюч\w*",
            r"безопасност\w*",
            r"опасн\w*",
            r"токсич\w*",
        ],
    ),
    (
        "sustainability_energy",
        [
            r"эколог\w*",
            r"переработ\w*",
            r"углерод\w*",
            r"энергоэффектив\w*",
            r"энергосбереж\w*",
            r"теплопотер\w*",
            r"устойчив\w*",
        ],
    ),
    (
        "competitors_market",
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
    ),
    (
        "customer_service",
        [
            r"поддержк\w*",
            r"сервис\w*",
            r"менеджер\w*",
            r"дилер\w*",
            r"продавец\w*",
            r"магазин\w*",
            r"клиент\w*",
        ],
    ),
]


def _literal_core(pattern: str) -> str | None:
    """Самый длинный кусок букв, без которого шаблон совпасть не может.

    Из шаблона убираются необязательные группы (?:…)?, экранированные классы
    (\\b, \\w, \\s) и буквы под квантификатором ? или *. Если в шаблоне есть
    альтернатива «|» или класс символов — ядра нет, шаблон проверяется
    регулярным выражением всегда.
    """
    rest = re.sub(r"\(\?:[^)]*\)\??", " ", pattern)
    rest = re.sub(r"\\.", " ", rest)
    if "|" in rest or "[" in rest or "(" in rest:
        return None
    rest = re.sub(r"[^\W\d_][?*]", " ", rest)
    runs = re.findall(r"[^\W\d_]+", rest)
    return max(runs, key=len) if runs else None


# Регулярное выражение с re.IGNORECASE проверяет шаблон с каждой позиции
# текста, и на 20 000 сообщений микротема занимала 8 из 11 секунд разбора
# выгрузки. Почти все шаблоны устроены просто: «ядро с начала слова»
# (\bцен\w*) или «ядро где угодно» (тариф\w*). Для них регулярка не нужна:
# ядро ищется обычным поиском подстроки, а граница слова \b перед ним — это
# ровно «предыдущий символ не буква, не цифра и не _» (так \w определён в re).
# Хвост \w* на то, есть ли совпадение, не влияет. Остальные шаблоны
# («не\s+работа», «\bакци(?:я|и…)?\b») проверяются регуляркой, но только
# если в тексте нашлось их ядро.
#
# Результат тот же: текст уже в нижнем регистре, и совпасть с шаблоном без
# учёта регистра может только текст, где ядро есть буквально. Исключение —
# символы, которые регулярное выражение считает той же буквой («ᲂ» — это «о»,
# «ſ» — «s»). Их список вычисляется тем же движком регулярок (_lookalikes_re),
# и текст с любым из них проверяется по-старому, полным выражением правила.
_WORD_START = "word_start"
_ANYWHERE = "anywhere"
_REGEX = "regex"
_PLAIN_WORD_START_RE = re.compile(r"\\b([^\W\d_]+)(?:\\w\*)?")
_PLAIN_ANYWHERE_RE = re.compile(r"([^\W\d_]+)(?:\\w\*)?")


def _matcher(pattern: str) -> tuple[str, str | None, re.Pattern]:
    """(вид проверки, ядро, скомпилированный шаблон) для одного шаблона правила."""
    compiled = re.compile(pattern, re.IGNORECASE)
    word_start = _PLAIN_WORD_START_RE.fullmatch(pattern)
    if word_start:
        return _WORD_START, word_start.group(1), compiled
    anywhere = _PLAIN_ANYWHERE_RE.fullmatch(pattern)
    if anywhere:
        return _ANYWHERE, anywhere.group(1), compiled
    return _REGEX, _literal_core(pattern), compiled


def _starts_word(core: str, text: str) -> bool:
    """Есть ли в тексте ядро, перед которым граница слова, как у \\b."""
    position = text.find(core)
    while position != -1:
        if position == 0:
            return True
        before = text[position - 1]
        if not (before.isalnum() or before == "_"):
            return True
        position = text.find(core, position + 1)
    return False


def _plain_match(matchers, text: str) -> bool:
    for kind, core, compiled in matchers:
        if kind == _WORD_START:
            if _starts_word(core, text):
                return True
        elif kind == _ANYWHERE:
            if core in text:
                return True
        elif core is None or core in text:
            if compiled.search(text):
                return True
    return False


_RULES: list[tuple[str, re.Pattern, list]] = [
    (
        _label,
        re.compile("|".join(f"(?:{_p})" for _p in _patterns), re.IGNORECASE),
        [_matcher(_p) for _p in _patterns],
    )
    for _label, _patterns in _RULE_PATTERNS
]


def _case_lookalikes(letters: set[str]) -> str:
    """Символы, которые re.IGNORECASE считает одной из этих букв, кроме самих букв."""
    letter_class = re.compile(
        "[" + re.escape("".join(sorted(letters))) + "]", re.IGNORECASE
    )
    every_char = "".join(map(chr, range(0x110000)))
    return "".join(sorted(set(letter_class.findall(every_char)) - letters))


@functools.lru_cache(maxsize=1)
def _lookalikes_re() -> re.Pattern:
    """Считается при первом разборе, а не при импорте: проход по всей таблице
    Unicode занимает доли секунды, и страницам без разбора платить за него
    незачем. Буквы берутся из самих шаблонов, а не только из ядер: двойник
    любой буквы шаблона — повод проверить текст полным выражением."""
    letters = {
        ch
        for _, patterns in _RULE_PATTERNS
        for pattern in patterns
        for ch in re.sub(r"\\.", "", pattern)
        if ch.isalpha()
    }
    return re.compile("[" + re.escape(_case_lookalikes(letters)) + "]")


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
    plain = _lookalikes_re().search(t) is None
    for label, rule, matchers in _RULES:
        if _plain_match(matchers, t) if plain else rule.search(t):
            return label

    # Source-provided labels are the strongest universal signal. Use them as
    # stable buckets after generic risk/problem buckets have had a chance to
    # catch operational issues.
    if tag_values_clean:
        preferred = sorted(tag_values_clean, key=lambda x: (len(x), x.lower()))[0]
        return label_microtopic(preferred)

    return "general"
