# -*- coding: utf-8 -*-
"""Доставка ошибок владельцу платформы.

Граница отказа прячет трейсбек от заказчика — и до сих пор прятала его и от
владельца: узнать, что раздел падает, можно было только из логов контейнера
или из жалобы клиента. Этот модуль отправляет событие наружу, если настроен
хотя бы один канал:

- SENTRY_DSN — событие уходит в Sentry. Пакет sentry-sdk в зависимости не
  входит: кто пользуется Sentry, ставит его сам; без пакета канал молча
  пропускается.
- ALERT_WEBHOOK_URL — POST с JSON в любой приёмник: свой endpoint,
  Slack/Telegram через промежуточный сервис.

Без настройки остаётся то, что было, — логирование. Ошибки доставки никогда
не поднимаются: наблюдаемость не должна ронять то, за чем наблюдает.

Повторы гасятся: Streamlit перерисовывает страницу на каждое нажатие, и
сломанный раздел без глушения слал бы одно и то же событие десятками в
минуту. Одна пара «место + класс ошибки» уходит не чаще раза в 10 минут на
процесс.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from typing import Any

import requests

LOGGER = logging.getLogger("platform.observability")

# Пара (место, класс ошибки) → монотонное время последней отправки.
_LAST_SENT: dict[tuple[str, str], float] = {}
THROTTLE_SECONDS = 600

# Ограничения полей: вебхук-приёмники и Sentry не обязаны любить мегабайтные
# трейсбеки, а для сигнала «раздел падает» хватает хвоста.
MAX_TRACEBACK_CHARS = 4000

_sentry_ready: bool | None = None


def _secret(name: str) -> str:
    """Значение из секретов Streamlit или окружения.

    st.secrets поднимает исключение, когда файла секретов нет вовсе (локальный
    запуск, тесты), — это не повод падать.
    """
    try:
        import streamlit as st

        value = st.secrets.get(name)  # type: ignore[attr-defined]
        if value:
            return str(value)
    except Exception:  # noqa: BLE001 — секретов может не быть
        pass
    return str(os.environ.get(name) or "").strip()


def _post(url: str, payload: dict[str, Any]) -> None:
    # Вынесено в отдельную функцию, чтобы тесты подменяли доставку, не
    # поднимая настоящий HTTP.
    requests.post(url, json=payload, timeout=5)


def reset_throttle() -> None:
    """Забыть, что уже отправлялось, — для тестов."""
    _LAST_SENT.clear()


def _throttled(where: str, exc: BaseException | None) -> bool:
    signature = (str(where), type(exc).__name__ if exc else "")
    now = time.monotonic()
    last = _LAST_SENT.get(signature)
    if last is not None and now - last < THROTTLE_SECONDS:
        return True
    _LAST_SENT[signature] = now
    return False


def _send_sentry(exc: BaseException | None, where: str) -> bool:
    dsn = _secret("SENTRY_DSN")
    if not dsn:
        return False
    global _sentry_ready
    try:
        import sentry_sdk
    except Exception:  # noqa: BLE001 — пакет не установлен, канал пропускается
        return False
    try:
        if _sentry_ready is None:
            sentry_sdk.init(dsn=dsn, traces_sample_rate=0)
            _sentry_ready = True
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("where", where)
            if exc is not None:
                sentry_sdk.capture_exception(exc)
            else:
                sentry_sdk.capture_message(where)
        return True
    except Exception:  # noqa: BLE001 — доставка не должна ронять страницу
        LOGGER.warning("Не удалось отправить событие в Sentry")
        return False


def _send_webhook(exc: BaseException | None, where: str, context: dict[str, Any]) -> bool:
    url = _secret("ALERT_WEBHOOK_URL")
    if not url:
        return False
    payload: dict[str, Any] = {
        "source": "media-digest-platform",
        "where": where,
        "error": f"{type(exc).__name__}: {exc}" if exc is not None else "",
        "context": {k: str(v) for k, v in (context or {}).items()},
    }
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        payload["traceback"] = tb[-MAX_TRACEBACK_CHARS:]
    try:
        _post(url, payload)
        return True
    except Exception:  # noqa: BLE001 — доставка не должна ронять страницу
        LOGGER.warning("Не удалось отправить событие на вебхук")
        return False


def report_failure(
    where: str, exc: BaseException | None = None, **context: Any
) -> bool:
    """Сообщить владельцу об отказе; вернуть, ушло ли хоть в один канал.

    Локальный лог пишется всегда — он был и остаётся; каналы добавляются
    поверх. Функция не бросает исключений ни при какой конфигурации.
    """
    try:
        if exc is not None:
            LOGGER.error("Сбой: %s — %s: %s", where, type(exc).__name__, exc)
        else:
            LOGGER.error("Сбой: %s", where)
        if _throttled(where, exc):
            return False
        sent_sentry = _send_sentry(exc, where)
        sent_webhook = _send_webhook(exc, where, context)
        return bool(sent_sentry or sent_webhook)
    except Exception:  # noqa: BLE001 — наблюдаемость не роняет наблюдаемое
        return False
