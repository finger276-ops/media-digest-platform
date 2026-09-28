# -*- coding: utf-8 -*-
"""Контракт разделения крупных модулей.

Разделены четыре модуля:
- services/report_export.py (1688 строк): общее ядро и форматы — в
  report_common, report_png, report_docx, report_pdf;
- overview_ui.py (1260): графики — в overview_charts;
- events_ui.py (1390): ручная модерация — в events_manual_ui, карточка
  инфоповода — в events_detail_ui;
- app.py (1195): данные дашборда — в dashboard_loader, граница отказа — в
  section_boundary_ui, разделы-фрагменты — в dashboard_sections_ui, шапка и
  панель «Вид» — в dashboard_header_ui, страницы без данных периодов — в
  service_pages_ui.

Исходные модули реэкспортируют прежние имена, так что внешний код их не
меняет. Этот тест проверяет само разделение, а не логику (её стерегут
остальные тесты):
- ре-экспорт не теряет имя, которое кто-то импортирует;
- новые модули не зависят от исходного, иначе появился бы цикл импорта;
- функция перенесена, а не скопирована: копия расходилась бы с оригиналом,
  и подмена в тесте не доходила бы до вызова.

Имена ищутся по всему репозиторию: `from X import …`, `import X as y`
плюс обращения `y.name`, относительные импорты внутри services.

Мутационные проверки (что ломает какой тест):
- убрать имя из блока ре-экспорта в report_export.py (например
  generate_summary_pdf) -> «services.report_export: всё, что просят
  импортёры, на месте» краснеет;
- добавить `from overview_ui import TONE_CARDS` в overview_charts.py ->
  «overview_charts импортируется без overview_ui» краснеет;
- вернуть копию _render_section_failure в app.py -> «app: определения не
  дублируются» краснеет;
- ре-экспорт обёрткой вместо имени (`def render_section_safely(*a, **k):
  return _boundary.render_section_safely(*a, **k)` в app.py) -> «app.
  render_section_safely — тот же объект, что в новом модуле» краснеет.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
for _p in (SRC, REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# Исходный модуль → (его другие имена в импортах, новые модули).
SPLITS = {
    "services.report_export": (
        {"services.report_export", "src.services.report_export"},
        ["services.report_common", "services.report_png", "services.report_docx", "services.report_pdf"],
    ),
    "overview_ui": ({"overview_ui", "src.overview_ui"}, ["overview_charts"]),
    "events_ui": ({"events_ui", "src.events_ui"}, ["events_manual_ui", "events_detail_ui"]),
    "app": (
        {"app", "src.app"},
        ["dashboard_loader", "section_boundary_ui", "dashboard_sections_ui", "dashboard_header_ui",
         "service_pages_ui"],
    ),
}


def module_path(name: str) -> Path:
    return SRC / Path(*name.split(".")).with_suffix(".py")


def python_files():
    for folder in (SRC, REPO / "tests", REPO / "scripts"):
        yield from sorted(folder.rglob("*.py"))
    yield REPO / "streamlit_app.py"


def absolute_module(path: Path, node: ast.ImportFrom) -> str:
    """Полное имя модуля в `from … import`, с учётом относительных импортов."""
    if not node.level:
        return node.module or ""
    package = path.relative_to(SRC).parent.parts if path.is_relative_to(SRC) else ()
    base = list(package[: len(package) - node.level + 1])
    return ".".join(base + ([node.module] if node.module else []))


def requested_names(module: str, aliases: set[str]) -> dict[str, set[str]]:
    """Какие имена модуля просит каждый файл репозитория (кроме самого модуля)."""
    own = module_path(module)
    found: dict[str, set[str]] = {}
    for path in python_files():
        if path == own:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        bound = set()
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                full = absolute_module(path, node)
                if full in aliases:
                    names.update(a.name for a in node.names if a.name != "*")
                for a in node.names:
                    # from services import report_export [as x]
                    if f"{full}.{a.name}" in aliases:
                        bound.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in aliases and a.asname:
                        bound.add(a.asname)
        if bound:
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound:
                    names.add(node.attr)
        if names:
            found[str(path.relative_to(REPO))] = names
    return found


def top_level_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    # Имя логгера у каждого модуля своё по смыслу, но общее по каналу.
    names.discard("LOGGER")
    return names


# Импорт нового модуля в отдельном процессе, где исходный модуль запрещён:
# получится — значит, зависимости ни прямой, ни через третий модуль нет.
BLOCKED_IMPORT = r"""
import importlib, importlib.abc, sys
blocked = set(sys.argv[2].split(","))
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in blocked:
            raise ImportError(f"запрещённый импорт {name}")
        return None
