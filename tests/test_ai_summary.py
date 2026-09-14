"""Проверка генерации саммари: транспорт, карточка данных, промпты.

Сеть не используется: вместо HTTP-сессии подставляется поддельный транспорт,
который записывает запросы и отдаёт заранее заданные ответы. Так проверяются
и формат запроса к каждому провайдеру, и разбор ответа, и поведение при ошибках.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Секреты задаём до импорта модулей, чтобы load_ai_config их увидел.
for _key in list(os.environ):
    if _key.startswith(("AI_", "YANDEX_", "GIGACHAT_")):
        os.environ.pop(_key)

import pandas as pd  # noqa: E402

from services import ai_provider  # noqa: E402
from services.ai_provider import (  # noqa: E402
    AIConfig,
    AIError,
    complete,
    load_ai_config,
    reset_gigachat_token,
)
from services.ai_summary import (  # noqa: E402
    ACCESS_EDITOR,
    ACCESS_OWNER,
    KIND_BRAND,
    KIND_RISKS,
    KIND_SUMMARY,
    ai_access_level,
    ai_text_storage_key,
    build_data_card,
    build_prompt,
    can_generate_ai,
    generate_text,
)

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeSession:
    """Транспорт-заглушка: пишет запросы и отдаёт ответы из очереди."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Лишний запрос к {url}")
        return self.responses.pop(0)


YANDEX_OK = FakeResponse(
    payload={
        "result": {
            "alternatives": [
                {"message": {"role": "assistant", "text": "Текст саммари от модели."}}
            ]
        }
    }
)


def gigachat_token_response(expires_ms=None):
    import time

    return FakeResponse(
        payload={
            "access_token": "token-123",
            "expires_at": expires_ms or int((time.time() + 1800) * 1000),
        }
    )


GIGACHAT_OK = FakeResponse(
    payload={"choices": [{"message": {"content": "Текст от GigaChat."}}]}
)


YANDEX_CONFIG = AIConfig(
    provider="yandex",
    api_key="key-1",
    folder_id="folder-1",
    model="yandexgpt/latest",
)
GIGACHAT_CONFIG = AIConfig(
    provider="gigachat", api_key="basic-key", model="GigaChat"
)


def sample_messages():
    return pd.DataFrame(
        {
            "message_id": [f"m{i}" for i in range(6)],
            "period_id": ["p1"] * 6,
            "sentiment": [
                "позитив",
                "негатив",
                "нейтрал",
                "негатив",
                "позитив",
                "нейтрал",
            ],
            "views": [1000, 5000, 200, 8000, 300, 100],
            "audience": [500, 900, 100, 1200, 200, 50],
            "engagement": [10, 300, 5, 450, 7, 1],
            "tags": ["Кровля", "Кровля|Жалобы", "Теплоизоляция", "Жалобы", "PR", "PR"],
            "chat_title": ["РБК", "Телеграм-канал", "Ведомости", "Пикабу", "VC", "РБК"],
            "text_clean": [
                "Компания открыла завод в Рязани",
                "Жители жалуются на запах с производства",
                "Обзор рынка теплоизоляции",
                "Подрядчик подал иск на 30 млн рублей",
                "Компания вошла в рейтинг работодателей",
                "Короткая заметка",
            ],
            "event_title": [
                "Открытие завода",
                "Жалобы жителей",
                "Обзор рынка",
                "Иск подрядчика",
                "Рейтинг работодателей",
                "Обзор рынка",
            ],
        }
    )


def sample_events():
    return pd.DataFrame(
        {
            "title": ["Жалобы жителей на запах с завода", "Открытие завода в Рязани"],
            "description": ["Описание 1", "Описание 2"],
            "tags": ["Жалобы", "Кровля"],
            "message_count": [12, 30],
            "chat_count": [5, 9],
            "negative_count": [11, 0],
            "negative_share": [0.92, 0.0],
            "importance_score": [18.0, 12.0],
            "merged_titles": [2, 0],
            "start_date": pd.to_datetime(["2026-04-24", "2026-04-25"]),
            "end_date": pd.to_datetime(["2026-04-28", "2026-04-26"]),
            "event_ids": [["e1", "e2", "e3"], ["e4"]],
        }
    )


def sample_periods():
    return pd.DataFrame(
        {"period_id": ["p1"], "period_name": ["24.04.2026–30.04.2026"]}
    )


