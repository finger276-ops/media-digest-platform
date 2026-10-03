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
    ca_pem_to_file,
    check_connection,
    complete,
    describe_certificates,
    is_tls_trust_error,
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


def safe_complete(config, session):
    """complete, но AIError возвращается значением, а не обрывает скрипт.

    Иначе одна сломанная проверка остановила бы все следующие, и по выводу
    не было бы видно, что ещё сломалось.
    """
    try:
        return complete("система", "запрос", config, session=session)
    except AIError as exc:
        return exc


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
# Ответ в формате v2 — по образцу из документации Сбера: вместо choices список
# messages, текст — частями, finish_reason и usage в корне. В образце текст
# начинается с пробела, поэтому и здесь он с пробелом.
GIGACHAT_V2_OK = FakeResponse(
    payload={
        "model": "GigaChat-2-Max:2.0.30.01",
        "created_at": 1781694924,
        "messages": [
            {"role": "assistant", "content": [{"text": " Текст от GigaChat v2."}]}
        ],
        "finish_reason": "stop",
        "usage": {
            "input_tokens": 29,
            "input_tokens_details": {"prompt_tokens": 29, "cached_tokens": 3},
            "output_tokens": 31,
            "total_tokens": 60,
        },
    }
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
GIGACHAT_V2_CONFIG = AIConfig(
    provider="gigachat",
    api_key="basic-key",
    model="GigaChat-3-Ultra",
    chat_url=ai_provider.GIGACHAT_CHAT_URL_V2,
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
text = safe_complete(GIGACHAT_CONFIG, session)
check("текст извлечён из ответа", text == "Текст от GigaChat.", repr(text))
check("первым идёт OAuth", session.calls[0]["url"] == ai_provider.GIGACHAT_OAUTH_URL)
check(
    "вторым — чат по новому адресу",
    session.calls[1]["url"] == "https://api.giga.chat/v1/chat/completions",
    session.calls[1]["url"],
)
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
v1_json = session.calls[1]["json"]
check(
    "по умолчанию формат v1: текст сообщений — строкой",
    v1_json["messages"]
    == [{"role": "system", "content": "система"}, {"role": "user", "content": "запрос"}],
    str(v1_json["messages"]),
)
check(
    "в v1 температура и лимит ответа — в корне тела",
    v1_json.get("temperature") == 0.3 and v1_json.get("max_tokens") == 1800,
    str(v1_json),
)
check("в v1 нет model_options", "model_options" not in v1_json, str(v1_json))
check(
    "в запросе к модели есть User-Agent",
    bool(session.calls[1]["headers"].get("User-Agent")),
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

print("3.1. Адреса GigaChat меняются настройкой, а не правкой кода")
check(
    "по умолчанию — новый единый адрес",
    ai_provider.GIGACHAT_CHAT_URL == "https://api.giga.chat/v1/chat/completions",
    ai_provider.GIGACHAT_CHAT_URL,
)
reset_gigachat_token()
custom = AIConfig(
    provider="gigachat", api_key="basic-key", model="GigaChat-3-Ultra",
    chat_url="https://api.giga.chat/v2/chat/completions",
    oauth_url="https://example.test/oauth",
)
session = FakeSession([gigachat_token_response(), GIGACHAT_V2_OK])
text = safe_complete(custom, session)
check(
    "свой адрес токена доезжает",
    session.calls[0]["url"] == "https://example.test/oauth",
    session.calls[0]["url"],
)
check(
    "свой адрес чата доезжает",
    session.calls[1]["url"] == "https://api.giga.chat/v2/chat/completions",
    session.calls[1]["url"],
)
check(
    "имя модели уходит как задано",
    session.calls[1]["json"]["model"] == "GigaChat-3-Ultra",
    str(session.calls[1]["json"].get("model")),
)
# Раньше адрес /v2/ менял только адрес: тело уходило в формате v1, а ответ
# v2 (без choices) платформа не читала.
v2_json = session.calls[1]["json"]
check(
    "на адресе /v2/ текст сообщений уходит списком частей",
    v2_json["messages"]
    == [
        {"role": "system", "content": [{"text": "система"}]},
        {"role": "user", "content": [{"text": "запрос"}]},
    ],
    str(v2_json["messages"]),
)
check(
    "в v2 температура и лимит ответа — в model_options",
    v2_json.get("model_options") == {"temperature": 0.3, "max_tokens": 1800},
    str(v2_json.get("model_options")),
)
check(
    "в v2 нет температуры и лимита в корне тела",
    "temperature" not in v2_json and "max_tokens" not in v2_json,
    str(v2_json),
)
check(
    "ответ v2 разобран, пробел в начале убран",
    text == "Текст от GigaChat v2.",
    repr(text),
)
check(
    "старый адрес по-прежнему доступен как запасной",
    ai_provider.GIGACHAT_CHAT_URL_LEGACY.startswith("https://gigachat.devices.sberbank.ru"),
    ai_provider.GIGACHAT_CHAT_URL_LEGACY,
)

print("3.2. GigaChat v2: формат по адресу, разбор частей")
V2_URL = ai_provider.GIGACHAT_CHAT_URL_V2


def gigachat_with_url(url, **kwargs):
    return AIConfig(provider="gigachat", api_key="basic-key", model="GigaChat", chat_url=url, **kwargs)


def gigachat_reply(config, payload):
    """Один запрос к GigaChat с заданным ответом; вернёт (текст или AIError, сессию)."""
    reset_gigachat_token()
    reply_session = FakeSession([gigachat_token_response(), FakeResponse(payload=payload)])
    try:
        return complete("система", "запрос", config, session=reply_session), reply_session
    except AIError as exc:
        return exc, reply_session


check("по умолчанию формат v1", AIConfig(provider="gigachat").gigachat_api_version == "v1")
check("адрес /v2/ — формат v2", gigachat_with_url(V2_URL).gigachat_api_version == "v2")
check(
    "старый адрес — формат v1",
    gigachat_with_url(ai_provider.GIGACHAT_CHAT_URL_LEGACY).gigachat_api_version == "v1",
)
check(
    "/v2/ с косой чертой в конце — тоже v2",
    gigachat_with_url(V2_URL + "/").gigachat_api_version == "v2",
)
check(
    "чужой адрес с /v1/ — v1",
    gigachat_with_url("https://example.test/v1/chat/completions").gigachat_api_version == "v1",
)
# Формат решает конец пути. Адрес прокси без версии остаётся v1 (как было до
# v2), а «/v2/» в начале пути прокси не превращает обычный v1 в v2.
check(
    "адрес без версии — v1",
    gigachat_with_url("https://proxy.example.test/gigachat/chat/completions").gigachat_api_version == "v1",
)
check(
    "/v2/ в начале пути прокси, а в конце v1 — v1",
    gigachat_with_url("https://proxy.example.test/v2/gigachat/v1/chat/completions").gigachat_api_version
    == "v1",
)
check(
    "/V2/ заглавными — тоже v2",
    gigachat_with_url("https://api.giga.chat/V2/chat/completions").gigachat_api_version == "v2",
)

_, zero_session = gigachat_reply(gigachat_with_url(V2_URL, temperature=0.0), GIGACHAT_V2_OK.json())
check(
    "в v2 нулевая температура поднимается до 0.01",
    zero_session.calls[1]["json"]["model_options"]["temperature"] == 0.01,
    str(zero_session.calls[1]["json"].get("model_options")),
)

parts_text, _ = gigachat_reply(
    GIGACHAT_V2_CONFIG,
    {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"text": "Первая часть. "},
                    {"files": [{"id": "f1", "target": "image", "mime": "image/png"}]},
                    {"text": "Вторая часть."},
                ],
            }
        ]
    },
)
check(
    "части текста склеены по порядку, файл пропущен",
    parts_text == "Первая часть. Вторая часть.",
    repr(parts_text),
)

