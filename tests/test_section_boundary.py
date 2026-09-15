"""Граница отказа разделов: падение одного блока не уносит страницу.

Проверяется ровно то, что однажды сломалось в проде: исключение в одном
разделе показывало заказчику трейсбек вместо платформы. И обратное свойство,
которое легко потерять при правке: st.rerun() и st.stop() внутри раздела
должны проходить сквозь границу насквозь. Если заменить except Exception на
except BaseException, перестанет работать каждая кнопка в приложении, а
внешне всё будет выглядеть исправным — этот тест ловит такую правку.
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

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(REPO / "tests" / "boundary_app.py")

failures = []


def check(label, condition, detail=""):
    print(
        ("  ✓ " if condition else "  ✗ ")
        + label
        + (f" — {detail}" if detail and not condition else "")
    )
    if not condition:
        failures.append(label)


def run(mode, **state):
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["boundary_mode"] = mode
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    return at


def texts(at):
    return [str(m.value) for m in at.markdown]


print("1. Исправный раздел проходит границу без следов")
at = run("ok")
check("исключения нет", not at.exception, str(at.exception))
check("содержимое раздела на месте", "раздел отрисован" in texts(at), str(texts(at)))
check("ошибок на странице нет", not at.error, str([e.value for e in at.error]))
check("граница вернула True", at.session_state["boundary_result"] is True)

print("2. Падение раздела не роняет страницу")
at = run("crash")
check("приложение не упало", not at.exception, str(at.exception))
check("граница вернула False", at.session_state["boundary_result"] is False)
errors = [str(e.value) for e in at.error]
check(
    "пользователю показано имя сломанного раздела",
    any("Инфоповоды" in text for text in errors),
    str(errors),
)
check(
    "сказано, что остальное работает",
    any("продолжают работать" in str(c.value) for c in at.caption),
    str([c.value for c in at.caption]),
)
check(
    "скрипт дошёл до конца — остальная страница отрисовалась",
    "скрипт дошёл до конца" in texts(at),
    str(texts(at)),
)
check(
    "часть раздела до падения осталась на экране",
    "строка до падения" in texts(at),
    str(texts(at)),
)

print("3. Технические подробности — только тем, кому их показали")
at = run("crash", boundary_details=False)
check(
    "без флага раскрывашки с трейсбеком нет",
    not any("Подробности ошибки" in str(e.label) for e in at.expander),
    str([e.label for e in at.expander]),
)
at = run("crash", boundary_details=True)
check(
    "с флагом раскрывашка появилась",
    any("Подробности ошибки" in str(e.label) for e in at.expander),
    str([e.label for e in at.expander]),
)

print("4. st.rerun() внутри раздела проходит сквозь границу")
at = run("rerun")
check("приложение не упало", not at.exception, str(at.exception))
passes = int(at.session_state["boundary_passes"])
check("перезапуск действительно случился (два прохода)", passes == 2, str(passes))
check(
    "граница не приняла перезапуск за ошибку",
    not at.error,
    str([e.value for e in at.error]),
)
check("второй проход отрисован", "проход 2" in texts(at), str(texts(at)))

# st.stop() в текущем Streamlit не бросает исключение сам: он просит рантайм
# остановиться, и StopException прилетает на ближайшем вызове st.* — в том
# числе внутри except. Поэтому слишком широкую границу ловят проверки выше, а
# эти закрепляют само поведение остановки.
print("5. st.stop() внутри раздела останавливает скрипт, а не гасится границей")
at = run("stop")
check("приложение не упало", not at.exception, str(at.exception))
check("строка до stop отрисована", "строка до stop" in texts(at), str(texts(at)))
check(
    "строка после stop не отрисована",
    "строка после stop" not in texts(at),
    str(texts(at)),
)
check(
    "скрипт остановился, до конца не дошёл",
    "скрипт дошёл до конца" not in texts(at),
    str(texts(at)),
)
check(
    "граница не приняла остановку за ошибку",
    not at.error,
    str([e.value for e in at.error]),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Границы отказа разделов работают.")
