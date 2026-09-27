# -*- coding: utf-8 -*-
"""Ошибки в интерфейсе: понятное сообщение всем, подробности — только владельцу.

Клиент — и пользователь, и аналитик заказчика — не должен видеть текст
исключения Python: в нём пути к файлам, адреса базы и куски данных, а помочь
себе этим он всё равно не может. Подробности нужны тому, кто чинит, — владельцу
платформы. Кто сейчас владелец, решает main() (src/app.py) и кладёт ответ в
сессию: страницы и фрагменты рисуются позже и читают его отсюда.
"""

from __future__ import annotations

import streamlit as st

DETAILS_KEY = "_platform_error_details"


def set_error_details_allowed(allowed: bool) -> None:
    st.session_state[DETAILS_KEY] = bool(allowed)


def error_details_allowed() -> bool:
    return bool(st.session_state.get(DETAILS_KEY))


def show_error_details(exc: BaseException) -> None:
    """Раскрывашка с трейсбеком — только если подробности сейчас разрешены."""
    if not error_details_allowed():
        return
    with st.expander("Подробности ошибки", expanded=False):
        st.exception(exc)
        cause = exc.__cause__ or exc.__context__
        if cause is not None and cause is not exc:
            st.caption("Исходная ошибка:")
            st.exception(cause)


def show_error(message: str, exc: BaseException | None = None, *, warning: bool = False) -> None:
    """Сообщение для человека и, владельцу, подробности исключения."""
    (st.warning if warning else st.error)(message)
    if exc is not None:
        show_error_details(exc)