echo_text, _ = gigachat_reply(
    GIGACHAT_V2_CONFIG,
    {
        "messages": [
            {"role": "user", "content": [{"text": "эхо"}]},
            {"role": "assistant", "content": [{"text": "Ответ модели."}]},
        ]
    },
)
check(
    "сообщения не от модели не попадают в текст",
    isinstance(echo_text, str) and "эхо" not in echo_text and "Ответ модели." in echo_text,
    repr(echo_text),
)

two_text, _ = gigachat_reply(
    GIGACHAT_V2_CONFIG,
    {
        "messages": [
            {"role": "assistant", "content": [{"text": "Первый."}]},
            {"role": "assistant", "content": [{"text": "Второй."}]},
        ]
    },
)
check("два сообщения модели — через перенос строки", two_text == "Первый.\nВторой.", repr(two_text))

no_role_text, _ = gigachat_reply(
    GIGACHAT_V2_CONFIG, {"messages": [{"content": [{"text": "Без роли."}]}]}
)
check("сообщение без роли считается ответом модели", no_role_text == "Без роли.", repr(no_role_text))

for empty_label, empty_payload in (
    ("пустой список messages", {"messages": []}),
    ("сообщение без частей", {"messages": [{"role": "assistant", "content": []}]}),
):
    empty_result, _ = gigachat_reply(GIGACHAT_V2_CONFIG, empty_payload)
    check(
        f"v2, {empty_label}: AIError про пустой ответ",
        isinstance(empty_result, AIError) and "пуст" in str(empty_result).lower(),
        repr(empty_result),
    )

v1_parts, _ = gigachat_reply(
    GIGACHAT_CONFIG, {"choices": [{"message": {"content": [{"text": "Части в v1."}]}}]}
)
check("v1-ответ со списком частей тоже читается", v1_parts == "Части в v1.", repr(v1_parts))
v2_string, _ = gigachat_reply(
    GIGACHAT_V2_CONFIG, {"messages": [{"role": "assistant", "content": "Строкой."}]}
)
check("v2-ответ со строкой вместо частей тоже читается", v2_string == "Строкой.", repr(v2_string))

reset_gigachat_token()
session = FakeSession(
    [
        gigachat_token_response(),
        FakeResponse(status_code=401, payload={"message": "expired"}),
        gigachat_token_response(),
        GIGACHAT_V2_OK,
    ]
)
text = safe_complete(GIGACHAT_V2_CONFIG, session)
check("v2: после обновления токена запрос прошёл", text == "Текст от GigaChat v2.", repr(text))
check("v2: сделано ровно четыре запроса", len(session.calls) == 4, str(len(session.calls)))
check(
    "v2: повтор уходит в том же формате",
    "model_options" in session.calls[3]["json"]
    and session.calls[3]["json"]["messages"][1]["content"] == [{"text": "запрос"}],
    str(session.calls[3]["json"]),
)
check(
    "v2: User-Agent есть в обоих запросах к модели",
    bool(session.calls[1]["headers"].get("User-Agent"))
    and bool(session.calls[3]["headers"].get("User-Agent")),
    str(session.calls[3]["headers"]),
)

v2_status = ai_provider.describe_config(GIGACHAT_V2_CONFIG)
check(
    "в строке состояния видно, что включён v2",
    "v2" in v2_status and "api.giga.chat" in v2_status,
    v2_status,
)
v1_status = ai_provider.describe_config(GIGACHAT_CONFIG)
check("для v1 строка состояния прежняя, без «v2»", "v2" not in v1_status, v1_status)
legacy_status = ai_provider.describe_config(gigachat_with_url(ai_provider.GIGACHAT_CHAT_URL_LEGACY))
check("старый адрес по-прежнему помечен устаревшим", "устаревший адрес" in legacy_status, legacy_status)
legacy_slash_status = ai_provider.describe_config(
    gigachat_with_url(ai_provider.GIGACHAT_CHAT_URL_LEGACY + "/")
)
check(
    "старый адрес с косой чертой в конце — тоже устаревший",
    "устаревший адрес" in legacy_slash_status,
    legacy_slash_status,
)
# v2 на старом адресе всегда получает отказ: строка состояния не должна
# называть это рабочей настройкой и терять пометку «устаревший».
legacy_v2_status = ai_provider.describe_config(
    gigachat_with_url("https://gigachat.devices.sberbank.ru/api/v2/chat/completions")
)
check(
    "v2 на старом адресе: сказано, что не работает",
    "не работает" in legacy_v2_status
    and "устаревший адрес" in legacy_v2_status
    and "запросы в формате v2" not in legacy_v2_status,
    legacy_v2_status,
)