print("1. YandexGPT: формат запроса и разбор ответа")
session = FakeSession([YANDEX_OK])
text = complete("система", "запрос", YANDEX_CONFIG, session=session)
call = session.calls[0]
check("текст извлечён из ответа", text == "Текст саммари от модели.", text)
check("адрес запроса верный", call["url"] == ai_provider.YANDEX_URL, call["url"])
check(
    "ключ уходит заголовком Api-Key",
    call["headers"]["Authorization"] == "Api-Key key-1",
    str(call["headers"]),
)
check(
    "modelUri собран из каталога",
    call["json"]["modelUri"] == "gpt://folder-1/yandexgpt/latest",
    call["json"]["modelUri"],
)
check(
    "системный и пользовательский промпт разделены",
    [m["role"] for m in call["json"]["messages"]] == ["system", "user"],
    str(call["json"]["messages"]),
)

print("2. GigaChat: сначала токен, потом запрос")
reset_gigachat_token()
session = FakeSession([gigachat_token_response(), GIGACHAT_OK])
text = complete("система", "запрос", GIGACHAT_CONFIG, session=session)
check("текст извлечён из ответа", text == "Текст от GigaChat.", text)
check("первым идёт OAuth", session.calls[0]["url"] == ai_provider.GIGACHAT_OAUTH_URL)
check("вторым — чат", session.calls[1]["url"] == ai_provider.GIGACHAT_CHAT_URL)
check(
    "у OAuth есть RqUID",
    bool(session.calls[0]["headers"].get("RqUID")),
    str(session.calls[0]["headers"]),
)
check(
    "токен подставлен в Bearer",
    session.calls[1]["headers"]["Authorization"] == "Bearer token-123",
    str(session.calls[1]["headers"]),
)

print("3. Токен GigaChat переиспользуется")
session = FakeSession([GIGACHAT_OK])
complete("система", "запрос", GIGACHAT_CONFIG, session=session)
check(
    "повторный запрос идёт без нового OAuth",
    len(session.calls) == 1 and session.calls[0]["url"] == ai_provider.GIGACHAT_CHAT_URL,
    str([c["url"] for c in session.calls]),
)

print("4. Протухший токен обновляется один раз")
reset_gigachat_token()
session = FakeSession(
    [
        gigachat_token_response(),
        FakeResponse(status_code=401, payload={"message": "expired"}),
        gigachat_token_response(),
        GIGACHAT_OK,
    ]
)
text = complete("система", "запрос", GIGACHAT_CONFIG, session=session)
check("после обновления токена запрос прошёл", text == "Текст от GigaChat.", text)
check("сделано ровно четыре запроса", len(session.calls) == 4, str(len(session.calls)))

