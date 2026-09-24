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
session = FakeSession([gigachat_token_response(), GIGACHAT_OK])
complete("система", "запрос", custom, session=session)
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
check(
    "старый адрес по-прежнему доступен как запасной",
    ai_provider.GIGACHAT_CHAT_URL_LEGACY.startswith("https://gigachat.devices.sberbank.ru"),
    ai_provider.GIGACHAT_CHAT_URL_LEGACY,
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

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Проверки генерации саммари пройдены.")
