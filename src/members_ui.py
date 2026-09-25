# -*- coding: utf-8 -*-
"""Карточка проекта → «Доступ по email»: кто входит в проект по адресу.

Выдаёт и снимает доступ только владелец платформы. Аналитик видит список
своего проекта, но не меняет его: иначе подрядчик с кодом аналитика мог бы
сам раздавать доступ к данным заказчика.

Блок вынесен из render_project_manager отдельной функцией: карточка
проекта открывается выбором строки в таблице, а его тестом не нажать —
здесь же всё проверяется напрямую (tests/members_app.py).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from platform_store import MEMBER_ROLES, member_problem, normalize_email
from services.cached_store import (
    list_project_members,
    remove_project_member,
    save_project_member,
)
from services.email_login import email_login_enabled
from services.formatting import fmt_date
from services.roles import role_title

REMOVE = "__remove__"


def _members_table(members: pd.DataFrame) -> pd.DataFrame:
    view = pd.DataFrame(
        {
            "Email": members["user_email"].astype(str),
            "Роль": members["role"].astype(str).map(role_title),
        }
    )
    if "created_at" in members.columns:
        view["Выдан"] = members["created_at"].apply(fmt_date)
    return view


def render_project_members(project_id: str, *, can_edit: bool) -> None:
    flash_key = f"member_flash_{project_id}"
    flash = st.session_state.pop(flash_key, None)
    with st.expander("Доступ по email", expanded=bool(flash)):
        if flash:
            # Сообщение переживает перерисовку: без неё таблица выше
            # показала бы старый список, а с ней успех мелькнул бы и пропал.
            st.success(flash)
        if not email_login_enabled():
            st.caption(
                "Вход по email пока выключен (EMAIL_LOGIN_ENABLED в секретах). "
                "Список можно подготовить заранее — он заработает, когда вход "
                "включат."
            )
        try:
            members = list_project_members(project_id)
        except Exception as exc:  # noqa: BLE001 — карточка проекта важнее списка
            st.warning("Не удалось загрузить список доступа.")
            if can_edit:
                st.caption(str(exc))
            return
        if not members.empty and "status" in members.columns:
            members = members[members["status"].astype(str) == "active"]

        if members.empty:
            st.caption("Доступ по email к проекту ещё никому не выдан.")
        else:
            st.dataframe(_members_table(members), hide_index=True, width="stretch")

        if not can_edit:
            st.caption("Доступы по email выдаёт владелец платформы.")
            return

        st.caption(
            "Человек входит по адресу и коду из письма и видит только проекты, "
            "к которым ему выдан доступ. Владельцы платформы в списке не "
            "нужны — они видят всё."
        )
        new_email = st.text_input(
            "Email", key=f"member_email_{project_id}", placeholder="ivan@company.ru"
        )
        new_role = st.selectbox(
            "Роль",
            list(MEMBER_ROLES),
            format_func=role_title,
            key=f"member_role_{project_id}",
        )
        if st.button("Выдать доступ", key=f"member_add_{project_id}"):
            problem = member_problem(new_email, new_role)
            if problem:
                st.error(problem)
            else:
                saved = save_project_member(project_id, new_email, new_role)
                st.session_state[flash_key] = (
                    f"Доступ выдан: {saved} · {role_title(new_role)}."
                )
                st.rerun()

        if members.empty:
            return
        st.markdown("**Изменить доступ**")
        emails = members["user_email"].astype(str).tolist()
        target = st.selectbox(
            "Кому", emails, key=f"member_target_{project_id}"
        )
        action = st.selectbox(
            "Что сделать",
            [*MEMBER_ROLES, REMOVE],
            format_func=lambda x: (
                "Закрыть доступ" if x == REMOVE else f"Сделать: {role_title(x)}"
            ),
            key=f"member_action_{project_id}",
        )
        if st.button("Применить", key=f"member_apply_{project_id}"):
            email = normalize_email(target)
            if action == REMOVE:
                remove_project_member(project_id, email)
                st.session_state[flash_key] = f"Доступ закрыт: {email}."
            else:
                save_project_member(project_id, email, action)
                st.session_state[flash_key] = (
                    f"Роль изменена: {email} · {role_title(action)}."
                )
            st.rerun()
