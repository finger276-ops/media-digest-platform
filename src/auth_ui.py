# -*- coding: utf-8 -*-
"""Вход по email в боковой панели: код из письма и запомненный вход.

Порядок на каждой перерисовке (src/app.py, main):

1. restore_login_from_cookie — если в браузере есть ключ запомненного входа,
   адрес восстанавливается без письма. Один раз за сессию Streamlit.
2. render_account_panel — «Вы вошли: адрес» с кнопкой «Выйти», или форма
   входа, если в сессии никого нет.
3. is_platform_admin / render_project_access (app.py, project_admin_ui.py) —
   владелец по адресу из PLATFORM_OWNER_EMAILS, остальные — по списку
   доступа проекта.
4. flush_login_cookie — записать или стереть cookie в браузере.

Контракт сессии тот же, что у входа по коду: platform_project_id и
platform_project_role ставятся по выбранному проекту, platform_is_admin — у
владельца. Поэтому разделы платформы о способе входа ничего не знают.
"""

from __future__ import annotations

import json

import streamlit as st

from services.email_login import (
    email_login_enabled,
    is_owner_email,
    login_allowed,
    request_login_code,
    verify_login_code,
)
from services.login_sessions import (
    COOKIE_NAME,
    SESSION_TTL_DAYS,
    create_session,
    resolve_session,
    revoke_session,
)
from services.observability import report_failure

USER_EMAIL_KEY = "platform_user_email"
LOGIN_TOKEN_KEY = "platform_login_token"
# Откуда у сессии режим владельца: "email" или "password". Нужен, чтобы
# снять режим, когда адрес убрали из PLATFORM_OWNER_EMAILS, и не трогать
# при этом вход по паролю.
ADMIN_VIA_KEY = "platform_admin_via"
PENDING_EMAIL_KEY = "login_pending_email"
NOTICE_KEY = "login_notice"
COOKIE_OP_KEY = "_login_cookie_op"
COOKIE_CHECKED_KEY = "_login_cookie_checked"

NOT_READY_MESSAGE = (
    "Вход по email пока не готов: владельцу платформы нужно применить "
    "миграцию 0007 (см. docs/AUTH.md)."
)


def current_email() -> str:
    return str(st.session_state.get(USER_EMAIL_KEY) or "")


def read_login_cookie() -> str:
    """Ключ запомненного входа из cookie первого запроса вкладки."""
    try:
        return str(st.context.cookies.get(COOKIE_NAME) or "")
    except Exception:  # noqa: BLE001 — нет контекста запроса (тесты, скрипты)
        return ""


def restore_login_from_cookie() -> None:
    """Восстановить вход по ключу из cookie — один раз за сессию Streamlit."""
    if st.session_state.get(COOKIE_CHECKED_KEY):
        return
    st.session_state[COOKIE_CHECKED_KEY] = True
    if not email_login_enabled() or current_email():
        return
    token = read_login_cookie()
    if not token:
        return
    try:
        email = resolve_session(token)
        allowed = bool(email) and login_allowed(email)
    except Exception as exc:  # noqa: BLE001 — без таблицы входа страница работает
        report_failure("вход по email: не удалось проверить запомненный вход", exc)
        return
    if not allowed:
        # Ключ отозван, просрочен или у адреса больше нет доступа: cookie
        # больше не нужен, а вход идёт обычным путём.
        st.session_state[COOKIE_OP_KEY] = ("clear", "")
        return
    st.session_state[USER_EMAIL_KEY] = email
    st.session_state[LOGIN_TOKEN_KEY] = token


def _complete_login(email: str) -> None:
    """Код подтверждён: запомнить адрес в сессии и в браузере."""
    st.session_state[USER_EMAIL_KEY] = email
    # Проект, открытый до этого кодом, не наследуется: доступ по email
    # решает список доступа, а не то, что было в сессии раньше.
    st.session_state.pop("platform_project_id", None)
    st.session_state.pop("platform_project_role", None)
    st.session_state.pop(PENDING_EMAIL_KEY, None)
    st.session_state.pop(NOTICE_KEY, None)
    try:
        token = create_session(email)
    except Exception as exc:  # noqa: BLE001 — не запомнили, но вход состоялся
        report_failure("вход по email: не удалось запомнить вход", exc)
    else:
        st.session_state[LOGIN_TOKEN_KEY] = token
        st.session_state[COOKIE_OP_KEY] = ("set", token)
    st.rerun()