# load_ai_config читает и Streamlit Secrets: на время проверки отключаем их,
# чтобы локальный secrets.toml с настоящими ключами не участвовал в тесте.
_saved_st = ai_provider.st
ai_provider.st = None
_env_keys = ("AI_PROVIDER", "GIGACHAT_AUTH_KEY", "GIGACHAT_API_URL", "AI_MODEL")
try:
    os.environ["AI_PROVIDER"] = "gigachat"
    os.environ["GIGACHAT_AUTH_KEY"] = "fake-basic-key"
    loaded = load_ai_config()
    check(
        "без AI_MODEL модель по умолчанию — «GigaChat»",
        loaded.model == "GigaChat" == ai_provider.DEFAULT_GIGACHAT_MODEL,
        loaded.model,
    )
    check(
        "без GIGACHAT_API_URL — адрес v1 по умолчанию",
        loaded.chat_url == ai_provider.GIGACHAT_CHAT_URL and loaded.gigachat_api_version == "v1",
        loaded.chat_url,
    )
    check("конфиг GigaChat готов", loaded.is_ready, loaded.problem)
    os.environ["GIGACHAT_API_URL"] = V2_URL
    os.environ["AI_MODEL"] = "GigaChat-3-Ultra"
    loaded = load_ai_config()
    check("AI_MODEL доезжает", loaded.model == "GigaChat-3-Ultra", loaded.model)
    check(
        "GIGACHAT_API_URL с /v2/ включает формат v2",
        loaded.chat_url == V2_URL and loaded.gigachat_api_version == "v2",
        f"{loaded.chat_url} → {loaded.gigachat_api_version}",
    )
finally:
    ai_provider.st = _saved_st
    for _key in _env_keys:
        os.environ.pop(_key, None)

print("3.3. GigaChat: отказ по теме и оборванный ответ — ошибка, а не готовый текст")
# При отказе по теме GigaChat всё равно присылает текст — заготовку отказа.
# Раньше она становилась черновиком саммари с пометкой «готово». Список причин
# — из описания API Сбера (finish_reason в v1 и v2); он записан здесь явно,
# а не взят из модуля, чтобы потерю причины в коде было видно.
CANNED = "Не люблю менять тему разговора, но вот сейчас тот самый случай."
for filter_reason in (
    "blacklist",
    "request_blacklist",
    "request_whitelist",
    "request_filter",
    "response_blacklist",
):
    refused, _ = gigachat_reply(
        GIGACHAT_V2_CONFIG,
        {
            "messages": [{"role": "assistant", "content": [{"text": CANNED}]}],
            "finish_reason": filter_reason,
        },
    )
    check(
        f"v2 {filter_reason}: ошибка-отказ, а не текст",
        isinstance(refused, AIError) and refused.kind == ai_provider.ERROR_FILTER,
        repr(refused),
    )
    if isinstance(refused, AIError):
        check(
            f"v2 {filter_reason}: заготовки отказа нет в сообщении, причина — у владельца",
            CANNED not in str(refused) and filter_reason in refused.detail,
            f"{refused} / {refused.detail}",
        )

v1_refused, _ = gigachat_reply(
    GIGACHAT_CONFIG,
    {"choices": [{"message": {"content": CANNED}, "finish_reason": "blacklist"}]},
)
check(
    "v1 blacklist: тоже ошибка-отказ",
    isinstance(v1_refused, AIError) and v1_refused.kind == ai_provider.ERROR_FILTER,
    repr(v1_refused),
)
if isinstance(v1_refused, AIError):
    check(
        "отказ по теме подсказывает, что поменять",
        "выдержки" in str(v1_refused).lower() and "указания" in str(v1_refused),
        str(v1_refused),
    )

for cut_label, cut_config, cut_payload in (
    (
        "v2",
        GIGACHAT_V2_CONFIG,
        {
            "messages": [{"role": "assistant", "content": [{"text": "Начало саммари, оборв"}]}],
            "finish_reason": "length",
        },
    ),
    (
        "v1",
        GIGACHAT_CONFIG,
        {"choices": [{"message": {"content": "Начало саммари, оборв"}, "finish_reason": "length"}]},
    ),
):
    cut, _ = gigachat_reply(cut_config, cut_payload)
    check(
        f"{cut_label} length: оборванный ответ — ошибка про AI_MAX_TOKENS",
        isinstance(cut, AIError) and "AI_MAX_TOKENS" in str(cut) and "оборв" in str(cut),
        repr(cut),
    )
    if isinstance(cut, AIError):
        check(
            f"{cut_label} length: это настройка, не лимит запроса",
            cut.kind == "" and "Начало саммари" not in str(cut),
            f"{cut.kind!r}: {cut}",
        )

v1_stop, _ = gigachat_reply(
    GIGACHAT_CONFIG, {"choices": [{"message": {"content": "Готово."}, "finish_reason": "stop"}]}
)
check("v1 stop: обычный ответ проходит", v1_stop == "Готово.", repr(v1_stop))

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
text = safe_complete(GIGACHAT_CONFIG, session)
check("после обновления токена запрос прошёл", text == "Текст от GigaChat.", repr(text))
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

print("7.5. Площадки и типы сообщений в карточке")
typed = sample_messages().assign(
    platform=["vk.com", "vk.com", "t.me", "vk.com", "telegram.org", ""],
    message_link=[""] * 5 + ["https://otzovik.com/review/1"],
    message_type=["Пост", "Комментарий", "Пост", "Пост", "Репост", "Пост"],
)
card_typed = build_data_card(project_name="ТЕХНОНИКОЛЬ", periods=periods, period_ids=["p1"], messages=typed,
                             events_agg=events, include_excerpts=False)
