"""Доставка ошибок владельцу: каналы, глушение повторов, живучесть.

Модуль services/observability.py обязан выполнять два противоположных
обещания: доносить сбой до владельца, когда канал настроен, и никогда не
ронять страницу — какой бы ни была конфигурация и что бы ни случилось с
доставкой.

Мутационные проверки:
- убрать вызов канала → падает «вебхук получил событие»;
- убрать глушение (слать всегда) → падает «повтор не отправлен»;
- глушить всё подряд → падает «другое место проходит»;
- пробросить исключение доставки наружу → падает «сбой доставки проглочен»;
- отвязать report_failure от границы отказа → падает блок 5 (AppTest).
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"

# Пакет sentry-sdk намеренно «не установлен»: None в sys.modules заставляет
# import упасть — детерминированно, независимо от окружения запуска.
sys.modules["sentry_sdk"] = None  # type: ignore[assignment]

from services import observability as obs  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(
        ("  ✓ " if condition else "  ✗ ")
        + label
        + (f" — {detail}" if detail and not condition else "")
    )
    if not condition:
        failures.append(label)


SECRETS: dict[str, str] = {}
POSTED: list[tuple[str, dict]] = []

obs._secret = lambda name: SECRETS.get(name, "")


def recorder(url, payload):
    POSTED.append((url, payload))


print("1. Без конфигурации: тихий noop, исключений нет")
obs.reset_throttle()
obs._post = recorder
sent = obs.report_failure("раздел «Тест»", RuntimeError("сломалось"))
check("ничего не отправлено", sent is False and not POSTED)

print("2. Вебхук настроен: событие уходит с контекстом")
obs.reset_throttle()
SECRETS["ALERT_WEBHOOK_URL"] = "https://hooks.example/alert"
sent = obs.report_failure(
    "раздел «Инфоповоды»", ValueError("нет колонки"), project_id="p1"
)
check("отправлено", sent is True and len(POSTED) == 1, f"постов: {len(POSTED)}")
url, payload = POSTED[-1]
check("адрес из настроек", url == "https://hooks.example/alert")
check("указано место", payload.get("where") == "раздел «Инфоповоды»")
check(
    "ошибка с классом и текстом",
    payload.get("error") == "ValueError: нет колонки",
    str(payload.get("error")),
)
check("контекст приведён к строкам", payload.get("context") == {"project_id": "p1"})
check("есть хвост трейсбека", "ValueError" in str(payload.get("traceback", "")))

print("3. Глушение повторов")
sent = obs.report_failure("раздел «Инфоповоды»", ValueError("нет колонки"))
check("повтор не отправлен", sent is False and len(POSTED) == 1)
sent = obs.report_failure("раздел «Сообщения»", ValueError("нет колонки"))
check("другое место проходит", sent is True and len(POSTED) == 2)
sent = obs.report_failure("раздел «Инфоповоды»", KeyError("другой класс"))
check("другой класс ошибки проходит", sent is True and len(POSTED) == 3)
obs.reset_throttle()
sent = obs.report_failure("раздел «Инфоповоды»", ValueError("нет колонки"))
check("после сброса снова уходит", sent is True and len(POSTED) == 4)

print("4. Живучесть: сбой доставки и отсутствие sentry-sdk")


def broken(url, payload):
    raise ConnectionError("приёмник лежит")


obs.reset_throttle()
obs._post = broken
sent = obs.report_failure("раздел «Тест»", RuntimeError("сломалось"))
check("сбой доставки проглочен", sent is False)
obs._post = recorder

obs.reset_throttle()
SECRETS["SENTRY_DSN"] = "https://key@sentry.example/1"
sent = obs.report_failure("раздел «Тест»", RuntimeError("сломалось"))
check(
    "без sentry-sdk канал пропущен, вебхук работает",
    sent is True and POSTED[-1][1]["where"] == "раздел «Тест»",
)
del SECRETS["SENTRY_DSN"]

obs.reset_throttle()
sent = obs.report_failure("проверка без исключения")
check(
    "событие без исключения уходит",
    sent is True and POSTED[-1][1]["error"] == "",
)

print("5. Граница отказа зовёт доставку (настоящий рантайм Streamlit)")
from streamlit.testing.v1 import AppTest  # noqa: E402

obs.reset_throttle()
POSTED.clear()
at = AppTest.from_file(str(REPO / "tests" / "boundary_app.py"), default_timeout=90)
at.session_state["boundary_mode"] = "crash"
at.run()
check("приложение не упало", not at.exception, str(at.exception))
check(
    "падение раздела дошло до вебхука",
    len(POSTED) == 1 and "Инфоповоды" in POSTED[-1][1]["where"],
    str(POSTED),
)
check(
    "в событии — настоящая ошибка раздела",
    "раздел сломался" in str(POSTED[-1][1].get("error", "")) if POSTED else False,
)
at = AppTest.from_file(str(REPO / "tests" / "boundary_app.py"), default_timeout=90)
at.session_state["boundary_mode"] = "crash"
at.run()
check("повторное падение заглушено", len(POSTED) == 1, f"постов: {len(POSTED)}")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("Все проверки доставки ошибок пройдены.")
