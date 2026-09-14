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
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

DEFAULT_TIMEOUT = 90.0
DEFAULT_MAX_TOKENS = 1800
DEFAULT_TEMPERATURE = 0.3


class AIError(Exception):
    """Ошибка генерации, которую можно показать пользователю как есть."""


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
            except Exception:
                pass
            try:
                if "ai" in st.secrets:
                    section_key = name.lower()
                    for prefix in ("ai_", "yandex_", "gigachat_"):
                        section_key = section_key.replace(prefix, "", 1)
                    if section_key in st.secrets["ai"]:
                        return str(st.secrets["ai"][section_key]).strip()
            except Exception:
                pass
        value = os.getenv(name)
        if value:
            return str(value).strip()
    return ""


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
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE

    @property
    def title(self) -> str:
        return PROVIDER_TITLES.get(self.provider, self.provider)

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


def _readable_http_error(status: int, body: str, provider: str) -> str:
    text = (body or "").strip()[:400]
    if status in (401, 403):
        return (
            f"{PROVIDER_TITLES.get(provider, provider)} отклонил ключ доступа "
            f"(код {status}). Проверьте ключ и права сервисного аккаунта. {text}"
        )
    if status == 429:
        return (
            f"{PROVIDER_TITLES.get(provider, provider)}: превышен лимит запросов "
            f"(код 429). Попробуйте через минуту. {text}"
        )
    if status >= 500:
        return (
            f"{PROVIDER_TITLES.get(provider, provider)} временно недоступен "
            f"(код {status}). {text}"
        )
    return f"{PROVIDER_TITLES.get(provider, provider)}: ошибка {status}. {text}"


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
            GIGACHAT_OAUTH_URL,
            headers={
                "Authorization": f"Basic {config.api_key}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"scope": config.scope},
            timeout=config.timeout,
            verify=config.verify_ssl,
        )
        if response.status_code >= 400:
            raise AIError(
                _readable_http_error(
                    response.status_code, response.text, PROVIDER_GIGACHAT
                )
            )
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
        raise AIError(
            _readable_http_error(response.status_code, response.text, PROVIDER_YANDEX)
        )
    payload = response.json()
    alternatives = (payload.get("result") or {}).get("alternatives") or []
    if not alternatives:
        raise AIError("YandexGPT вернул пустой ответ.")
    return str((alternatives[0].get("message") or {}).get("text") or "").strip()


def _complete_gigachat(config: AIConfig, system: str, user: str, session: Any) -> str:
    token = _gigachat_token(config, session)
    response = session.post(
        GIGACHAT_CHAT_URL,
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
        verify=config.verify_ssl,
    )
    if response.status_code in (401, 403):
        # Токен мог протухнуть раньше срока — один повтор с новым токеном.
        reset_gigachat_token()
        token = _gigachat_token(config, session)
        response = session.post(
            GIGACHAT_CHAT_URL,
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
            verify=config.verify_ssl,
        )
    if response.status_code >= 400:
        raise AIError(
            _readable_http_error(response.status_code, response.text, PROVIDER_GIGACHAT)
        )
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
    return f"{config.title} · {model} · {state}"


# Стоимость запроса полезно понимать до нажатия кнопки. Обе модели считают
# токены примерно одинаково: для русского текста ~2 символа на токен.
def estimate_tokens(text: str) -> int:
    return max(1, int(len(str(text or "")) / 2))
