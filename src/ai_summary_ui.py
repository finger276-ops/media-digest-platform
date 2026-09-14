"""Панель генерации текстов моделью.

Ограничение доступа здесь — не косметика. Генерация тратит деньги на каждый
запуск и отправляет данные проекта внешнему сервису, поэтому по умолчанию её
видит только владелец платформы: решение о запуске принимает тот, кто отвечает
за расходы и за данные.

Владелец может открыть генерацию редакторам конкретного проекта — настройка
хранится в `platform_projects.settings.ai_access` и действует только в своём
проекте. Клиенту (роль viewer) генерация недоступна в любом случае: он не может
и остальное редактировать, а каждая кнопка стоит денег.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from services import category_store
from services.ai_provider import (
    PROVIDER_OFF,
    AIError,
    describe_config,
    estimate_tokens,
    load_ai_config,
)
from services.ai_summary import (
    ACCESS_OPTIONS,
    KIND_BRAND,
    KIND_RISKS,
    KIND_SUMMARY,
    KIND_TITLES,
    ai_access_level,
    ai_text_storage_key,
    build_data_card,
    can_generate_ai,
    generate_text,
)
from services.brand_metrics import compute_brand_metrics, merge_settings
from services.cached_store import (
    clear_platform_caches,
    delete_manual,
    get_manual,
    save_manual,
    update_project,
)

SETUP_HINT = """
```toml
AI_PROVIDER = "yandex"          # или "gigachat"
YANDEX_API_KEY = "..."          # для YandexGPT
YANDEX_FOLDER_ID = "..."        # для YandexGPT
GIGACHAT_AUTH_KEY = "..."       # для GigaChat, ключ авторизации (Basic)
```
"""

def is_platform_owner() -> bool:
    """Владелец платформы — тот, кто вошёл по PLATFORM_ADMIN_PASSWORD."""
    return bool(st.session_state.get("platform_is_admin"))


def _render_access_control(
    project_id: str, project_settings: dict[str, Any] | None
) -> None:
    """Переключатель доступа. Виден только владельцу платформы."""
    current = ai_access_level(project_settings)
    options = list(ACCESS_OPTIONS)
    choice = st.selectbox(
        "Кому доступна генерация в этом проекте",
        options,
        index=options.index(current),
        format_func=lambda value: ACCESS_OPTIONS[value],
        key=f"ai_access_{project_id}",
        help=(
            "Настройка действует только в этом проекте. Клиенты (роль «просмотр») "
            "генерацию не получают ни при каком значении — они видят только "
            "сохранённые тексты."
        ),
    )
    if choice == current:
        return
    if st.button(
        "Сохранить доступ", key=f"ai_access_save_{project_id}", type="primary"
    ):
        updated = dict(project_settings or {})
        updated["ai_access"] = choice
        try:
            update_project(project_id, settings=updated)
            clear_platform_caches(project_id)
        except Exception as exc:  # noqa: BLE001 - показываем причину, не падаем
            st.warning(f"Не удалось сохранить: {exc}")
            return
        st.success(f"Теперь генерация доступна: {ACCESS_OPTIONS[choice].lower()}.")
        st.rerun()


def load_ai_text(
    project_id: str, kind: str, period_ids: list[str]
) -> dict[str, Any] | None:
    """Сохранённый текст модели для этого набора периодов."""
    try:
        payload = get_manual(project_id, ai_text_storage_key(kind, period_ids))
    except Exception:  # noqa: BLE001 - без текста раздел работает как раньше
        return None
    if isinstance(payload, dict) and str(payload.get("text") or "").strip():
        return payload
    return None


def render_saved_ai_text(
    project_id: str, kind: str, period_ids: list[str], *, heading: str = ""
) -> bool:
    """Показать сохранённый текст модели в профильном разделе.

    Возвращает True, если что-то показано, — вызывающий код может решить, нужен
    ли ему собственный заполнитель.
    """
    payload = load_ai_text(project_id, kind, period_ids)
    if not payload:
        return False
    with st.container(border=True):
        if heading:
            st.markdown(f"**{heading}**")
        st.markdown(str(payload.get("text") or "").replace("\n", "  \n"))
        st.caption(_origin_caption(payload))
    return True


def _origin_caption(payload: dict[str, Any]) -> str:
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    created = str(payload.get("created_at") or "")[:16].replace("T", " ")
    parts = ["Текст сгенерирован моделью и проверен аналитиком"]
    tail = " · ".join(x for x in (provider, model, created) if x)
    if tail:
        parts.append(tail)
    return " · ".join(parts)


def _brand_cards(
    project_id: str,
    project_settings: dict[str, Any] | None,
    messages: pd.DataFrame,
    period_ids: list[str],
) -> dict[str, dict[str, Any]]:
    try:
        benchmarks = category_store.load_benchmarks(project_id, period_ids)
    except Exception:  # noqa: BLE001 - индексы считаются и без категорийных данных
        benchmarks = {}
    try:
        return compute_brand_metrics(
            messages,
            benchmark=category_store.merged_benchmark(benchmarks),
            settings=merge_settings((project_settings or {}).get("brand_metrics")),
        )
    except Exception:  # noqa: BLE001
        return {}


def render_ai_summary_panel(
    project_id: str,
    project_name: str,
    period_ids: list[str],
    messages: pd.DataFrame,
    events_agg: pd.DataFrame,
    periods: pd.DataFrame,
    *,
    role: str = "",
    metrics: dict[str, Any] | None = None,
    project_settings: dict[str, Any] | None = None,
) -> None:
    """Блок «Тексты от ИИ» в разделе «Отчёт»."""
    owner = is_platform_owner()
    if not can_generate_ai(role, project_settings, is_platform_owner=owner):
        return

    config = load_ai_config()
    with st.expander("Тексты от ИИ", expanded=False):
        st.caption(
            "Платформа считает цифры сама, модель их только интерпретирует: "
            "в промпт уходит готовая карточка данных, а не выгрузка."
        )
        st.caption(describe_config(config))

        # Доступ настраивается независимо от ключей: владелец может открыть его
        # редакторам заранее, а ключи добавить позже — и наоборот.
        if owner:
            _render_access_control(project_id, project_settings)
            st.divider()

        if config.provider == PROVIDER_OFF or not config.is_ready:
            if owner:
                st.info(
                    "Генерация не настроена. "
                    + (config.problem or "")
                    + " Добавьте в Streamlit Secrets:"
                )
                st.markdown(SETUP_HINT)
            else:
                st.info(
                    "Генерация пока не настроена — обратитесь к владельцу платформы."
                )
            return

        if not owner:
            st.caption(
                "Доступ к генерации открыт владельцем платформы. "
                "Каждый запуск тратит платный запрос к модели."
            )

        include_excerpts = st.checkbox(
            "Отправлять выдержки сообщений",
            value=True,
            key=f"ai_excerpts_{project_id}",
            help=(
                "Без выдержек модель видит только цифры и заголовки — тексты "
                "сообщений не покидают платформу, но и формулировки клиентов "
                "в саммари не попадут."
            ),
        )
        extra = st.text_area(
            "Дополнительные указания к тексту",
            value="",
            height=80,
            key=f"ai_extra_{project_id}",
            placeholder="Например: сделать акцент на строительной рознице, писать суше.",
        )

        brand_cards = _brand_cards(project_id, project_settings, messages, period_ids)
        base_args = dict(
            project_name=project_name,
            periods=periods,
            period_ids=period_ids,
            messages=messages,
            events_agg=events_agg,
            metrics=metrics,
            brand_cards=brand_cards,
            include_excerpts=include_excerpts,
        )
        preview_card = build_data_card(**base_args)

        with st.expander("Что именно уходит в модель", expanded=False):
            st.caption(
                f"Примерно {estimate_tokens(preview_card)} токенов. "
                "Это весь контекст запроса — ничего сверх этого не отправляется."
            )
            st.code(preview_card, language="text")

        columns = st.columns(3)
        kinds = [KIND_SUMMARY, KIND_BRAND, KIND_RISKS]
        for column, kind in zip(columns, kinds):
            with column:
                if st.button(
                    KIND_TITLES[kind],
                    key=f"ai_generate_{kind}_{project_id}",
                    use_container_width=True,
                ):
                    _run_generation(
                        project_id, kind, base_args, extra, config
                    )

        for kind in kinds:
            _render_generated_block(project_id, kind, period_ids)


def _run_generation(
    project_id: str,
    kind: str,
    base_args: dict[str, Any],
    extra: str,
    config: Any,
) -> None:
    args = dict(base_args)
    if kind == KIND_RISKS:
        # Блоку рисков нужны именно негативные сообщения, а не самые заметные.
        args["negative_excerpts"] = True
    card = build_data_card(**args)
    with st.spinner(f"{KIND_TITLES[kind]}: модель пишет текст..."):
        try:
            result = generate_text(
                kind, card, extra_instructions=extra, config=config
            )
        except AIError as exc:
            st.error(str(exc))
            return
    st.session_state[f"ai_draft_{kind}_{project_id}"] = result
    st.success(f"{KIND_TITLES[kind]}: готово. Проверьте текст и сохраните.")


def _render_generated_block(project_id: str, kind: str, period_ids: list[str]) -> None:
    draft = st.session_state.get(f"ai_draft_{kind}_{project_id}")
    saved = load_ai_text(project_id, kind, period_ids)
    if not draft and not saved:
        return

    st.divider()
    st.markdown(f"**{KIND_TITLES[kind]}**")
    current = str((draft or saved or {}).get("text") or "")
    edited = st.text_area(
        "Текст",
        value=current,
        height=260,
        key=f"ai_text_{kind}_{project_id}",
        label_visibility="collapsed",
    )

    columns = st.columns(3)
    with columns[0]:
        if st.button(
            "Сохранить", key=f"ai_save_{kind}_{project_id}", use_container_width=True
        ):
            payload = dict(draft or saved or {})
            payload.update({"text": edited, "kind": kind, "period_ids": period_ids})
            save_manual(
                project_id, "ai_texts", ai_text_storage_key(kind, period_ids), payload
            )
            st.session_state.pop(f"ai_draft_{kind}_{project_id}", None)
            st.success("Сохранено. Текст виден в своём разделе дашборда.")
            st.rerun()
    with columns[1]:
        if kind == KIND_SUMMARY and st.button(
            "Сделать саммари периода",
            key=f"ai_promote_{kind}_{project_id}",
            use_container_width=True,
            help=(
                "Заменить текст саммари периода этим. Он попадёт в Word, PDF и "
                "PNG-выгрузки и будет виден клиенту."
            ),
        ):
            periods_key = "summary::" + "__".join(
                sorted(str(x) for x in period_ids if str(x).strip())
            )
            save_manual(
                project_id,
                "summaries",
                periods_key,
                {"summary": edited, "period_ids": period_ids, "source": "ai"},
            )
            st.success("Саммари периода заменено.")
            st.rerun()
    with columns[2]:
        if saved and st.button(
            "Удалить",
            key=f"ai_delete_{kind}_{project_id}",
            use_container_width=True,
        ):
            delete_manual(project_id, ai_text_storage_key(kind, period_ids))
            st.session_state.pop(f"ai_draft_{kind}_{project_id}", None)
            st.rerun()

    if draft:
        st.caption(
            f"Черновик · {draft.get('provider')} · {draft.get('model')} · "
            f"запрос ~{draft.get('prompt_tokens_estimate')} токенов. "
            "Пока не сохранён."
        )