print("5. Ошибки объясняются по-русски, а не трассировкой")
reset_gigachat_token()
try:
    complete(
        "система",
        "запрос",
        YANDEX_CONFIG,
        session=FakeSession([FakeResponse(status_code=401, text="bad key")]),
    )
    check("401 поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("401 объясняет проблему с ключом", "ключ" in str(exc).lower(), str(exc))

try:
    complete(
        "система",
        "запрос",
        YANDEX_CONFIG,
        session=FakeSession([FakeResponse(status_code=429, text="slow down")]),
    )
    check("429 поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("429 говорит про лимит", "лимит" in str(exc).lower(), str(exc))

try:
    complete(
        "система",
        "запрос",
        YANDEX_CONFIG,
        session=FakeSession([FakeResponse(payload={"result": {"alternatives": []}})]),
    )
    check("пустой ответ поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("пустой ответ назван пустым", "пуст" in str(exc).lower(), str(exc))

print("6. Без ключей генерация не запускается")
try:
    complete("система", "запрос", AIConfig(provider="yandex", api_key="", folder_id=""))
    check("без ключа поднимается AIError", False, "исключения не было")
except AIError as exc:
    check("сказано, какого ключа не хватает", "YANDEX_API_KEY" in str(exc), str(exc))

off = AIConfig()
check("по умолчанию генерация выключена", not off.is_ready and "выключена" in off.problem)

os.environ["AI_PROVIDER"] = "yandexgpt"
os.environ["YANDEX_API_KEY"] = "k"
os.environ["YANDEX_FOLDER_ID"] = "f"
config = load_ai_config()
check("псевдоним провайдера распознан", config.provider == "yandex", config.provider)
check("конфиг считается готовым", config.is_ready, config.problem)
for _key in ("AI_PROVIDER", "YANDEX_API_KEY", "YANDEX_FOLDER_ID"):
    os.environ.pop(_key, None)

print("7. Карточка данных: цифры есть, лишнего нет")
messages, events, periods = sample_messages(), sample_events(), sample_periods()
card = build_data_card(
    project_name="ТЕХНОНИКОЛЬ",
    periods=periods,
    period_ids=["p1"],
    messages=messages,
    events_agg=events,
    metrics=None,
    brand_cards={
        "BPI": {"available": True, "value": 12.5, "unit": ""},
        "SOV": {"available": False, "reason": "нет выгрузки по категории"},
    },
    include_excerpts=False,
)
check("в карточке есть название проекта", "ТЕХНОНИКОЛЬ" in card)
check("есть период", "24.04.2026" in card, card[:200])
check("есть число сообщений", "Сообщений: 6" in card, card[:400])
check("есть тональность", "негатив 2" in card, card[:600])
check("есть топ тегов", "Топ тегов" in card)
check("есть инфоповоды с долей негатива", "92,3%" in card or "91,7%" in card, card)
check("видно, что инфоповод склеен", "формулировок заголовка" in card, card)
check("метрика без данных названа с причиной", "нет выгрузки по категории" in card, card)
check(
    "без выдержек тексты сообщений не уходят",
    "Жители жалуются" not in card,
    card,
)
check(
    "при одном периоде честно сказано про динамику",
    "сравнивать не с чем" in card,
    card,
)

print("8. Выдержки: включаются явно, для рисков — только негатив")
card_full = build_data_card(
    project_name="ТЕХНОНИКОЛЬ",
    periods=periods,
    period_ids=["p1"],
    messages=messages,
    events_agg=events,
    include_excerpts=True,
)
check("выдержки появились", "Жители жалуются" in card_full, card_full[-500:])
card_risk = build_data_card(
    project_name="ТЕХНОНИКОЛЬ",
    periods=periods,
    period_ids=["p1"],
    messages=messages,
    events_agg=events,
    include_excerpts=True,
    negative_excerpts=True,
)
check("в блок рисков попал негатив", "Подрядчик подал иск" in card_risk, card_risk[-500:])
check(
    "позитивные сообщения в блок рисков не попали",
    "вошла в рейтинг" not in card_risk,
    card_risk[-500:],
)

print("9. Промпт: задача, карточка и запрет выдумывать числа")
prompt = build_prompt(KIND_SUMMARY, card, extra_instructions="писать суше")
check("в промпте есть карточка", "ТЕХНОНИКОЛЬ" in prompt)
check("в промпте есть задача", "саммари периода" in prompt.lower(), prompt[:200])
check("указания заказчика переданы", "писать суше" in prompt)
check(
    "у каждого типа свой промпт",
    build_prompt(KIND_BRAND, card) != build_prompt(KIND_RISKS, card),
)
try:
    build_prompt("нет такого", card)
    check("неизвестный тип отклоняется", False, "исключения не было")
except AIError:
    check("неизвестный тип отклоняется", True)

print("10. Полный прогон генерации без сети")
session = FakeSession([YANDEX_OK])
result = generate_text(KIND_SUMMARY, card, config=YANDEX_CONFIG, session=session)
check("вернулся текст", result["text"] == "Текст саммари от модели.")
check("записан провайдер", result["provider"] == "yandex")
check("записана модель", result["model"] == "yandexgpt/latest")
check("есть оценка размера запроса", int(result["prompt_tokens_estimate"]) > 0)
check("есть отметка времени", bool(result["created_at"]))
check(
    "системный промпт запрещает выдумывать числа",
    "не придумывай" in session.calls[0]["json"]["messages"][0]["text"].lower(),
    session.calls[0]["json"]["messages"][0]["text"][:200],
)

print("11. Ключ хранения не зависит от порядка периодов")
check(
    "порядок периодов не важен",
    ai_text_storage_key(KIND_SUMMARY, ["p2", "p1"])
    == ai_text_storage_key(KIND_SUMMARY, ["p1", "p2"]),
)
check(
    "у разных типов разные ключи",
    ai_text_storage_key(KIND_SUMMARY, ["p1"]) != ai_text_storage_key(KIND_RISKS, ["p1"]),
)

print("12. Доступ к генерации: по умолчанию закрыт, открывается настройкой проекта")
check("по умолчанию доступ у владельца платформы", ai_access_level({}) == ACCESS_OWNER)
check("настройка проекта читается", ai_access_level({"ai_access": "editor"}) == ACCESS_EDITOR)
check(
    "мусор в настройке не расширяет доступ",
    ai_access_level({"ai_access": "все подряд"}) == ACCESS_OWNER,
)
check(
    "владелец платформы генерирует всегда",
    can_generate_ai("none", {}, is_platform_owner=True),
)
check(
    "редактор без разрешения не видит панель",
    not can_generate_ai("editor", {}, is_platform_owner=False),
)
check(
    "редактор с разрешением видит панель",
    can_generate_ai("editor", {"ai_access": "editor"}, is_platform_owner=False),
)
check(
    "клиент не получает генерацию даже при открытом доступе",
    not can_generate_ai("viewer", {"ai_access": "editor"}, is_platform_owner=False),
)
check(
    "пустая роль не проходит",
    not can_generate_ai("", {"ai_access": "editor"}, is_platform_owner=False),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки генерации саммари пройдены.")
