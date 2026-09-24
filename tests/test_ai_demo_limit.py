"""Лимит ИИ-генерации в демо-проекте: сквозной прогон от кнопки до базы.

Демо-проект показывают многим, и единственное, что держит расходы на модель, —
счётчик `demo_ai_runs` в настройках проекта. Раньше его покрывала только
арифметика (`demo_ai_runs_left/used` в test_roles.py), а сама кнопка — нет:
тест ролей не настраивал провайдера и до кнопок генерации не доходил.

Здесь провайдер настроен (YandexGPT), а сеть подменена поддельным транспортом:
`ai_provider._requests` отдаёт объект, у которого `Session().post` возвращает
заранее заданные ответы. Supabase — поддельный клиент из fake_supabase.py,
поэтому счётчик проверяется там, куда его пишет приложение, — в строке проекта.

Мутационные проверки (каждая обязана уронить тест):
- списывать запуск только при успехе генерации → падают блоки 1, 4 и 5;
- убрать `disabled=demo_exhausted` у кнопок генерации → падают блоки 2 и 3;
- вернуть списание после `_run_generation` (как было) → падает блок 4:
  прерванный прогон уносит оплаченный запрос без списания;
- убрать `st.rerun()` после списания → падают блоки 1 и 2: остаток на экране
  отстаёт на один запуск, кнопки после последнего остаются включёнными;
- списывать по настройкам из кеша, а не из базы → падает блок 5: посетитель
  со страницей двухминутной давности проходит исчерпанный лимит и откатывает
  счётчик назад;
- показывать `detail` всем, а не только владельцу → падает блок 1;
- вернуть сырой ответ провайдера в текст ошибки → падают блоки 1 и 6.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "scripts", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Провайдер настраивается окружением до импорта приложения. Чужие AI_* из
# окружения разработчика убираем, чтобы тест не зависел от машины.
for _key in list(os.environ):
    if _key.startswith(("AI_", "YANDEX_", "GIGACHAT_")):
        os.environ.pop(_key)
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-key"
os.environ["PLATFORM_ADMIN_PASSWORD"] = "test-admin"
os.environ["AI_PROVIDER"] = "yandex"
os.environ["YANDEX_API_KEY"] = "test-key"
os.environ["YANDEX_FOLDER_ID"] = "test-folder"

from fake_supabase import FakeClient  # noqa: E402

import platform_store as store  # noqa: E402

CLIENT = FakeClient()
store.get_supabase_client = lambda: CLIENT

PROJECT_ID = "demo_project"
PERIOD_ID = "p_2026_04"
now = datetime.now(timezone.utc).isoformat()

CLIENT.db["platform_projects"] = [
    {
        "project_id": PROJECT_ID,
        "project_name": "Ромашка",
        "status": "active",
        "viewer_code_hash": store.hash_code("viewer123"),
        "editor_code_hash": store.hash_code("editor123"),
        "settings": {"demo_mode": True},
        "created_at": now,
        "updated_at": now,
    }
]
CLIENT.db["platform_periods"] = [
    {
        "project_id": PROJECT_ID,
        "period_id": PERIOD_ID,
        "period_name": "24.04.2026–30.04.2026",
        "date_from": "2026-04-24",
        "date_to": "2026-04-30",
        "source_filename": "april.xlsx",
        "status": "active",
        "manifest": {},
        "uploaded_at": now,
    }
]
START = datetime(2026, 4, 24, 10, 0, 0)
THEMES = [
    ("позитив", "Запуск новой линейки"),
    ("негатив", "Жалобы на сроки доставки"),
    ("нейтрал", "Обзор рынка за квартал"),
]
rows = []
for i in range(12):
    sentiment, theme = THEMES[i % len(THEMES)]
    day = START + timedelta(days=i % 5, hours=i % 6)
    rows.append(
        {
            "project_id": PROJECT_ID,
            "period_id": PERIOD_ID,
            "table_name": "messages",
            "row_id": f"m{i}",
            "payload": {
                "message_id": f"m{i}",
                "period_id": PERIOD_ID,
                "date": day.strftime("%d.%m.%Y"),
                "datetime": day.isoformat(),
                "sentiment": sentiment,
                "views": 9000 + i * 100,
                "audience": 4000,
                "engagement": 70 + i,
                "text_clean": f"Сообщение про {theme.lower()}",
                "platform": "vk.com",
                "chat_title": f"Канал {i % 4}",
                "author": f"id{100 + i}",
                "tags": "Ромашка|Темы",
                "event_title": theme,
            },
        }
    )
CLIENT.db["platform_table_rows"] = rows


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


INTERRUPT = "interrupt"


class FakeProvider:
    """Вместо модуля requests: `Session().post` отдаёт ответы из очереди.

    Особый ответ INTERRUPT записывает запрос как ушедший и прерывает прогон
    через st.rerun() — так же Streamlit прерывает скрипт, когда человек во
    время генерации нажимает что-то ещё или закрывает вкладку.
    """

    def __init__(self):
        self.queue = []
        self.calls = 0

    def Session(self):  # noqa: N802 — повторяем имя из requests
        return self

    def post(self, url, **kwargs):
        self.calls += 1
        if not self.queue:
            raise AssertionError(f"Лишний запрос к модели: {url}")
        answer = self.queue.pop(0)
        if answer == INTERRUPT:
            import streamlit as st  # noqa: PLC0415

            st.rerun()
        return answer


from services import ai_provider  # noqa: E402

PROVIDER = FakeProvider()
ai_provider._requests = lambda: PROVIDER

RAW_LIMIT_TEXT = "Number of input tokens must be no more than 8192, got 9136"
CONTEXT_ERROR = FakeResponse(
    400,
    {"error": {"grpcCode": 3, "httpCode": 400, "message": RAW_LIMIT_TEXT, "httpStatus": "Bad Request"}},
)
MODEL_OK = FakeResponse(
    payload={"result": {"alternatives": [{"message": {"text": "Готовый текст от модели."}}]}}
)

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from services.project_settings import DEMO_AI_LIMIT  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(
        ("  ✓ " if condition else "  ✗ ")
        + label
        + (f" — {detail}" if detail and not condition else "")
    )
    if not condition:
        failures.append(label)


def runs_in_db():
    """Счётчик ровно там, куда его пишет приложение, — в строке проекта."""
    return (CLIENT.db["platform_projects"][0].get("settings") or {}).get("demo_ai_runs")


def set_runs(used):
    """Начальное состояние счётчика.

    Кеш сбрасывается целиком: clear_platform_caches работает бампом версии в
    session_state, а у каждого нового AppTest она начинается заново.
    """
    CLIENT.db["platform_projects"][0]["settings"] = {"demo_mode": True, "demo_ai_runs": used}
    st.cache_data.clear()


def change_db_behind_cache(**settings):
    """Другой посетитель уже что-то записал, а у этой сессии кеш прежний.

    Кеш списка проектов общий для всех сессий и живёт до двух минут, а версия,
    которой его сбрасывают, у каждой сессии своя, — поэтому здесь кеш
    намеренно НЕ чистится.
    """
    CLIENT.db["platform_projects"][0]["settings"] = {"demo_mode": True, **settings}


def open_report(role="editor"):
    at = AppTest.from_file(str(REPO / "streamlit_app.py"), default_timeout=90)
    if role == "owner":
        at.session_state["platform_is_admin"] = True
        at.session_state["platform_project_id"] = PROJECT_ID
    else:
        at.session_state["platform_project_id"] = PROJECT_ID
        at.session_state["platform_project_role"] = role
    at.session_state["platform_nav_page"] = "Отчёт"
    at.run()
    return at


def generate_buttons(at):
    return {
        str(b.key): b for b in at.button if str(b.key or "").startswith("ai_generate_")
    }


SUMMARY_KEY = f"ai_generate_summary_{PROJECT_ID}"
BRAND_KEY = f"ai_generate_brand_{PROJECT_ID}"


def texts(elements):
    return [str(e.value) for e in elements]


def shows_left(at, left):
    """Остаток виден одинаково и в панели, и в плашке демо в шапке."""
    infos = texts(at.info)
    panel = any(f"{left} запусков ИИ-генерации из {DEMO_AI_LIMIT}" in t for t in infos)
    banner = any(f"осталось {left} запусков" in t for t in infos)
    return panel and banner


print("1. Ошибка модели: запуск всё равно списан, текст ошибки понятный")
set_runs(0)
PROVIDER.calls = 0
at = open_report()
check("раздел открылся", not at.exception, str(at.exception))
buttons = generate_buttons(at)
check(
    "кнопки генерации на месте и включены",
    len(buttons) == 3 and not any(b.disabled for b in buttons.values()),
    str([(k, b.disabled) for k, b in buttons.items()]),
)
check("до клика показан весь лимит", shows_left(at, DEMO_AI_LIMIT), str(texts(at.info)))
PROVIDER.queue = [CONTEXT_ERROR]
buttons[SUMMARY_KEY].click().run()
check("после клика без исключений", not at.exception, str(at.exception))
check("запрос к модели ушёл один", PROVIDER.calls == 1, str(PROVIDER.calls))
check("счётчик в настройках проекта вырос на 1", runs_in_db() == 1, str(runs_in_db()))
errors = " ".join(texts(at.error))
check("человеку сказано, что запрос не поместился", "не поместился" in errors, errors)
check("и что сделать — убрать выдержки", "выдержки" in errors, errors)
page = " ".join(texts(at.error) + texts(at.caption) + texts(at.markdown))
check(
    "сырого ответа провайдера на экране редактора нет",
    RAW_LIMIT_TEXT not in page and "grpcCode" not in page,
    page[:300],
)
check(
    "остаток на экране обновлён сразу после клика",
    shows_left(at, DEMO_AI_LIMIT - 1),
    str(texts(at.info)),
)
at.run()
check("простая перерисовка запуск не списывает", runs_in_db() == 1, str(runs_in_db()))
check("перерисовка к модели не ходит", PROVIDER.calls == 1, str(PROVIDER.calls))
check(
    "сообщение об ошибке показано один раз, а не висит вечно",
    not texts(at.error),
    str(texts(at.error)),
)

print("2. Последний запуск: текст получен, кнопки выключаются сразу")
set_runs(DEMO_AI_LIMIT - 1)
PROVIDER.calls = 0
at = open_report()
PROVIDER.queue = [MODEL_OK]
generate_buttons(at)[BRAND_KEY].click().run()
check("последний запуск прошёл", not at.exception, str(at.exception))
check(
    "об успехе сказано",
    any("готово" in t for t in texts(at.success)),
    str(texts(at.success)),
)
check(
    "черновик текста на экране",
    any("Готовый текст от модели." in str(a.value) for a in at.text_area),
    str([str(a.value)[:40] for a in at.text_area]),
)
check("успешный запуск тоже списан", runs_in_db() == DEMO_AI_LIMIT, str(runs_in_db()))
check(
    "сразу сказано, что запуски закончились",
    any("закончились" in t for t in texts(at.warning)),
    str(texts(at.warning)),
)
buttons = generate_buttons(at)
check(
    "кнопки генерации выключены без лишнего клика",
    len(buttons) == 3 and all(b.disabled for b in buttons.values()),
    str([(k, b.disabled) for k, b in buttons.items()]),
)

print("3. Лимит исчерпан: новый посетитель видит выключенные кнопки")
set_runs(DEMO_AI_LIMIT)
PROVIDER.calls = 0
at = open_report()
buttons = generate_buttons(at)
check(
    "все три кнопки генерации выключены",
    len(buttons) == 3 and all(b.disabled for b in buttons.values()),
    str([(k, b.disabled) for k, b in buttons.items()]),
)
check(
    "предупреждение на месте",
    any("закончились" in t for t in texts(at.warning)),
    str(texts(at.warning)),
)
check("к модели не ходили", PROVIDER.calls == 0, str(PROVIDER.calls))
check("счётчик не ушёл за лимит", runs_in_db() == DEMO_AI_LIMIT, str(runs_in_db()))

print("4. Прогон прервали во время генерации — запуск всё равно списан")
# Пока модель пишет, человек может нажать что-то ещё или закрыть вкладку.
# Streamlit тогда прерывает прогон на ближайшем st-вызове, и всё, что стояло
# после запроса к модели, не выполняется. Оплаченный запрос уже ушёл — значит,
# и запуск должен быть списан, иначе демо крутится бесконечно.
set_runs(0)
PROVIDER.calls = 0
at = open_report()
PROVIDER.queue = [INTERRUPT]
generate_buttons(at)[SUMMARY_KEY].click().run()
check("прерывание не уронило раздел", not at.exception, str(at.exception))
check("запрос к модели ушёл", PROVIDER.calls == 1, str(PROVIDER.calls))
check("запуск списан, хотя прогон прерван", runs_in_db() == 1, str(runs_in_db()))

print("5. Страница другого посетителя устарела — лимит всё равно держится")
set_runs(DEMO_AI_LIMIT - 1)
PROVIDER.calls = 0
at = open_report()
change_db_behind_cache(demo_ai_runs=DEMO_AI_LIMIT)
check(
    "на устаревшей странице кнопки ещё включены — ровно этот случай и проверяем",
    not generate_buttons(at)[SUMMARY_KEY].disabled,
    "",
)
PROVIDER.queue = [MODEL_OK]
generate_buttons(at)[SUMMARY_KEY].click().run()
check("клик по устаревшей странице не уронил раздел", not at.exception, str(at.exception))
check("к модели сверх лимита не ходили", PROVIDER.calls == 0, str(PROVIDER.calls))
check("счётчик не ушёл за лимит", runs_in_db() == DEMO_AI_LIMIT, str(runs_in_db()))
check(
    "после клика страница показала, что запуски закончились",
    any("закончились" in t for t in texts(at.warning))
    and all(b.disabled for b in generate_buttons(at).values()),
    str(texts(at.warning)),
)
PROVIDER.queue = []

set_runs(2)
PROVIDER.calls = 0
at = open_report()
change_db_behind_cache(demo_ai_runs=5, ai_access="editor")
PROVIDER.queue = [MODEL_OK]
generate_buttons(at)[SUMMARY_KEY].click().run()
check("запуск со старой страницы прошёл", PROVIDER.calls == 1, str(PROVIDER.calls))
check(
    "счётчик считается от базы, а не от кеша: 5 + 1, а не 2 + 1",
    runs_in_db() == 6,
    str(runs_in_db()),
)
check(
    "чужие свежие настройки проекта не затёрты старым снимком",
    (CLIENT.db["platform_projects"][0].get("settings") or {}).get("ai_access") == "editor",
    str(CLIENT.db["platform_projects"][0].get("settings")),
)

print("6. Владелец платформы: демо-лимит не тратит, ответ сервиса видит")
set_runs(3)
PROVIDER.calls = 0
at = open_report("owner")
check("владелец открыл раздел", not at.exception, str(at.exception))
PROVIDER.queue = [CONTEXT_ERROR]
generate_buttons(at)[SUMMARY_KEY].click().run()
check("у владельца счётчик не тронут", runs_in_db() == 3, str(runs_in_db()))
check(
    "владелец видит исходный ответ сервиса отдельной строкой",
    any(RAW_LIMIT_TEXT in t for t in texts(at.caption)),
    str(texts(at.caption))[:300],
)
check(
    "а в самом сообщении об ошибке JSON нет и у владельца",
    bool(texts(at.error)) and not any("grpcCode" in t for t in texts(at.error)),
    str(texts(at.error)),
)

print("7. Перерисовка после списания не стирает то, что гость ввёл ниже кнопок")
# st.rerun() посреди страницы стирал состояние полей, которые не успели
# появиться в прогоне: правку черновика ИИ и выбор разделов выгрузки. Теперь
# перерисовка откладывается до конца страницы.
set_runs(0)
PROVIDER.calls = 0
at = open_report()
PROVIDER.queue = [MODEL_OK]
generate_buttons(at)[SUMMARY_KEY].click().run()
drafts = [a for a in at.text_area if str(a.value) == "Готовый текст от модели."]
check("черновик саммари появился", bool(drafts), str([str(a.value)[:40] for a in at.text_area]))
sections = [m for m in at.multiselect if str(m.label) == "Разделы отчёта"]
check("есть выбор разделов выгрузки", bool(sections))
if drafts and sections:
    draft_key = drafts[0].key
    drafts[0].set_value("Правка гостя").run()
    sections[0].set_value(["summary_text"]).run()
    PROVIDER.queue = [MODEL_OK]
    generate_buttons(at)[BRAND_KEY].click().run()
    check("вторая генерация без исключений", not at.exception, str(at.exception))
    check("запуск списан", runs_in_db() == 2, str(runs_in_db()))
    kept = [a for a in at.text_area if a.key == draft_key]
    check("правка черновика после перерисовки на месте",
          bool(kept) and str(kept[0].value) == "Правка гостя",
          str([str(a.value)[:40] for a in kept]))
    kept_sections = [m for m in at.multiselect if str(m.label) == "Разделы отчёта"]
    check("выбор разделов выгрузки после перерисовки на месте",
          bool(kept_sections) and list(kept_sections[0].value) == ["summary_text"],
          str([list(m.value) for m in kept_sections]))
    check("остаток обновлён", shows_left(at, DEMO_AI_LIMIT - 2), str(texts(at.info)))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Демо-лимит ИИ списывается по клику, до запроса к модели, и выключает кнопки.")