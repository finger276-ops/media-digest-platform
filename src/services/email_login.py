# -*- coding: utf-8 -*-
"""Вход по email: одноразовый код из письма.

Код выдаёт и проверяет Supabase Auth, письмо уходит через SMTP, настроенный
в панели Supabase (docs/AUTH.md). Платформа решает три вещи, которых Supabase
не делает сама.

1. Кому вообще слать код. Только адресам из списка доступа проекта или из
   PLATFORM_OWNER_EMAILS. На чужой адрес письмо не уходит, но ответ на экране
   тот же, что и для своего: по нему нельзя перебором узнать, чьи адреса есть
   в платформе.
2. Сколько раз можно ошибиться. У Supabase нет счётчика неверных попыток на
   один код, а все запросы приходят с одного адреса сервера Streamlit —
   его общие лимиты злоумышленник выберет за всех. Поэтому пять неверных
   попыток закрывают код, и нужен новый.
3. Как часто слать письма: не чаще раза в минуту и не больше пяти в час на
   адрес.

Токены Supabase после проверки кода не хранятся: платформе нужен только
факт, что человек владеет адресом. Дальше вход живёт в сессии Streamlit и,
по желанию, в запомненном входе (services/login_sessions.py).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import platform_store as store
from services.observability import report_failure

LOGGER = logging.getLogger("platform.email_login")

RESEND_COOLDOWN_SECONDS = 60
SENDS_PER_HOUR = 5
MAX_FAILED_ATTEMPTS = 5
# Длина кода настраивается в Supabase от 6 до 10 цифр, и у новых проектов
# по умолчанию бывает 8. Жёсткие 6 цифр отвергли бы настоящий код.
CODE_MIN_DIGITS = 6
CODE_MAX_DIGITS = 10

SENT_MESSAGE = (
    "Если этот адрес есть в списке доступа, на него отправлен код. Письмо "
    "приходит в течение минуты — загляните и в «Спам»."
)
# Supabase отвечает одной и той же ошибкой (otp_expired, «Token has expired
# or is invalid») и на неверный, и на просроченный код — различить их нельзя,
# поэтому и сообщение одно.
WRONG_CODE_MESSAGE = (
    "Код не подошёл или его срок истёк. Проверьте цифры или запросите новый код."
)
LOCKED_MESSAGE = (
    "Слишком много неверных попыток. Запросите новый код — старый больше "
    "не примется."
)
SEND_FAILED_MESSAGE = (
    "Не удалось отправить письмо. Попробуйте через несколько минут; если не "
    "поможет — напишите владельцу платформы."
)
MAIL_LIMIT_MESSAGE = (
    "Почта платформы сейчас упёрлась в лимит отправки. Попробуйте через "
    "несколько минут."
)

_TRUE = {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class LoginStep:
    ok: bool
    message: str
    email: str = ""


# --- настройки ---------------------------------------------------------------


def email_login_enabled() -> bool:
    """Показывать ли вход по email. Выключен, пока владелец не настроит почту.

    Отдельный выключатель, а не «есть ли таблицы»: без SMTP в Supabase письма
    не дойдут никому, кроме участников команды Supabase, а узнать это из
    приложения нельзя.
    """
    return store._secret_value("EMAIL_LOGIN_ENABLED").strip().lower() in _TRUE


def owner_emails() -> set[str]:
    """Адреса владельцев платформы из PLATFORM_OWNER_EMAILS.

    Строка через запятую, точку с запятой или пробел. Массив TOML здесь не
    подходит: секреты читаются строкой.
    """
    raw = store._secret_value("PLATFORM_OWNER_EMAILS")
    out = set()
    for part in re.split(r"[\s,;]+", raw):
        email = store.normalize_email(part)
        if email:
            out.add(email)
    return out


def is_owner_email(email: str) -> bool:
    email = store.normalize_email(email)
    return bool(email) and email in owner_emails()


def login_allowed(email: str) -> bool:
    """Можно ли этому адресу войти: владелец или есть действующий доступ."""
    email = store.normalize_email(email)
    if not email:
        return False
    return is_owner_email(email) or store.email_has_access(email)


def clean_code(value: Any) -> str:
    """Цифры кода без пробелов и дефисов: из письма код часто копируют с ними."""
    return re.sub(r"[\s\-]", "", str(value or ""))


# --- ограничители --------------------------------------------------------------


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _throttle_row(email: str) -> dict[str, Any]:
    rows = (
        store.get_supabase_client()
        .table("platform_auth_throttle")
        .select("*")
        .eq("email_hash", _email_hash(email))
        .limit(1)
        .execute()
        .data
        or []
    )
    return dict(rows[0]) if rows else {}


def _save_throttle(email: str, **fields: Any) -> None:
    payload = {"email_hash": _email_hash(email), "updated_at": _now().isoformat()}
    payload.update(fields)
    store.get_supabase_client().table("platform_auth_throttle").upsert(
        payload, on_conflict="email_hash"
    ).execute()


def _send_blocked(row: dict[str, Any], now: datetime) -> str | None:
    """Почему сейчас нельзя отправить код; None — можно."""
    last_sent = _parse_ts(row.get("last_sent_at"))
    if last_sent and (now - last_sent).total_seconds() < RESEND_COOLDOWN_SECONDS:
        wait = RESEND_COOLDOWN_SECONDS - int((now - last_sent).total_seconds())
        return f"Новый код можно запросить через {max(wait, 1)} с."
    window_start = _parse_ts(row.get("window_started_at"))
    in_window = window_start and now - window_start < timedelta(hours=1)
    if in_window and int(row.get("sends_in_window") or 0) >= SENDS_PER_HOUR:
        return (
            "Код на этот адрес уже отправляли несколько раз за последний час. "
            "Попробуйте позже."
        )
    return None


# --- Supabase Auth -------------------------------------------------------------


def _auth_client():
    """Отдельный клиент на каждый вызов, без сохранения сессии и автообновления.

    Клиент supabase-py после входа переписывает свой заголовок Authorization
    на токен пользователя. Общий клиент приложения (get_supabase_client) от
    этого стал бы клиентом чужого человека, поэтому для Auth — свой,
    одноразовый. Без автообновления: иначе каждый вход оставлял бы в
    процессе таймер обновления токена, который никому не нужен.
    """
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    url = store.normalize_supabase_url(store._secret_value("SUPABASE_URL"))
    key = store._secret_value(
        "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY"
    )
    if not url or not key:
        raise RuntimeError("Не заданы SUPABASE_URL и ключ Supabase в secrets.")
    return create_client(
        url,
        key,
        options=SyncClientOptions(persist_session=False, auto_refresh_token=False),
    )


def _error_code(exc: Exception) -> str:
    return str(getattr(exc, "code", "") or "").strip().lower()


def _describe(exc: Exception) -> str:
    """Тип, код и статус ошибки — без текста и без стека.

    Текст ошибки Supabase может содержать адрес, а Sentry по умолчанию
    снимает локальные переменные кадров, среди которых адрес точно есть.
    Поэтому владельцу уходит только то, что описывает сбой, а не человека.
    """
    parts = [type(exc).__name__]
    code = _error_code(exc)
    if code:
        parts.append(code)
    status = getattr(exc, "status", None)
    if status:
        parts.append(str(status))
    return " ".join(parts)


def _ensure_auth_user(client, email: str) -> None:
    """Завести пользователя Supabase Auth, если его ещё нет.

    Код отправляется с should_create_user=False: так Supabase шлёт шаблон
    «Magic Link» (с кодом), а не «Confirm signup», и не заводит учётки
    сам. Поэтому пользователя заводит платформа — уже подтверждённым, через
    служебный ключ. Регистрацию в самом Supabase можно выключить.
    """
    try:
        client.auth.admin.create_user({"email": email, "email_confirm": True})
    except Exception as exc:  # noqa: BLE001 — «уже есть» разбирается ниже
        code = _error_code(exc)
        message = str(getattr(exc, "message", "") or exc).lower()
        if code in {"email_exists", "user_already_exists"} or "already" in message:
            return
        raise


def request_login_code(email: str) -> LoginStep:
    """Отправить код на адрес. Ответ одинаков для своего и чужого адреса."""
    email = store.normalize_email(email)
    if not email:
        return LoginStep(False, "Укажите адрес целиком, например ivan@company.ru.")
    now = _now()
    row = _throttle_row(email)
    blocked = _send_blocked(row, now)
    if blocked:
        return LoginStep(False, blocked, email)

    window_start = _parse_ts(row.get("window_started_at"))
    if not window_start or now - window_start >= timedelta(hours=1):
        window_start, sends = now, 0
    else:
        sends = int(row.get("sends_in_window") or 0)

    if login_allowed(email):
        try:
            client = _auth_client()
            _ensure_auth_user(client, email)
            client.auth.sign_in_with_otp(
                {"email": email, "options": {"should_create_user": False}}
            )
        except Exception as exc:  # noqa: BLE001 — любая ошибка отправки — отказ
            code = _error_code(exc)
            if code in {"over_email_send_rate_limit", "over_request_rate_limit"}:
                return LoginStep(False, MAIL_LIMIT_MESSAGE, email)
            # Адрес в сбой не попадает: уведомление уходит во внешний
            # вебхук или Sentry, а это персональные данные.
            report_failure(f"вход по email: не удалось отправить код ({_describe(exc)})")
            return LoginStep(False, SEND_FAILED_MESSAGE, email)

    # Счётчик ведётся и для чужих адресов: иначе повторный запрос выдавал бы,
    # есть адрес в платформе или нет («подождите минуту» против «отправлено»).
    _save_throttle(
        email,
        last_sent_at=now.isoformat(),
        window_started_at=window_start.isoformat(),
        sends_in_window=sends + 1,
        failed_attempts=0,
    )
    return LoginStep(True, SENT_MESSAGE, email)


def verify_login_code(email: str, code: str) -> LoginStep:
    """Проверить код. ok=True — адрес подтверждён и ему можно войти."""
    email = store.normalize_email(email)
    code = clean_code(code)
    if not email:
        return LoginStep(False, "Укажите адрес целиком, например ivan@company.ru.")
    if not code.isdigit() or not CODE_MIN_DIGITS <= len(code) <= CODE_MAX_DIGITS:
        return LoginStep(
            False,
            f"Код — это {CODE_MIN_DIGITS}–{CODE_MAX_DIGITS} цифр из письма.",
            email,
        )
    row = _throttle_row(email)
    failed = int(row.get("failed_attempts") or 0)
    if failed >= MAX_FAILED_ATTEMPTS:
        return LoginStep(False, LOCKED_MESSAGE, email)

    def _fail(message: str) -> LoginStep:
        _save_throttle(email, failed_attempts=failed + 1)
        if failed + 1 >= MAX_FAILED_ATTEMPTS:
            return LoginStep(False, LOCKED_MESSAGE, email)
        return LoginStep(False, message, email)

    if not login_allowed(email):
        # Кода на такой адрес не отправляли. Ответ тот же, что и на неверный
        # код, а в Supabase запрос не уходит.
        return _fail(WRONG_CODE_MESSAGE)

    try:
        response = _auth_client().auth.verify_otp(
            {"email": email, "token": code, "type": "email"}
        )
    except Exception as exc:  # noqa: BLE001 — разбирается по коду ошибки
        code_name = _error_code(exc)
        if code_name in {"over_request_rate_limit", "over_email_send_rate_limit"}:
            return LoginStep(False, MAIL_LIMIT_MESSAGE, email)
        if getattr(exc, "status", None) is None and not code_name:
            # Не ответ Supabase, а сбой сети или настройки: попытку не
            # списываем, владелец узнаёт о сбое.
            report_failure(f"вход по email: не удалось проверить код ({_describe(exc)})")
            return LoginStep(False, SEND_FAILED_MESSAGE, email)
        return _fail(WRONG_CODE_MESSAGE)

    user = getattr(response, "user", None)
    confirmed = store.normalize_email(getattr(user, "email", "") if user else "")
    if confirmed != email:
        return _fail(WRONG_CODE_MESSAGE)
    _save_throttle(email, failed_attempts=0)
    return LoginStep(True, "", email)
