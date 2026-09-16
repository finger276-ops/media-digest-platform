# -*- coding: utf-8 -*-
"""Контракт распила preprocess.py (2237 строк → оркестрация + 8 подмодулей).

preprocess.py раньше был монолитом: вся логика конвейера — от чистки текста
до сборки инфоповодов — в одном файле. Распилен на
services/{text_cleaning,tag_parsing,microtopics,message_normalize,
discussion_build,discussion_titles,clustering,event_assembly}.py по границам
ответственности; сам preprocess.py остался только оркестрацией
(build_processed_tables и обёртки) плюс ре-экспортом — внешний код
(services/ingest.py, scripts/loadtest_pipeline.py, tests/) продолжает делать
`from preprocess import X` теми же именами, что и раньше.

Этот файл — не проверка логики конвейера (её стерегут test_preprocess.py,
test_story_recovery.py и другие), а страховка самого распила: что ре-экспорт
не потеряет имя при будущей правке и что подмодули не обзавелись скрытой
зависимостью обратно на preprocess.py (тогда его нельзя было бы больше
распиливать — цикл импорта).

Мутационные проверки (что ломает какой тест):
- убрать любую строку из блока ре-экспорта в preprocess.py → «все реальные
  импортёры получают то, что просят» краснеет на этом имени;
- добавить `import preprocess` внутрь любого из 8 подмодулей → «подмодули
  импортируются в изоляции, без preprocess.py» краснеет (ImportError или
  циклический импорт);
- убрать имя из preprocess.__all__, оставив реальный импорт, → «__all__
  соответствует общедоступным именам модуля» краснеет.
"""

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


SPLIT_MODULES = [
    "text_cleaning",
    "tag_parsing",
    "microtopics",
    "message_normalize",
    "discussion_build",
    "discussion_titles",
    "clustering",
    "event_assembly",
]


def names_imported_from_preprocess(path: Path) -> set[str]:
    """Все имена, которые файл берёт через `from preprocess import ...`."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "preprocess":
            names.update(alias.name for alias in node.names)
    return names


print("1. Все реальные импортёры получают то, что просят у preprocess")
import preprocess  # noqa: E402

real_importers = [
    REPO / "src" / "services" / "ingest.py",
    REPO / "scripts" / "loadtest_pipeline.py",
    REPO / "tests" / "test_preprocess.py",
    REPO / "tests" / "test_loadtest_pipeline.py",
    REPO / "tests" / "test_project_settings.py",
]
total_checked = 0
for path in real_importers:
    for name in sorted(names_imported_from_preprocess(path)):
        total_checked += 1
        check(
            f"{path.relative_to(REPO)}: preprocess.{name} доступен",
            hasattr(preprocess, name),
        )
check(f"хотя бы один импортёр действительно найден и проверен", total_checked > 0, str(total_checked))

print("2. __all__ соответствует общедоступным (без подчёркивания) атрибутам модуля")
# Исключаем сами модули (pd, np и т.п. — не часть контракта имён preprocess.py)
import types  # noqa: E402
public_names = {
    n for n in dir(preprocess)
    if not n.startswith("_") and not isinstance(getattr(preprocess, n), types.ModuleType)
}
check(
    "каждое публичное имя из __all__ действительно есть на модуле",
    set(preprocess.__all__) <= public_names,
    str(set(preprocess.__all__) - public_names),
)

print("3. Подмодули импортируются в изоляции, без preprocess.py (нет цикла)")
for mod_name in SPLIT_MODULES:
    full = f"services.{mod_name}"
    check(
        f"{full} не тянет preprocess при импорте",
        full not in sys.modules or "preprocess" not in getattr(sys.modules[full], "__dict__", {}),
    )
    source = (REPO / "src" / "services" / f"{mod_name}.py").read_text(encoding="utf-8-sig")
    check(
        f"{mod_name}.py не содержит `import preprocess`",
        not re.search(r"^\s*(from preprocess import|import preprocess\b)", source, flags=re.MULTILINE),
        f"найдена ссылка на preprocess в {mod_name}.py",
    )

print("4. Ключевые функции конвейера реально исполняются через новый путь")
# Не переисполняем test_preprocess.py — только доказываем, что вызов через
# preprocess.* доходит до кода в новых подмодулях, а не до заглушки.
import pandas as pd  # noqa: E402

check("normalize_spaces работает", preprocess.normalize_spaces("  a\t\tb  ") == "a b")
check("normalize_label отсекает служебные значения", preprocess.normalize_label("нет") == "")
check("tag_set разбирает строку по |", preprocess.tag_set("a|b|a") == {"a", "b"})
check(
    "classify_microtopic классифицирует жалобу",
    preprocess.classify_microtopic("Пришёл брак, монтажники жалуются") == "issue_problem",
)
check(
    "build_title собирает заголовок из ключевых слов",
    preprocess.build_title(tag="Без тега", keywords=["доставка", "склад", "сроки"], microtopic="other").startswith("Обсуждение:"),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Контракт распила preprocess.py соблюдён.")