check("типы сообщений в карточке",
      "Типы сообщений: пост — 4 (66,7%), комментарий — 1 (16,7%), репост — 1 (16,7%)." in card_typed, card_typed)
check("площадки — домены с долей и негативом",
      "- vk.com: 3 сообщ. (50,0%), негатив 2 (66,7%)" in card_typed
      and "- telegram.org: 2 сообщ. (33,3%)" in card_typed and "- otzovik.com: 1 сообщ." in card_typed, card_typed)
check("в блоке площадок нет названий сообществ", "Телеграм-канал" not in card_typed and "Пикабу" not in card_typed)
check("без типа в выгрузке — так и сказано", "в выгрузке тип сообщения не указан" in card, card)
unmarked_typed = typed.assign(sentiment="")
card_unmarked = build_data_card(project_name="Т", periods=periods, period_ids=["p1"], messages=unmarked_typed,
                                events_agg=events, include_excerpts=False)
check("без разметки у площадок нет «негатив 0»", "- vk.com: 3 сообщ. (50,0%)\n" in card_unmarked + "\n"
      and "vk.com: 3 сообщ. (50,0%), негатив" not in card_unmarked, card_unmarked)
check("типы из метрик периода, если они посчитаны",
      "Типы сообщений: пост — 10 (100,0%)." in build_data_card(
          project_name="Т", periods=periods, period_ids=["p1"], messages=typed, events_agg=events,
          metrics={"messages": 10, "sentiment": {}, "message_types": [("Пост", 10)]}, include_excerpts=False))
check("задание саммари упоминает площадки и формат", "на каких площадках и в каком формате" in build_prompt(KIND_SUMMARY, "x"))
check("задание рисков просит назвать площадку", "площадка, если негатив сосредоточен на одной" in build_prompt(KIND_RISKS, "x")
      .replace("\n", " "))

check("срез назван в карточке", "Срез: только сообщения с тегами (тег: Технониколь)." in build_data_card(
    project_name="Т", periods=periods, period_ids=["p1"], messages=typed, events_agg=events,
    include_excerpts=False, slice_label="тег: Технониколь"))
check("без среза — строки о срезе нет", "Срез:" not in card_typed)
check("у среза свой ключ текста ИИ", ai_text_storage_key(KIND_SUMMARY, ["p1"], "tags=технониколь")
      == "ai_text::summary::p1::tags=технониколь" != ai_text_storage_key(KIND_SUMMARY, ["p1"]))

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

