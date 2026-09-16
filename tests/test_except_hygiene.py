"""Гигиена except: молчание должно быть осознанным.

Аудит нашёл десятки мест, где широкий `except Exception` молча глотал ошибку
(pass/continue/return без лога). Часть из них прятала настоящие потери: отчёт
без инфографики, период «скрыт», хотя скрыть не удалось, выпавший лист Excel.

После ревизии правило такое:
- узкий типизированный except (ValueError, TypeError, ...) самодостаточен —
  тип называет ожидаемую ошибку;
- широкий `except Exception` с молчаливым телом обязан нести комментарий,
  объясняющий, почему молчать здесь правильно, — на строке except, между ней
  и телом или на строке самого pass/return;
- обработчик, который логирует или отправляет report_failure, молчаливым не
  считается.

Этот тест — линтер правила: новый бескомментарный глотатель ошибок не пройдёт
CI. Мутационная проверка встроена: тест сам вносит нарушение во временный
текст и убеждается, что детектор его находит.
"""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

failures = []


def check(label, condition, detail=""):
    print(
        ("  ✓ " if condition else "  ✗ ")
        + label
        + (f" — {detail}" if detail and not condition else "")
    )
    if not condition:
        failures.append(label)


BROAD_NAMES = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    node = handler.type
    if node is None:  # голый except:
        return True
    if isinstance(node, ast.Name):
        return node.id in BROAD_NAMES
    if isinstance(node, ast.Tuple):
        return any(
            isinstance(el, ast.Name) and el.id in BROAD_NAMES for el in node.elts
        )
    return False


def find_violations(text: str, filename: str) -> list[str]:
    lines = text.splitlines()
    violations = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
            continue
        if len(node.body) != 1:
            continue
        body = node.body[0]
        silent = isinstance(body, (ast.Pass, ast.Continue, ast.Return))
        if not silent:
            continue
        # Комментарий ищется от строки except до строки тела включительно.
        region = "\n".join(lines[node.lineno - 1 : body.lineno])
        if "#" not in region:
            violations.append(f"{filename}:{node.lineno}")
    return violations


print("1. Детектор ловит бескомментарного глотателя (мутационная проверка)")
bad = "try:\n    x = 1\nexcept Exception:\n    pass\n"
check("нарушение найдено", find_violations(bad, "образец.py") == ["образец.py:3"])
good = "try:\n    x = 1\nexcept Exception:  # noqa: BLE001 — обосновано\n    pass\n"
check("комментарий на строке except засчитан", not find_violations(good, "x.py"))
good2 = "try:\n    x = 1\nexcept Exception:\n    pass  # молчание объяснено\n"
check("комментарий на строке тела засчитан", not find_violations(good2, "x.py"))
typed = "try:\n    x = 1\nexcept ValueError:\n    pass\n"
check("типизированный except не трогаем", not find_violations(typed, "x.py"))
loud = "try:\n    x = 1\nexcept Exception:\n    log()\n    raise\n"
check("логирующий обработчик не молчалив", not find_violations(loud, "x.py"))

print("2. Кодовая база чиста")
all_violations: list[str] = []
scanned = 0
for folder in ("src", "scripts"):
    for path in sorted((REPO / folder).rglob("*.py")):
        scanned += 1
        # utf-8-sig: часть файлов сохранена с BOM, ast.parse его не прощает.
        text = path.read_text(encoding="utf-8-sig")
        all_violations.extend(
            find_violations(text, str(path.relative_to(REPO)))
        )
check(
    f"молчаливых except без обоснования нет (файлов проверено: {scanned})",
    not all_violations,
    "; ".join(all_violations[:10]),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Гигиена except соблюдена.")