def logout() -> None:
    """Выйти: отозвать запомненный вход и начать сессию с чистого листа.

    Сессия очищается целиком, а не по списку ключей: в ней лежат черновики
    правок, выбранные периоды и тексты ИИ прошлого человека, и следующий,
    кто войдёт в этой вкладке, не должен их унаследовать.
    """
    token = str(st.session_state.get(LOGIN_TOKEN_KEY) or "")
    if token:
        try:
            revoke_session(token)
        except Exception as exc:  # noqa: BLE001 — cookie всё равно стирается
            report_failure("вход по email: не удалось отозвать запомненный вход", exc)
    st.session_state.clear()
    st.session_state[COOKIE_CHECKED_KEY] = True
    st.session_state[COOKIE_OP_KEY] = ("clear", "")
    st.rerun()


def _nobody_logged_in() -> bool:
    return not (
        current_email()
        or st.session_state.get("platform_is_admin")
        or st.session_state.get("platform_project_id")
    )


def render_account_panel() -> None:
    """«Вы вошли: адрес» с выходом, либо форма входа по email."""
    email = current_email()
    if email:
        who = "владелец платформы" if is_owner_email(email) else "вход по email"
        st.sidebar.success(f"Вы вошли: {email} · {who}")
        if st.sidebar.button("Выйти", key="login_logout"):
            logout()
        return
    if email_login_enabled() and _nobody_logged_in():
        render_login_form()


def render_login_form() -> None:
    st.sidebar.markdown("**Вход по email**")
    notice = st.session_state.get(NOTICE_KEY)
    pending = str(st.session_state.get(PENDING_EMAIL_KEY) or "")
    if not pending:
        email = st.sidebar.text_input(
            "Рабочий email", key="login_email", placeholder="ivan@company.ru"
        )
        if st.sidebar.button("Получить код", key="login_send_code"):
            try:
                step = request_login_code(email)
            except Exception as exc:  # noqa: BLE001 — нет таблиц 0007 или сети
                report_failure("вход по email: форма отправки кода", exc)
                st.sidebar.error(NOT_READY_MESSAGE)
                return
            if step.ok:
                st.session_state[PENDING_EMAIL_KEY] = step.email
                st.session_state[NOTICE_KEY] = step.message
                st.rerun()
            st.sidebar.error(step.message)
        st.sidebar.caption("Или войдите кодом проекта ниже.")
        return

    st.sidebar.caption(f"Адрес: {pending}")
    if notice:
        st.sidebar.info(notice)
    code = st.sidebar.text_input("Код из письма", key="login_code", max_chars=16)
    if st.sidebar.button("Войти", key="login_verify", type="primary"):
        try:
            step = verify_login_code(pending, code)
        except Exception as exc:  # noqa: BLE001 — нет таблиц 0007 или сети
            report_failure("вход по email: форма проверки кода", exc)
            st.sidebar.error(NOT_READY_MESSAGE)
            return
        if step.ok:
            _complete_login(step.email)
        st.sidebar.error(step.message)
    resend_col, change_col = st.sidebar.columns(2)
    if resend_col.button("Прислать ещё", key="login_resend"):
        try:
            step = request_login_code(pending)
        except Exception as exc:  # noqa: BLE001 — нет таблиц 0007 или сети
            report_failure("вход по email: повторная отправка кода", exc)
            st.sidebar.error(NOT_READY_MESSAGE)
            return
        if step.ok:
            st.session_state[NOTICE_KEY] = step.message
            st.rerun()
        st.sidebar.error(step.message)
    if change_col.button("Другой адрес", key="login_change_email"):
        st.session_state.pop(PENDING_EMAIL_KEY, None)
        st.session_state.pop(NOTICE_KEY, None)
        st.rerun()


def _cookie_script(action: str, token: str) -> str:
    # Secure только на https: на локальном http браузер такой cookie бы
    # просто отбросил. Path=/ — на Streamlit Cloud приложение открыто по
    # вложенному пути, а запрос вкладки идёт к корню домена.
    if action == "set":
        value = json.dumps(f"{COOKIE_NAME}={token}")
        max_age = SESSION_TTL_DAYS * 24 * 3600
    else:
        value = json.dumps(f"{COOKIE_NAME}=")
        max_age = 0
    return (
        "<script>(function(){"
        "var secure = window.location.protocol === 'https:' ? '; Secure' : '';"
        f"document.cookie = {value} + '; Max-Age={max_age}; Path=/; SameSite=Lax' + secure;"
        "})();</script>"
    )


def flush_login_cookie() -> None:
    """Записать или стереть cookie, если вход только что начался или закончился.

    Отдельным шагом на следующей перерисовке, а не сразу при входе: вход
    заканчивается st.rerun(), и скрипт, отправленный прямо перед ним, до
    браузера может не дойти.
    """
    op = st.session_state.pop(COOKIE_OP_KEY, None)
    if not op:
        return
    action, token = op
    st.sidebar.html(_cookie_script(action, token), unsafe_allow_javascript=True)