print("8.5. По дням: пиковый день виден модели, а не выдумывается на скудных данных")
# _comparison_block выше - период к периоду, это заголовочная динамика (на
# ней держатся цифры PNG/DOCX/PDF, её менять нельзя). Блок "по дням" -
# дополнение: даёт модели сказать "пик пришёлся на 26.04", а не только
# "негатив вырос". Дни: 24.04(1, без негатива), 25.04(2, 1 негатив),
# 26.04(3, все три негативные - и самый насыщенный, и худший по негативу),
# 27.04(1, нейтрал).
daily_messages = pd.DataFrame(
    {
        "message_id": [f"d{i}" for i in range(7)],
        "period_id": ["p1"] * 7,
        "datetime": [
            "2026-04-24T10:00:00",
            "2026-04-25T10:00:00",
            "2026-04-25T11:00:00",
            "2026-04-26T10:00:00",
            "2026-04-26T11:00:00",
            "2026-04-26T12:00:00",
            "2026-04-27T10:00:00",
        ],
        "sentiment": [
            "позитив",
            "негатив",
            "нейтрал",
            "негатив",
            "негатив",
            "негатив",
            "нейтрал",
        ],
        "views": [100] * 7,
        "audience": [50] * 7,
        "engagement": [5] * 7,
        "text_clean": ["Текст"] * 7,
    }
)
card_daily = build_data_card(
    project_name="ТЕХНОНИКОЛЬ",
    periods=periods,
    period_ids=["p1"],
    messages=daily_messages,
    events_agg=events,
    include_excerpts=False,
)
check(
    "блок по дням появился - дат хватает (4 дня)",
    "По дням внутри периода" in card_daily,
    card_daily[-500:],
)
check(
    "самый насыщенный день назван верно - 26.04 (3 сообщения)",
    "больше всего сообщений: 26.04 (3)" in card_daily,
    card_daily[-500:],
)
check(
    "день пика негатива назван верно - 26.04 (3 негативных)",
    "больше всего негативных сообщений: 26.04 (3)" in card_daily,
    card_daily[-500:],
)
check(
    "без колонки datetime блока по дням нет - карточка из раздела 7 без изменений",
    "По дням внутри периода" not in card,
    card,
)
two_day_messages = daily_messages[daily_messages["message_id"].isin(["d0", "d1"])]
card_two_days = build_data_card(
    project_name="ТЕХНОНИКОЛЬ",
    periods=periods,
    period_ids=["p1"],
    messages=two_day_messages,
    events_agg=events,
    include_excerpts=False,
)
check(
    "на двух днях блока по дням нет - весь диапазон не наблюдение, а не пик",
    "По дням внутри периода" not in card_two_days,
    card_two_days,
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

print("13. Сертификат НУЦ Минцифры: ошибка объясняется, а не падает трассировкой")


class SslSession:
    """Транспорт, который падает ровно так же, как requests без корневого сертификата."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        raise RuntimeError(
            "HTTPSConnectionPool(host='ngw.devices.sberbank.ru', port=9443): "
            "Max retries exceeded with url: /api/v2/oauth (Caused by SSLError("
            "SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed: self-signed certificate in certificate chain')))"
        )


check("ошибка доверия опознаётся", is_tls_trust_error("CERTIFICATE_VERIFY_FAILED"))
check(
    "обрыв сети не путается с недоверием",
    not is_tls_trust_error("Connection reset by peer"),
)

reset_gigachat_token()
try:
    complete("система", "запрос", GIGACHAT_CONFIG, session=SslSession())
    check("падение по сертификату превращается в AIError", False, "исключения не было")
except AIError as exc:
    text = str(exc)
    check("названа причина — НУЦ Минцифры", "НУЦ Минцифры" in text, text[:160])
    check("сказано, куда положить файл", "certs/" in text, text[:200])
    check("дана команда скачивания", "gu-st.ru" in text, text[:300])
    check(
        "отключение проверки предложено только как временная мера",
        "временную меру" in text,
        text[-160:],
    )

reset_gigachat_token()
with_bundle = AIConfig(
    provider="gigachat", api_key="basic-key", model="GigaChat",
    ca_bundle="/opt/certs/russian_trusted_ca.pem",
)
try:
    complete("система", "запрос", with_bundle, session=SslSession())
    check("с указанным файлом тоже AIError", False, "исключения не было")
except AIError as exc:
    check(
        "если файл задан — сообщение про этот файл",
        "/opt/certs/russian_trusted_ca.pem" in str(exc),
        str(exc)[:200],
    )

print("14. Путь к сертификату доезжает до запроса")
reset_gigachat_token()
session = FakeSession([gigachat_token_response(), GIGACHAT_OK])
complete("система", "запрос", with_bundle, session=session)
check(
    "verify получает путь к файлу",
    all(c["verify"] == "/opt/certs/russian_trusted_ca.pem" for c in session.calls),
    str([c.get("verify") for c in session.calls]),
)

reset_gigachat_token()
session = FakeSession([gigachat_token_response(), GIGACHAT_OK])
complete("система", "запрос", GIGACHAT_CONFIG, session=session)
check(
    "без файла — обычная системная проверка",
    all(c["verify"] is True for c in session.calls),
    str([c.get("verify") for c in session.calls]),
)

reset_gigachat_token()
off_verify = AIConfig(
    provider="gigachat", api_key="basic-key", model="GigaChat", verify_ssl=False
)
session = FakeSession([gigachat_token_response(), GIGACHAT_OK])
complete("система", "запрос", off_verify, session=session)
check(
    "выключенная проверка доезжает как False",
    all(c["verify"] is False for c in session.calls),
    str([c.get("verify") for c in session.calls]),
)

print("15. Кнопка «Проверить подключение»")
reset_gigachat_token()
session = FakeSession([gigachat_token_response(), GIGACHAT_OK])
result = check_connection(GIGACHAT_CONFIG, session=session)
check("проверка возвращает ответ модели", "GigaChat ответил" in result, result)
check(
    "проверка стоит один короткий запрос",
    len(session.calls[-1]["json"]["messages"][1]["content"]) < 60,
    session.calls[-1]["json"]["messages"][1]["content"],
)
reset_gigachat_token()
session = FakeSession([gigachat_token_response(), GIGACHAT_V2_OK])
result = check_connection(GIGACHAT_V2_CONFIG, session=session)
check("проверка в формате v2 возвращает ответ модели", "GigaChat ответил" in result, result)
check(
    "проверка в формате v2 — тоже один короткий запрос",
    len(session.calls[-1]["json"]["messages"][1]["content"][0]["text"]) < 60,
    str(session.calls[-1]["json"]["messages"][1]["content"]),
)

print("16. Сертификат заводится текстом, без терминала")
SAMPLE_PEM = """-----BEGIN CERTIFICATE-----
MIIFwqmUBMRUAAAAAAUkwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIFwjCCA6qgAwIBAgICEAAwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
-----END CERTIFICATE-----
"""
path = ca_pem_to_file(SAMPLE_PEM)
check("текст сертификата сохранён в файл", bool(path) and Path(path).is_file(), path)
check(
    "повторный вызов не плодит файлы",
    ca_pem_to_file(SAMPLE_PEM) == path,
    path,
)
check("пустой текст не создаёт файла", ca_pem_to_file("") == "")
check(
    "в файле сохранено ровно то, что дали",
    Path(path).read_text(encoding="utf-8").count("BEGIN CERTIFICATE") == 2,
)

conf = AIConfig(provider="gigachat", api_key="k", ca_bundle=path)
check("этот путь уходит в verify", conf.tls_verify == path, str(conf.tls_verify))

check("мусор не выдаётся за сертификат", describe_certificates("просто текст") == [])
real = describe_certificates(SAMPLE_PEM)
check(
    "битый PEM помечается как неразобранный",
    real and all(not b.get("ok") for b in real),
    str(real),
)

print("17. Карточки для ИИ видят долю голоса по тегам, как раздел «Индексы бренда»")
# Раньше карточки для ИИ брали SOV только из загруженной выгрузки по категории.
# Если конкуренты размечены тегами самой выгрузки проекта, раздел показывал
# долю голоса, а ИИ получал прочерк и писал «доля голоса не посчитана».
from fake_supabase import FakeClient  # noqa: E402

from ai_summary_ui import _brand_cards  # noqa: E402
from services import category_store  # noqa: E402

fake_db = FakeClient()
category_store.get_supabase_client = lambda: fake_db
brand_messages = pd.DataFrame(
    [
        {"message_id": f"b{i}", "period_id": "p1", "tags": tag, "sentiment": "нейтральная",
         "views": 1000, "audience": 5000, "author": f"a{i}"}
        for i, tag in enumerate(["Бренд А", "Бренд А", "Бренд Б", "Бренд Б|Бренд А"])
    ]
)
brand_settings = {"category_brands": {"own": ["Бренд А"], "competitors": ["Бренд Б"]}}
cards = _brand_cards("proj-ai", brand_settings, brand_messages, ["p1"])
check("SOV по тегам доступен", bool(cards) and cards["SOV"]["available"], str(cards.get("SOV")))
check(
    "SOV по тегам посчитан (3 из 5 упоминаний брендов)",
    bool(cards) and cards["SOV"]["value"] is not None and abs(cards["SOV"]["value"] - 60.0) < 0.05,
    str(cards.get("SOV", {}).get("value")),
)

fake_db.db["platform_category_benchmarks"] = [
    {
        "project_id": "proj-ai",
        "period_id": "p1",
        "own_brand": "Бренд А",
        "brands": [
            {"brand": "Бренд А", "messages": 10, "audience": 0, "reach": 0, "engagement": 0, "is_own": True},
            {"brand": "Бренд В", "messages": 30, "audience": 0, "reach": 0, "engagement": 0, "is_own": False},
        ],
    }
]
uploaded_cards = _brand_cards("proj-ai", brand_settings, brand_messages, ["p1"])
check(
    "загруженная выгрузка по категории главнее тегов",
    bool(uploaded_cards) and uploaded_cards["SOV"]["value"] is not None
    and abs(uploaded_cards["SOV"]["value"] - 25.0) < 0.05,
    str(uploaded_cards.get("SOV", {}).get("value")),
)

print("18. Лимиты модели и квоты объясняются по-человечески, без JSON провайдера")
# Раньше любой отказ, кроме 401/403/429/5xx, показывался как «ошибка 400.
# {"error": ...}», а исчерпанная квота на 429 — как «попробуйте через минуту»,
# хотя через минуту ничего не изменится (а в демо каждый повтор съедает запуск).
# Тексты ответов ниже — формы, которыми отвечают YandexGPT, GigaChat и шлюзы
# в духе OpenAI; классификация идёт по признакам, а не по одной фразе.
from services.ai_provider import ERROR_CONTEXT, ERROR_QUOTA, ERROR_RATE  # noqa: E402

LIMIT_CASES = [
    (
        "YandexGPT 400: входных токенов больше лимита",
        YANDEX_CONFIG,
        FakeResponse(400, {"error": {"grpcCode": 3, "httpCode": 400, "message": "Number of input tokens must be no more than 8192, got 9136", "httpStatus": "Bad Request"}}),
        ERROR_CONTEXT,
    ),
    (
        "400: maximum context length",
        YANDEX_CONFIG,
        FakeResponse(400, text='{"error":{"message":"This model\'s maximum context length is 8192 tokens"}}'),
        ERROR_CONTEXT,
    ),
    (
        "GigaChat 413 без пояснений",
        GIGACHAT_CONFIG,
        FakeResponse(413, {"status": 413, "message": "Payload Too Large"}),
        ERROR_CONTEXT,
    ),
    (
        "GigaChat 400: «Слишком много токенов»",
        GIGACHAT_CONFIG,
        FakeResponse(400, {"status": 400, "message": "Слишком много токенов в запросе: 33000 при лимите 32768"}),
        ERROR_CONTEXT,
    ),
    (
        "GigaChat 422: token limit",
        GIGACHAT_CONFIG,
        FakeResponse(422, {"status": 422, "message": "Input exceeds token limit"}),
        ERROR_CONTEXT,
    ),
    (
        "GigaChat 402: закончился пакет токенов",
        GIGACHAT_CONFIG,
        FakeResponse(402, {"status": 402, "message": "Payment Required"}),
        ERROR_QUOTA,
    ),
    (
        "402 без пояснений: квоту видно по одному коду",
        GIGACHAT_CONFIG,
        FakeResponse(402, text="{}"),
        ERROR_QUOTA,
    ),
    (
        "429 с квотой: деньги кончились, а не частота",
        YANDEX_CONFIG,
        FakeResponse(429, {"error": {"httpCode": 429, "message": "Quota exceeded: monthly token quota is exhausted", "httpStatus": "Too Many Requests"}}),
        ERROR_QUOTA,
    ),
    (
        "YandexGPT 429: квота частоты (…rate) — это лимит частоты, не деньги",
        YANDEX_CONFIG,
        FakeResponse(429, {"error": {"grpcCode": 8, "httpCode": 429, "message": "Quota limit ai.languageModels.requestCount.rate exceeded", "httpStatus": "Too Many Requests"}}),
        ERROR_RATE,
    ),
    (
        "GigaChat 429 без пояснений",
        GIGACHAT_CONFIG,
        FakeResponse(429, {"status": 429, "message": "Too Many Requests"}),
        ERROR_RATE,
    ),
    # Лимит одновременных генераций Yandex Cloud: слово «quota» есть, но он
    # снимается, как только освободится место. «Повтор не поможет» было бы
    # ложью, а владелец пошёл бы пополнять баланс без нужды.
    (
        "YandexGPT 429: лимит одновременных генераций — временный, не деньги",
        YANDEX_CONFIG,
        FakeResponse(429, {"error": {"grpcCode": 8, "httpCode": 429, "message": "ai.textGenerationCompletionSessionsCount.count gauge quota limit exceed: allowed 10 requests", "httpStatus": "Too Many Requests"}}),
        ERROR_RATE,
    ),
    (
        "429 «Quota exceeded» без признаков денег — временный лимит",
        YANDEX_CONFIG,
        FakeResponse(429, {"error": {"httpCode": 429, "message": "Quota exceeded", "httpStatus": "Too Many Requests"}}),
        ERROR_RATE,
    ),
    # «Исчерпан» бывает и про минутный лимит: признак частоты важнее слова,
    # похожего на деньги.
    (
        "429 «Лимит запросов в минуту исчерпан» — частота, не деньги",
        GIGACHAT_CONFIG,
        FakeResponse(429, {"status": 429, "message": "Лимит запросов в минуту исчерпан, повторите позже"}),
        ERROR_RATE,
    ),
    # Формат v2 меняет тело запроса, но не коды ошибок: лимиты объясняются так же.
    (
        "GigaChat v2 413",
        GIGACHAT_V2_CONFIG,
        FakeResponse(413, {"status": 413, "message": "Payload Too Large"}),
        ERROR_CONTEXT,
    ),
    (
        "GigaChat v2 429",
        GIGACHAT_V2_CONFIG,
        FakeResponse(429, {"status": 429, "message": "Too Many Requests"}),
        ERROR_RATE,
    ),
]

for label, limit_config, response, expected_kind in LIMIT_CASES:
    reset_gigachat_token()
    responses = [response]
    if limit_config.provider == "gigachat":
        responses.insert(0, gigachat_token_response())
    try:
        complete("система", "запрос", limit_config, session=FakeSession(responses))
        check(f"{label}: поднимает AIError", False, "исключения не было")
        continue
    except AIError as exc:
        error = exc
    message = str(error)
    check(f"{label}: вид ошибки — {expected_kind}", error.kind == expected_kind, f"{error.kind!r}: {message}")
    check(
        f"{label}: в тексте нет сырого ответа сервиса",
        "{" not in message and response.text[:40] not in message,
        message,
    )
    check(
        f"{label}: сырой ответ сохранён для владельца",
        response.text[:40] in error.detail,
        error.detail[:80],
    )
    if expected_kind == ERROR_CONTEXT:
        check(f"{label}: сказано, что запрос не поместился", "не поместился" in message, message)
        check(f"{label}: сказано, что сократить — выдержки", "выдержки" in message.lower(), message)
    if expected_kind == ERROR_QUOTA:
        check(f"{label}: названа квота", "квота" in message.lower(), message)
        check(f"{label}: повторять бесполезно", "не поможет" in message, message)
        check(f"{label}: не зовёт «через минуту»", "минуту" not in message, message)
        where = "Yandex Cloud" if limit_config.provider == "yandex" else "кабинете GigaChat"
        check(f"{label}: сказано, где пополнить", where in message, message)
    if expected_kind == ERROR_RATE:
        check(f"{label}: предложено подождать минуту", "минуту" in message, message)
        check(f"{label}: не зовёт пополнять баланс", "баланс" not in message, message)

# Незнакомые отказы классифицировать нельзя — их текст остаётся в сообщении,
# иначе владельцу нечем будет разобраться.
reset_gigachat_token()
try:
    complete(
        "система",
        "запрос",
        YANDEX_CONFIG,
        session=FakeSession([FakeResponse(400, text="modelUri is invalid")]),
    )
    check("незнакомая 400 поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("незнакомая 400 не выдаётся за лимит", exc.kind == "", repr(exc.kind))
    check("её текст виден как раньше", "modelUri is invalid" in str(exc), str(exc))

# Ошибка выдачи токена GigaChat идёт тем же путём, но про лимиты ничего не
# говорит: «токен» в тексте не должен превращать её в «не поместился».
reset_gigachat_token()
try:
    complete(
        "система",
        "запрос",
        GIGACHAT_CONFIG,
        session=FakeSession([FakeResponse(400, {"code": 7, "message": "invalid token request: scope from db not fully includes consumed scope"})]),
    )
    check("отказ OAuth поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("отказ OAuth не выдаётся за лимит модели", exc.kind == "", f"{exc.kind!r}: {exc}")

# Отказ по правам со словами «quota» и «insufficient» — это про доступ, а не
# про деньги: «пополните баланс» отправило бы владельца не туда.
reset_gigachat_token()
try:
    complete(
        "система",
        "запрос",
        YANDEX_CONFIG,
        session=FakeSession([FakeResponse(403, {"error": {"httpCode": 403, "message": "Permission denied: insufficient permissions to use quota of folder b1g", "httpStatus": "Forbidden"}})]),
    )
    check("отказ по правам поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("отказ по правам не выдаётся за квоту", exc.kind == "", f"{exc.kind!r}: {exc}")
    check("сказано, что отклонён доступ", "отклонил ключ" in str(exc), str(exc))

# А 403 из-за заблокированного за неуплату аккаунта — это деньги: «проверьте
# ключ» отправило бы владельца искать ошибку там, где её нет.
reset_gigachat_token()
try:
    complete(
        "система",
        "запрос",
        YANDEX_CONFIG,
        session=FakeSession([FakeResponse(403, {"error": {"httpCode": 403, "message": "Billing account is not active", "httpStatus": "Forbidden"}})]),
    )
    check("403 по оплате поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("403 по оплате — квота, а не ключ", exc.kind == ERROR_QUOTA, f"{exc.kind!r}: {exc}")

check(
    "старое поведение: AIError по-прежнему создаётся одной строкой",
    str(AIError("текст")) == "текст" and AIError("текст").kind == "" and AIError("текст").detail == "",
)

print("19. Понятные ошибки GigaChat: модель и адрес")
# 404 у GigaChat — «нет такой модели», а 403 со страницей HTML — неверный
# путь (например, v2 на старом адресе). Раньше первое выглядело как «ошибка 404»
# с JSON, а второе — как «отклонил ключ», хотя ключ в порядке.
reset_gigachat_token()
no_model = FakeResponse(404, {"status": 404, "message": "No such model"})
try:
    complete(
        "система",
        "запрос",
        GIGACHAT_V2_CONFIG,
        session=FakeSession([gigachat_token_response(), no_model]),
    )
    check("404 GigaChat поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("404: сказано проверить AI_MODEL", "AI_MODEL" in str(exc), str(exc))
    check("404: в тексте нет сырого ответа", "{" not in str(exc), str(exc))
    check("404: сырой ответ сохранён для владельца", "No such model" in exc.detail, exc.detail)
    check("404: это не лимит", exc.kind == "", repr(exc.kind))

reset_gigachat_token()
try:
    complete(
        "система",
        "запрос",
        YANDEX_CONFIG,
        session=FakeSession([FakeResponse(404, text="folder not found")]),
    )
    check("404 YandexGPT поднимает AIError", False, "исключения не было")
except AIError as exc:
    check(
        "404 YandexGPT объясняется как раньше",
        "ошибка 404" in str(exc) and "folder not found" in str(exc) and "AI_MODEL" not in str(exc),
        str(exc),
    )

HTML_403 = "<html>\r\n<head><title>403 Forbidden</title></head>\r\n<body><center><h1>403 Forbidden</h1></center></body>\r\n</html>"
reset_gigachat_token()
wrong_path = AIConfig(
    provider="gigachat",
    api_key="basic-key",
    model="GigaChat",
    chat_url="https://gigachat.devices.sberbank.ru/api/v2/chat/completions",
)
# Страница HTML значит «по этому адресу сервиса нет»: новый токен тут не
# поможет, поэтому повтора нет и рабочий токен в кэше не сбрасывается.
session = FakeSession([gigachat_token_response(), FakeResponse(403, text=HTML_403)])
try:
    complete("система", "запрос", wrong_path, session=session)
    check("403 со страницей HTML поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("HTML-403: без повтора — токен ни при чём", len(session.calls) == 2, str(len(session.calls)))
    check("HTML-403: сказано проверить GIGACHAT_API_URL", "GIGACHAT_API_URL" in str(exc), str(exc))
    check("HTML-403: в тексте нет страницы HTML", "<html" not in str(exc), str(exc))
    check("HTML-403: страница сохранена для владельца", "<html" in exc.detail, exc.detail[:80])
    check("HTML-403: это не лимит", exc.kind == "", repr(exc.kind))
cached_session = FakeSession([GIGACHAT_OK])
safe_complete(GIGACHAT_CONFIG, cached_session)
check(
    "HTML-403: токен в кэше не сброшен — следующий запрос без OAuth",
    [c["url"] for c in cached_session.calls] == [ai_provider.GIGACHAT_CHAT_URL],
    str([c["url"] for c in cached_session.calls]),
)

# Страницы у веб-серверов разные: с DOCTYPE, заглавными, без тега <html>.
for page_label, page_status, page in (
    ("403 <!DOCTYPE HTML> заглавными", 403, "<!DOCTYPE HTML>\n<HTML><BODY>403</BODY></HTML>"),
    ("403 без тега <html>", 403, "<!DOCTYPE html><head><title>SynGX</title></head><body>403</body>"),
    ("404 страницей", 404, "<html><body><h1>404 Not Found</h1></body></html>"),
):
    reset_gigachat_token()
    page_error = safe_complete(
        GIGACHAT_V2_CONFIG,
        FakeSession([gigachat_token_response(), FakeResponse(page_status, text=page)]),
    )
    check(
        f"{page_label}: про адрес, а не про ключ или модель",
        isinstance(page_error, AIError)
        and "GIGACHAT_API_URL" in str(page_error)
        and "отклонил ключ" not in str(page_error)
        and "AI_MODEL" not in str(page_error),
        repr(page_error),
    )
    check(
        f"{page_label}: в тексте нет страницы",
        isinstance(page_error, AIError) and "<" not in str(page_error),
        str(page_error),
    )

reset_gigachat_token()
json_403 = FakeResponse(403, {"status": 403, "message": "Unauthorized"})
session = FakeSession([gigachat_token_response(), json_403, gigachat_token_response(), json_403])
try:
    complete("система", "запрос", GIGACHAT_V2_CONFIG, session=session)
    check("403 с JSON поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("403 с JSON по-прежнему про ключ", "отклонил ключ" in str(exc), str(exc))
    check("403 с JSON: один повтор с новым токеном, как раньше", len(session.calls) == 4, str(len(session.calls)))

# Те же коды от адреса токена — не про модель и не про GIGACHAT_API_URL.
reset_gigachat_token()
try:
    complete(
        "система",
        "запрос",
        GIGACHAT_CONFIG,
        session=FakeSession([FakeResponse(404, text="not found")]),
    )
    check("404 от адреса токена поднимает AIError", False, "исключения не было")
except AIError as exc:
    check("404 от адреса токена не зовёт править AI_MODEL", "AI_MODEL" not in str(exc), str(exc))

print("20. Ключ не попадает в текст ошибки")
# Ключ, вставленный в Secrets с переносом строки, requests не отправляет и
# пишет в текст ошибки сам заголовок — то есть ключ. Владелец видит этот текст
# на странице. Ключ ниже — выдуманный.
import requests  # noqa: E402


class RaisingSession:
    """Транспорт, который падает на первом же запросе заданным исключением."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        raise self.exc


def invalid_header_error(value):
    """Настоящая ошибка requests для такого заголовка — без сети."""
    try:
        requests.models.PreparedRequest().prepare_headers({"Authorization": value})
    except requests.exceptions.InvalidHeader as exc:
        return exc
    return None


LEAKY_KEY = "FAKEKEYPART1\nFAKEKEYPART2"
for leak_label, leak_config, header in (
    (
        "GigaChat",
        AIConfig(provider="gigachat", api_key=LEAKY_KEY, model="GigaChat"),
        f"Basic {LEAKY_KEY}",
    ),
    (
        "YandexGPT",
        AIConfig(provider="yandex", api_key=LEAKY_KEY, folder_id="folder-1", model="yandexgpt/latest"),
        f"Api-Key {LEAKY_KEY}",
    ),
):
    header_exc = invalid_header_error(header)
    check(f"{leak_label}: requests отвергает ключ с переносом строки", header_exc is not None)
    if header_exc is None:
        continue
    reset_gigachat_token()
    leak_error = safe_complete(leak_config, RaisingSession(header_exc))
    leak_text = str(leak_error)
    check(f"{leak_label}: ключа нет в тексте ошибки", "FAKEKEYPART" not in leak_text, leak_text)
    expected_name = "GIGACHAT_AUTH_KEY" if leak_label == "GigaChat" else "YANDEX_API_KEY"
    check(
        f"{leak_label}: сказано, какую настройку вставить заново",
        isinstance(leak_error, AIError) and expected_name in leak_text and "одной строкой" in leak_text,
        leak_text,
    )

# Любая другая ошибка транспорта: текст виден владельцу, но ключ из него убран.
reset_gigachat_token()
other_error = safe_complete(
    AIConfig(provider="gigachat", api_key=LEAKY_KEY, model="GigaChat"),
    RaisingSession(OSError(f"сбой при отправке {('Basic ' + LEAKY_KEY)!r}")),
)
check(
    "прочие ошибки: текст на месте, ключа нет",
    isinstance(other_error, AIError)
    and "Не удалось обратиться к модели" in str(other_error)
    and "FAKEKEYPART" not in str(other_error),
    str(other_error),
)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки генерации саммари пройдены.")