sys.meta_path.insert(0, Block())
sys.path[:0] = [sys.argv[3]]
importlib.import_module(sys.argv[1])
"""

# Первым: цикл импорта уронил бы проверки ниже раньше, чем эта его назовёт.
print("1. Новые модули не зависят от исходного")
for module, (aliases, parts) in SPLITS.items():
    for part in parts:
        result = subprocess.run(
            [sys.executable, "-c", BLOCKED_IMPORT, part, ",".join(sorted(aliases)), str(SRC)],
            capture_output=True, text=True, env={**os.environ}, timeout=180,
        )
        tail = (result.stderr.strip().splitlines() or [""])[-1]
        check(f"{part} импортируется без {module}", result.returncode == 0, tail)

print("2. Всё, что просят импортёры, на месте")
import importlib  # noqa: E402


def load(name):
    """Модуль или None: сломанный импорт — провал проверки, а не всего теста."""
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        check(f"{name} импортируется", False, f"{type(exc).__name__}: {exc}")
        return None


total = 0
for module, (aliases, _) in SPLITS.items():
    loaded = load(module)
    requests = requested_names(module, aliases)
    missing = sorted(f"{where}: {name}" for where, names in requests.items() for name in names
                     if not hasattr(loaded, name))
    missing = missing if loaded is not None else ["модуль не импортируется"]
    total += sum(len(names) for names in requests.values())
    check(f"{module}: всё, что просят импортёры, на месте", bool(requests) and not missing,
          "; ".join(missing) or "импортёров не найдено")
# Импортёры действительно находятся: и `from … import`, и обращения через псевдоним.
report_requests = requested_names("services.report_export", SPLITS["services.report_export"][0])
check("найдены импортёры через псевдоним модуля",
      "generate_summary_docx" in report_requests.get("tests/test_report_sources.py", set()),
      str(report_requests.get("tests/test_report_sources.py")))
check("найдены импортёры app из тестов",
      {"_as_fragment", "render_section_safely"} <= requested_names("app", SPLITS["app"][0]).get(
          "tests/boundary_app.py", set()))
# Сейчас их 58: заметно меньше — значит, сканер перестал видеть часть импортов.
check("проверено не меньше 50 запросов имён", total >= 50, str(total))

print("3. Функции перенесены, а не скопированы")
for module, (_, parts) in SPLITS.items():
    own = top_level_definitions(module_path(module))
    seen: dict[str, str] = {name: module for name in own}
    duplicates = []
    for part in parts:
        for name in top_level_definitions(module_path(part)):
            if name in seen:
                duplicates.append(f"{name} ({seen[name]} и {part})")
            seen[name] = part
    check(f"{module}: определения не дублируются", not duplicates, "; ".join(sorted(duplicates)))

REEXPORTED = [
    ("services.report_export", "generate_summary_pdf", "services.report_pdf"),
    ("services.report_export", "generate_summary_docx", "services.report_docx"),
    ("services.report_export", "generate_summary_infographic_png", "services.report_png"),
    ("services.report_export", "resolve_report_sections", "services.report_common"),
    ("overview_ui", "render_period_comparison_charts", "overview_charts"),
    ("events_ui", "render_title_merge_report", "events_manual_ui"),
    ("events_ui", "render_selected_event_detail", "events_detail_ui"),
    ("app", "render_section_safely", "section_boundary_ui"),
    ("app", "period_overview_metrics", "dashboard_loader"),
]
for module, name, part in REEXPORTED:
    old, new = load(module), load(part)
    check(f"{module}.{name} — тот же объект, что в новом модуле",
          old is not None and new is not None and getattr(old, name, None) is getattr(new, name))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Разделение модулей: ре-экспорт полный, циклов нет, копий нет.")
