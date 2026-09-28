# -*- coding: utf-8 -*-
"""Граница отказа раздела: падение одного раздела не уносит страницу.

render_section_safely — граница вокруг раздела, _as_fragment — фрагмент
Streamlit со своей границей внутри (при перерисовке одного фрагмента внешней
границы нет). Стережёт tests/test_section_boundary.py.

Вынесено из app; app реэкспортирует отсюда прежние имена.
"""

from __future__ import annotations

import functools
import logging

import streamlit as st

from services.observability import report_failure

# Имя логгера — прежнее, из app: события раздела идут в тот же канал.
LOGGER = logging.getLogger("platform.app")


def render_section_safely(title: str, render, *args, _details: bool = False, **kwargs) -> bool:
    """Отрисовать раздел так, чтобы его падение не уносило всю страницу.

    Без этой границы исключение в любом блоке роняло весь дашборд: заказчик
    видел трейсбек вместо платформы, хотя не работал один раздел из двенадцати.

    st.rerun() внутри раздела продолжает работать: RerunException наследуется от
    BaseException, поэтому мимо except Exception проходит насквозь. Расширить
    границу до BaseException — значит молча сломать каждую кнопку в приложении;
    это стережёт tests/test_section_boundary.py.

    Параметр назван с подчёркиванием, чтобы не столкнуться с именами аргументов
    самих разделов, которые уезжают дальше через **kwargs.
    """
    if getattr(render, "_guarded_fragment", False):
        # Фрагмент ловит свои ошибки сам (см. _as_fragment): при перерисовке
        # одного фрагмента эта граница уже не участвует.
        kwargs = {**kwargs, "_section_title": title, "_section_details": _details}
    try:
        render(*args, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 — это и есть граница отказа
        _render_section_failure(title, exc, _details)
        return False


def _render_section_failure(title: str, exc: BaseException, details: bool) -> None:
    LOGGER.error("Раздел «%s» не отрисовался", title, exc_info=exc)
    # Заказчик видит вежливое сообщение, а владелец платформы — событие
    # в настроенном канале (Sentry или вебхук). Без настройки — только лог.
    report_failure(f"раздел «{title}»", exc)
    st.error(f"Не удалось отобразить раздел «{title}».")
    st.caption(
        "Остальные разделы продолжают работать. Попробуйте обновить "
        "страницу, выбрать другой период или вернуться сюда позже."
    )
    if details:
        with st.expander("Подробности ошибки", expanded=False):
            st.exception(exc)


def _as_fragment(func):
    """Обернуть раздел во фрагмент, если версия Streamlit это умеет.

    Внутри фрагмента перерисовывается только он сам: пагинация ленты, выбор
    тега или инфоповода больше не заставляют приложение заново собирать данные
    всего проекта.

    Ошибку фрагмент ловит сам, внутри. Исключение, вышедшее из фрагмента,
    Streamlit показывает своим трейсбеком — с путями к файлам и текстом
    ошибки — любому, кто смотрит страницу, и только потом отдаёт наружу; а при
    перерисовке одного фрагмента (клик по тегу, листание ленты) внешней
    границы render_section_safely нет вовсе. Название раздела и то, можно ли
    показывать подробности, приходят от render_section_safely и сохраняются в
    аргументах фрагмента для его собственных перерисовок.
    """

    @functools.wraps(func)
    def guarded(*args, _section_title: str = "", _section_details: bool = False, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — граница отказа внутри фрагмента
            _render_section_failure(_section_title or "раздел", exc, _section_details)
            return None

    fragment = getattr(st, "fragment", None)
    wrapped = fragment(guarded) if callable(fragment) else guarded
    wrapped._guarded_fragment = True
    return wrapped
