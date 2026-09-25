# -*- coding: utf-8 -*-
"""Запомненный вход: человек не вводит код из письма после F5 и деплоя.

Сессия Streamlit живёт, пока открыта вкладка: обновление страницы, новая
вкладка и каждый деплой начинают её заново. Без запомненного входа каждое
такое событие стоило бы нового письма с кодом.

В cookie браузера лежит случайный ключ, а не токены Supabase. В базе — только
его sha256, адрес и срок. Ключ живёт SESSION_TTL_DAYS дней с момента входа и
перестаёт работать сразу после «Выйти»: запись отзывается на сервере, даже
если браузер не успел стереть cookie.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import platform_store as store

SESSION_TTL_DAYS = 14
COOKIE_NAME = "mdp_login"
TABLE = "platform_auth_sessions"


def _hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


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


def create_session(email: str) -> str:
    """Запомнить вход адреса. Возвращает ключ для cookie — он нигде не хранится."""
    email = store.normalize_email(email)
    if not email:
        raise ValueError("Нельзя запомнить вход без адреса.")
    token = secrets.token_urlsafe(32)
    now = _now()
    client = store.get_supabase_client()
    client.table(TABLE).insert(
        {
            "token_hash": _hash(token),
            "user_email": email,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=SESSION_TTL_DAYS)).isoformat(),
            "last_seen_at": now.isoformat(),
        }
    ).execute()
    # Просроченные записи чистятся заодно: отдельного расписания у
    # платформы нет, а входы случаются достаточно часто.
    try:
        client.table(TABLE).delete().lt("expires_at", now.isoformat()).execute()
    except Exception:  # noqa: BLE001 — уборка не должна мешать входу
        pass
    return token


def resolve_session(token: str) -> str:
    """Адрес по ключу из cookie; пустая строка — ключ не действует."""
    token = str(token or "").strip()
    if not token:
        return ""
    rows = (
        store.get_supabase_client()
        .table(TABLE)
        .select("*")
        .eq("token_hash", _hash(token))
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return ""
    row = rows[0]
    if row.get("revoked_at"):
        return ""
    expires = _parse_ts(row.get("expires_at"))
    if not expires or expires <= _now():
        return ""
    return store.normalize_email(row.get("user_email"))


def revoke_session(token: str) -> None:
    """Отозвать ключ: после этого cookie с ним больше не впускает."""
    token = str(token or "").strip()
    if not token:
        return
    store.get_supabase_client().table(TABLE).update(
        {"revoked_at": _now().isoformat()}
    ).eq("token_hash", _hash(token)).execute()
