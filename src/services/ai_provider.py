"""Транспорт к российским языковым моделям: YandexGPT и GigaChat.

Почему именно они: данные проектов — это бренд-мониторинг клиентов, и выносить
тексты сообщений за пределы РФ нельзя без отдельного согласия. Оба провайдера
принимают оплату в рублях и доступны с российского сервера без прокси.

Модуль не знает ничего про Streamlit и про саммари: его дело — отдать текст по
системному и пользовательскому промпту, а при ошибке объяснить её по-русски,
а не уронить приложение трассировкой.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

try:  # pragma: no cover - в воркере Streamlit не нужен
    import streamlit as st
except Exception:  # pragma: no cover
    st = None  # type: ignore

PROVIDER_OFF = "off"
PROVIDER_YANDEX = "yandex"
PROVIDER_GIGACHAT = "gigachat"

PROVIDER_TITLES = {
    PROVIDER_OFF: "Выключено",
    PROVIDER_YANDEX: "YandexGPT",
    PROVIDER_GIGACHAT: "GigaChat",
}

YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

# С 17 июля 2026 года у GigaChat единый адрес для всех — физлиц и компаний.
# Старый адрес пока работает, но объявлен устаревшим, поэтому по умолчанию
# используем новый, а старый оставляем доступным через настройку.
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://api.giga.chat/v1/chat/completions"
GIGACHAT_CHAT_URL_LEGACY = (
    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
)

DEFAULT_TIMEOUT = 90.0
DEFAULT_MAX_TOKENS = 1800
DEFAULT_TEMPERATURE = 0.3


# Вид ошибки — чтобы интерфейс и тесты различали случаи, не разбирая текст
# сообщения. Пустая строка — всё остальное.
ERROR_CONTEXT = "context"  # запрос не поместился в модель — поможет сократить его
ERROR_QUOTA = "quota"  # кончились оплаченные токены — повтор не поможет
ERROR_RATE = "rate"  # слишком часто — пройдёт само через минуту


class AIError(Exception):
    """Ошибка генерации, которую можно показать пользователю как есть.

    `detail` — исходный ответ сервиса, если его убрали из текста сообщения.
    Гостю демо и редактору JSON провайдера ничего не скажет, а владельцу без
    него не понять, что именно ответил сервис, — поэтому он лежит отдельно.
    """

    def __init__(self, message: str = "", *, kind: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def _secret_value(*names: str) -> str:
    """Значение из Streamlit Secrets или переменных окружения.

    Тот же порядок, что и в `platform_store`: сначала секреты приложения,
    потом окружение — чтобы воркер и интерфейс настраивались одинаково.
    """
    for name in names:
        if st is not None:
            try:
                if name in st.secrets:
                    return str(st.secrets[name]).strip()
            except Exception:  # noqa: BLE001 — файла секретов может не быть вовсе
                pass
            try:
                if "ai" in st.secrets:
                    section_key = name.lower()
                    for prefix in ("ai_", "yandex_", "gigachat_"):
                        section_key = section_key.replace(prefix, "", 1)
                    if section_key in st.secrets["ai"]:
                        return str(st.secrets["ai"][section_key]).strip()
            except Exception:  # noqa: BLE001 — секция [ai] необязательна
                pass
        value = os.getenv(name)
        if value:
            return str(value).strip()
    return ""


# Сертификаты GigaChat подписаны НУЦ Минцифры, которого нет в системных
# хранилищах. Правильное решение — доверять этому корню точечно, только для
# запросов к GigaChat, а не отключать проверку и не подмешивать корень в
# системный список. Файл кладётся в папку certs/ в корне репозитория.
CA_BUNDLE_DIR = "certs"
CA_BUNDLE_NAMES = (
    "russian_trusted_ca.pem",
    "russian_trusted_root_ca.crt",
    "russian_trusted_root_ca_pem.crt",
    "russian_trusted_root_ca.pem",
)
CA_DOWNLOAD_HINT = (
    "mkdir -p certs && "
    "curl -sS -w '\\n' https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt "
    "> certs/russian_trusted_ca.pem && "
    "curl -sS -w '\\n' https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt "
    ">> certs/russian_trusted_ca.pem"
)


def bundled_ca_path() -> str:
    """Путь к сертификату НУЦ Минцифры в репозитории, если он там лежит."""
    from pathlib import Path  # noqa: PLC0415

    root = Path(__file__).resolve().parents[2]
    for name in CA_BUNDLE_NAMES:
        candidate = root / CA_BUNDLE_DIR / name
        if candidate.is_file():
            return str(candidate)
    return ""


def ca_pem_to_file(pem_text: str) -> str:
    """Сохранить сертификат из секрета во временный файл и вернуть путь.

    Так сертификат можно завести без терминала и без коммита в репозиторий:
    текст вставляется в Streamlit Secrets, а `requests` всё равно нужен путь
    к файлу. Файл пишется один раз на содержимое — повторные вызовы попадают
    в уже готовый.
    """
    import hashlib  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    text = str(pem_text or "").strip()
    if not text:
        return ""
    if not text.endswith("\n"):
        text += "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    target = Path(tempfile.gettempdir()) / f"gigachat_ca_{digest}.pem"
    if not target.is_file():
        try:
            target.write_text(text, encoding="utf-8")
        except OSError:
            return ""
    return str(target)


def describe_certificates(pem_text: str) -> list[dict[str, Any]]:
    """Разобрать PEM и вернуть, что именно в нём лежит.

    Нужно, чтобы человек, вставивший файл, сразу увидел — это действительно
    сертификаты НУЦ Минцифры и они не просрочены, а не «кажется, что-то не то».
    Без библиотеки cryptography возвращается пустой список: проверка
    необязательная, она не должна ломать основной путь.
    """
    try:
        from cryptography import x509  # noqa: PLC0415
    except Exception:  # pragma: no cover - проверка необязательная
        return []

    from datetime import datetime, timezone  # noqa: PLC0415

    blocks = []
    marker = "-----BEGIN CERTIFICATE-----"
    for chunk in str(pem_text or "").split(marker):
        if "-----END CERTIFICATE-----" not in chunk:
            continue
        body = marker + chunk.split("-----END CERTIFICATE-----")[0]
        body += "-----END CERTIFICATE-----\n"
        try:
            cert = x509.load_pem_x509_certificate(body.encode("utf-8"))
        except Exception:
            blocks.append({"ok": False, "subject": "не разобрался", "expired": False})
            continue
        try:
            subject = cert.subject.rfc4514_string()
        except Exception:
            subject = "?"
        try:
            not_after = cert.not_valid_after_utc
        except AttributeError:  # cryptography < 42
            not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
        blocks.append(
            {
                "ok": True,
                "subject": subject,
                "not_after": not_after,
                "expired": not_after < datetime.now(timezone.utc),
                "is_ca": _is_ca_cert(cert),
            }
        )
    return blocks


def _is_ca_cert(cert: Any) -> bool:
    try:
        from cryptography import x509  # noqa: PLC0415

        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        return bool(constraints.value.ca)
    except Exception:  # noqa: BLE001 — нет расширения или cryptography: не CA
        return False


def _bool_secret(name: str, default: bool) -> bool:
    raw = _secret_value(name)
    if not raw:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "нет"}


def _float_secret(name: str, default: float) -> float:
    try:
        return float(_secret_value(name) or default)
    except (TypeError, ValueError):
        return default


def _int_secret(name: str, default: int) -> int:
    try:
        return int(float(_secret_value(name) or default))
    except (TypeError, ValueError):
        return default


@dataclass
class AIConfig:
    provider: str = PROVIDER_OFF
    api_key: str = ""
    folder_id: str = ""
    model: str = ""
    scope: str = "GIGACHAT_API_PERS"
    verify_ssl: bool = True
    ca_bundle: str = ""
    chat_url: str = GIGACHAT_CHAT_URL
    oauth_url: str = GIGACHAT_OAUTH_URL
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE

    @property
    def title(self) -> str:
        return PROVIDER_TITLES.get(self.provider, self.provider)

    @property
    def tls_verify(self):
        """Что передать в `verify=` запроса.

        Путь к файлу сертификата — если он задан или найден в репозитории;
        иначе обычная системная проверка. `False` возвращается только тогда,
        когда администратор явно выключил проверку.
        """
        if not self.verify_ssl:
            return False
        return self.ca_bundle or True

    @property
    def is_ready(self) -> bool:
        return not self.problem

    @property
    def problem(self) -> str:
        """Пустая строка, если можно генерировать; иначе — что именно не так."""
        if self.provider == PROVIDER_OFF:
            return "Генерация выключена: не задан AI_PROVIDER."
        if self.provider == PROVIDER_YANDEX:
            if not self.api_key:
                return "Не задан YANDEX_API_KEY."
            if not self.folder_id:
                return "Не задан YANDEX_FOLDER_ID."
            return ""
        if self.provider == PROVIDER_GIGACHAT:
            if not self.api_key:
                return "Не задан GIGACHAT_AUTH_KEY."
            return ""
        return f"Неизвестный провайдер: {self.provider}."


def load_ai_config() -> AIConfig:
    """Собрать настройки генерации из секретов приложения."""
    provider = (_secret_value("AI_PROVIDER") or PROVIDER_OFF).strip().lower()
    if provider in {"yandexgpt", "yandex_gpt", "ya"}:
        provider = PROVIDER_YANDEX
    if provider in {"sber", "giga"}:
        provider = PROVIDER_GIGACHAT
    if provider not in {PROVIDER_OFF, PROVIDER_YANDEX, PROVIDER_GIGACHAT}:
        provider = PROVIDER_OFF

    if provider == PROVIDER_YANDEX:
        model = _secret_value("AI_MODEL") or "yandexgpt/latest"
        api_key = _secret_value("YANDEX_API_KEY", "AI_API_KEY")
    elif provider == PROVIDER_GIGACHAT:
        model = _secret_value("AI_MODEL") or "GigaChat"
        api_key = _secret_value("GIGACHAT_AUTH_KEY", "AI_API_KEY")
    else:
        model = _secret_value("AI_MODEL")
        api_key = _secret_value("AI_API_KEY")

    return AIConfig(
        provider=provider,
        api_key=api_key,
        folder_id=_secret_value("YANDEX_FOLDER_ID"),
        model=model,
        scope=_secret_value("GIGACHAT_SCOPE") or "GIGACHAT_API_PERS",
        # У GigaChat цепочка сертификатов подписана НУЦ Минцифры. Если корневой
        # сертификат не установлен в системе, проверку приходится отключать —
        # это осознанный выбор администратора, а не поведение по умолчанию.
        verify_ssl=_bool_secret("GIGACHAT_VERIFY_SSL", True),
        # Три способа задать сертификат, по убыванию удобства:
        # текстом в секретах (без терминала), путём к файлу, файлом в репозитории.
        ca_bundle=(
            ca_pem_to_file(_secret_value("GIGACHAT_CA_PEM"))
            or _secret_value("GIGACHAT_CA_BUNDLE")
            or bundled_ca_path()
        ),
        # Адреса вынесены в настройки: когда Сбер снова их поменяет, это правка
        # одной строки в секретах, а не выпуск новой версии платформы.
        chat_url=_secret_value("GIGACHAT_API_URL") or GIGACHAT_CHAT_URL,
        oauth_url=_secret_value("GIGACHAT_OAUTH_URL") or GIGACHAT_OAUTH_URL,
        timeout=_float_secret("AI_TIMEOUT", DEFAULT_TIMEOUT),
        max_tokens=_int_secret("AI_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        temperature=_float_secret("AI_TEMPERATURE", DEFAULT_TEMPERATURE),
    )


def _requests():
    try:
        import requests  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        raise AIError(
            "Не установлена библиотека requests — добавьте её в requirements.txt."
        ) from exc
    return requests


def is_tls_trust_error(message: str) -> bool:
    """Похоже ли это на «не доверяю сертификату», а не на обрыв сети."""
    text = str(message or "").lower()
    markers = (
        "certificate_verify_failed",
        "certificate verify failed",
        "self-signed certificate",
        "self signed certificate",
        "unable to get local issuer",
        "sslcertverificationerror",
    )
    return any(marker in text for marker in markers)


def tls_trust_hint(config: "AIConfig") -> str:
    """Что делать с ошибкой доверия сертификату GigaChat.

    Сообщение должно закрывать вопрос целиком: человек видит его вместо
    трассировки и не должен идти гуглить, что такое НУЦ Минцифры.
    """
    if config.provider != PROVIDER_GIGACHAT:
        return (
            "Сертификат сервера не прошёл проверку. Если между приложением и "
            "интернетом стоит корпоративный прокси, добавьте его корневой "
            "сертификат в GIGACHAT_CA_BUNDLE."
        )
    if config.ca_bundle:
        return (
            f"GigaChat не принял сертификат из файла {config.ca_bundle}. "
            "Проверьте, что в файле лежат оба сертификата НУЦ Минцифры — "
            "корневой и выпускающий, — и что файл не повреждён."
        )
    return (
        "GigaChat подписан сертификатом НУЦ Минцифры, которого нет в системном "
        "хранилище — поэтому проверка не прошла.\n\n"
        "**Без терминала:** раскройте блок «Сертификат для GigaChat: собрать без "
        "терминала» чуть выше, скачайте с gosuslugi.ru/crt два файла в формате "
        ".crt (корневой и выпускающий), загрузите их туда — платформа проверит "
        "их и выдаст готовый блок для Streamlit Secrets.\n\n"
        "**Если есть терминал:** положите сертификат в папку certs/ в корне "
        "репозитория, платформа подхватит его сама:\n\n"
        f"    {CA_DOWNLOAD_HINT}\n\n"
        "Путь к уже имеющемуся файлу задаётся секретом GIGACHAT_CA_BUNDLE. "
        "Отключать проверку (GIGACHAT_VERIFY_SSL = \"false\") стоит только как "
        "временную меру."
    )


# Провайдеры называют превышение лимита по-разному и меняют формулировки без
# предупреждения, поэтому ищем по набору признаков, а не по одной фразе.
# YandexGPT: «Number of input tokens must be no more than 8192», шлюзы в духе
# OpenAI: «maximum context length», GigaChat: код 413 или русский текст.
_CONTEXT_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "input tokens",
    "too many tokens",
    "token limit",
    "tokens limit",
    "too long",
    "too large",
    "слишком много токенов",
    "лимит токенов",
    "длина контекста",
    "длину контекста",
    "слишком длинн",
)
# Кончились деньги или пакет токенов: повтор не поможет, пока владелец не
# пополнит баланс.
_QUOTA_MARKERS = (
    "quota",
    "квот",
    "payment required",
    "insufficient",
    "billing",
    "balance",
    "баланс",
    "недостаточно средств",
    "закончил",
    "исчерпан",
)
# Лимит частоты Yandex Cloud тоже зовёт квотой («...requestCount.rate»), но он
# снимается сам через минуту — такой ответ не должен звать пополнять баланс.
_RATE_MARKERS = (
    ".rate",
    "rate limit",
    "rate_limit",
    "requests per",
    "per second",
    "per minute",
    "в секунду",
    "в минуту",
)


# Где пополнять: у YandexGPT это консоль облака, а не отдельный кабинет модели.
_BILLING_PLACES = {
    PROVIDER_YANDEX: "в консоли Yandex Cloud",
    PROVIDER_GIGACHAT: "в личном кабинете GigaChat",
}


def _error_kind(status: int, body: str) -> str:
    """Что случилось по сути: не влез запрос, кончилась квота или слишком часто."""
    text = str(body or "").lower()
    if 400 <= status < 500:
        rate_like = any(marker in text for marker in _RATE_MARKERS)
        if status == 402 or (
            not rate_like and any(marker in text for marker in _QUOTA_MARKERS)
        ):
            return ERROR_QUOTA
    if status == 413 or (
        status in (400, 422) and any(marker in text for marker in _CONTEXT_MARKERS)
    ):
        return ERROR_CONTEXT
    if status == 429:
        return ERROR_RATE
    return ""


def _http_error(status: int, body: str, provider: str) -> AIError:
    """Ошибка HTTP от провайдера — в виде, понятном человеку без JSON."""
    title = PROVIDER_TITLES.get(provider, provider)
    text = (body or "").strip()[:400]
    kind = _error_kind(status, body)
    # Про лимиты сырой ответ не нужен: человеку надо знать, что делать, а не
    # что написал сервис. Ответ уходит в detail — владелец увидит его отдельно.
    if kind == ERROR_CONTEXT:
        return AIError(
            f"{title}: запрос не поместился в лимит модели. Снимите галочку "
            "«Отправлять выдержки сообщений» или сократите дополнительные "
            "указания и запустите ещё раз.",
            kind=kind,
            detail=text,
        )
    if kind == ERROR_QUOTA:
        place = _BILLING_PLACES.get(provider, f"в кабинете {title}")
        return AIError(
            f"{title}: исчерпана квота — оплаченные запросы к модели "
            "закончились. Повторный запуск не поможет: владельцу платформы "
            f"нужно пополнить баланс или увеличить квоту {place}.",
            kind=kind,
            detail=text,
        )
    if kind == ERROR_RATE:
        return AIError(
            f"{title}: слишком много запросов подряд, сработал лимит частоты. "
            "Подождите минуту и запустите ещё раз.",
            kind=kind,
            detail=text,
        )
    if status in (401, 403):
        return AIError(
            f"{title} отклонил ключ доступа (код {status}). Проверьте ключ и "
            f"права сервисного аккаунта. {text}"
        )
    if status >= 500:
        return AIError(f"{title} временно недоступен (код {status}). {text}")
    return AIError(f"{title}: ошибка {status}. {text}")


@dataclass
class _TokenCache:
    value: str = ""
    expires_at: float = 0.0
    lock: Any = field(default_factory=threading.Lock)


_GIGACHAT_TOKEN = _TokenCache()


def _gigachat_token(config: AIConfig, session: Any) -> str:
    """Токен GigaChat живёт ~30 минут, поэтому кэшируем его в процессе."""
    now = time.time()
    with _GIGACHAT_TOKEN.lock:
        if _GIGACHAT_TOKEN.value and _GIGACHAT_TOKEN.expires_at - 60 > now:
            return _GIGACHAT_TOKEN.value
        response = session.post(
            config.oauth_url,
            headers={
                "Authorization": f"Basic {config.api_key}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"scope": config.scope},
            timeout=config.timeout,
            verify=config.tls_verify,
        )
        if response.status_code >= 400:
            raise _http_error(response.status_code, response.text, PROVIDER_GIGACHAT)
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise AIError("GigaChat не вернул access_token.")
        # expires_at приходит в миллисекундах.
        expires_at = float(payload.get("expires_at") or 0) / 1000.0
        _GIGACHAT_TOKEN.value = token
        _GIGACHAT_TOKEN.expires_at = expires_at or (now + 25 * 60)
        return token


def reset_gigachat_token() -> None:
    """Сбросить кэш токена — нужно после смены ключа в настройках."""
    with _GIGACHAT_TOKEN.lock:
        _GIGACHAT_TOKEN.value = ""
        _GIGACHAT_TOKEN.expires_at = 0.0


def _complete_yandex(config: AIConfig, system: str, user: str, session: Any) -> str:
    model_uri = config.model
    if not model_uri.startswith("gpt://") and not model_uri.startswith("ds://"):
        model_uri = f"gpt://{config.folder_id}/{model_uri}"
    response = session.post(
        YANDEX_URL,
        headers={
            "Authorization": f"Api-Key {config.api_key}",
            "x-folder-id": config.folder_id,
            "Content-Type": "application/json",
        },
        json={
            "modelUri": model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": config.temperature,
                "maxTokens": str(config.max_tokens),
            },
            "messages": [
                {"role": "system", "text": system},
                {"role": "user", "text": user},
            ],
        },
        timeout=config.timeout,
    )
    if response.status_code >= 400:
        raise _http_error(response.status_code, response.text, PROVIDER_YANDEX)
    payload = response.json()
    alternatives = (payload.get("result") or {}).get("alternatives") or []
    if not alternatives:
        raise AIError("YandexGPT вернул пустой ответ.")
    return str((alternatives[0].get("message") or {}).get("text") or "").strip()


def _complete_gigachat(config: AIConfig, system: str, user: str, session: Any) -> str:
    token = _gigachat_token(config, session)
    response = session.post(
        config.chat_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "model": config.model or "GigaChat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": max(0.01, config.temperature),
            "max_tokens": config.max_tokens,
        },
        timeout=config.timeout,
        verify=config.tls_verify,
    )
    if response.status_code in (401, 403):
        # Токен мог протухнуть раньше срока — один повтор с новым токеном.
        reset_gigachat_token()
        token = _gigachat_token(config, session)
        response = session.post(
            config.chat_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "model": config.model or "GigaChat",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": max(0.01, config.temperature),
                "max_tokens": config.max_tokens,
            },
            timeout=config.timeout,
            verify=config.tls_verify,
        )
    if response.status_code >= 400:
        raise _http_error(response.status_code, response.text, PROVIDER_GIGACHAT)
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise AIError("GigaChat вернул пустой ответ.")
    return str((choices[0].get("message") or {}).get("content") or "").strip()


def complete(
    system: str,
    user: str,
    config: AIConfig | None = None,
    *,
    session: Any = None,
) -> str:
    """Отправить промпт выбранному провайдеру и вернуть текст ответа.

    `session` нужен тестам: туда подставляется поддельный транспорт, поэтому
    проверки формата запроса и разбора ответа идут без сети.
    """
    config = config or load_ai_config()
    if not config.is_ready:
        raise AIError(config.problem)

    transport = session
    if transport is None:
        requests = _requests()
        transport = requests.Session()

    try:
        if config.provider == PROVIDER_YANDEX:
            text = _complete_yandex(config, system, user, transport)
        else:
            text = _complete_gigachat(config, system, user, transport)
    except AIError:
        raise
    except json.JSONDecodeError as exc:
        raise AIError(f"Ответ модели не разобран как JSON: {exc}") from exc
    except Exception as exc:
        message = str(exc)
        if is_tls_trust_error(message):
            raise AIError(tls_trust_hint(config)) from exc
        if "timed out" in message.lower() or "timeout" in message.lower():
            raise AIError(
                f"Модель не ответила за {int(config.timeout)} с. "
                "Попробуйте ещё раз или увеличьте AI_TIMEOUT."
            ) from exc
        raise AIError(f"Не удалось обратиться к модели: {message}") from exc

    if not text.strip():
        raise AIError("Модель вернула пустой текст.")
    return text.strip()


def describe_config(config: AIConfig | None = None) -> str:
    """Строка для интерфейса: что настроено, без раскрытия ключа."""
    config = config or load_ai_config()
    if config.provider == PROVIDER_OFF:
        return "Генерация не настроена."
    model = config.model or "модель по умолчанию"
    state = "готово" if config.is_ready else config.problem
    parts = [config.title, model, state]
    if config.provider == PROVIDER_GIGACHAT:
        host = config.chat_url.split("/")[2] if "//" in config.chat_url else config.chat_url
        parts.append(host)
        if config.chat_url == GIGACHAT_CHAT_URL_LEGACY:
            parts.append("устаревший адрес")
        if not config.verify_ssl:
            parts.append("проверка сертификата ВЫКЛЮЧЕНА")
        elif config.ca_bundle:
            parts.append("сертификат НУЦ Минцифры подключён")
    return " · ".join(parts)


def check_connection(config: AIConfig | None = None, *, session: Any = None) -> str:
    """Короткий пробный запрос. Возвращает описание успеха или бросает AIError.

    Нужен, чтобы настройку можно было проверить одной кнопкой, а не выяснять
    работоспособность на первом же реальном саммари.
    """
    config = config or load_ai_config()
    text = complete(
        "Ты отвечаешь одним словом.",
        "Ответь одним словом: работает",
        config,
        session=session,
    )
    return f"{config.title} ответил: {text[:80]}"


# Стоимость запроса полезно понимать до нажатия кнопки. Обе модели считают
# токены примерно одинаково: для русского текста ~2 символа на токен.
def estimate_tokens(text: str) -> int:
    return max(1, int(len(str(text or "")) / 2))
