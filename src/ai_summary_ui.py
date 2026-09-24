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
    PROVIDER_GIGACHAT,
    PROVIDER_OFF,
    AIError,
    check_connection,
    describe_certificates,
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
from services.metrics_compute import sentiment_unmarked
from services.cached_store import (
    ManualEditConflict,
    clear_platform_caches,
    delete_manual,
    get_manual,
    get_manual_version,
    save_manual,
    update_project,
)
from services.project_settings import (
    DEMO_AI_LIMIT,
    category_brands_from_project_settings,
    demo_ai_runs_left,
    demo_ai_runs_used,
    is_demo_project,
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


def render_certificate_helper() -> None:
    """Собрать сертификат НУЦ Минцифры без терминала и без коммита в репозиторий.

    На Streamlit Cloud терминала нет, а два скачанных файла надо ещё и склеить.
    Поэтому: загрузили оба файла — платформа проверила, что это действительно
    сертификаты удостоверяющего центра и они не просрочены, и выдала готовый
    блок для вставки в Secrets.
    """
    with st.expander("Сертификат для GigaChat: собрать без терминала", expanded=False):
        st.markdown(
            "1. Откройте **gosuslugi.ru/crt** и скачайте два файла для Linux "
            "в формате `.crt` — **корневой** и **выпускающий**.\n"
            "2. Загрузите здесь оба.\n"
            "3. Скопируйте получившийся блок в Streamlit Secrets."
        )
        uploaded = st.file_uploader(
            "Файлы сертификатов",
            type=["crt", "pem", "cer", "txt"],
            accept_multiple_files=True,
            key="ai_ca_upload",
        )
        if not uploaded:
            return

        parts = []
        for item in uploaded:
            try:
                text = item.getvalue().decode("utf-8", errors="replace").strip()
            except Exception as exc:  # noqa: BLE001
                st.error(f"{item.name}: не удалось прочитать — {exc}")
                return
            if "-----BEGIN CERTIFICATE-----" not in text:
                st.error(
                    f"{item.name}: это не PEM-сертификат. На госуслугах нужен "
                    "формат для Linux (.crt), а не .cer для Windows."
                )
                return
            parts.append(text)

        pem = "\n".join(parts).strip() + "\n"
        blocks = describe_certificates(pem)
        if blocks:
            for block in blocks:
                if not block.get("ok"):
                    st.error("Один из файлов не разобрался как сертификат.")
                    return
                name = str(block.get("subject") or "")
                tail = name.split(",")[0].replace("CN=", "") or name
                if block.get("expired"):
                    st.error(f"{tail}: срок действия истёк — нужен свежий файл.")
                    return
                if not block.get("is_ca"):
                    st.warning(
                        f"{tail}: это не сертификат удостоверяющего центра. "
                        "Похоже, скачан не тот файл."
                    )
                st.success(
                    f"{tail} · действует до "
                    f"{block['not_after'].strftime('%d.%m.%Y')}"
                )
            if len(blocks) < 2:
                st.warning(
                    "Загружен только один сертификат. Нужны оба — корневой и "
                    "выпускающий, иначе цепочка не соберётся."
                )

        st.caption("Скопируйте это в Streamlit Secrets целиком:")
        st.code('GIGACHAT_CA_PEM = """\n' + pem + '"""', language="toml")
        st.caption(
            "После сохранения секретов приложение перезапустится само. "
            "Затем нажмите «Проверить подключение»."
        )


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
        # Тот же источник SOV, что в разделе «Индексы бренда»: загруженная
        # категория, а без неё — бренды, размеченные тегами самой выгрузки.
        benchmark = category_store.resolve_category_benchmark(
            messages, category_brands_from_project_settings(project_settings), benchmarks
        )
        return compute_brand_metrics(
            messages,
            benchmark=benchmark,
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
    client_preview: bool = False,
) -> None:
    """Блок «Тексты от ИИ» в разделе «Отчёт»."""
    # В предпросмотре клиентского вида владелец смотрит на проект глазами
    # заказчика, а заказчик генерацию не видит никогда. Признак владельца здесь
    # живёт отдельно от роли (session_state), поэтому понижения роли мало.
    owner = is_platform_owner() and not client_preview
    # Демо-проект показывают снаружи, и генерация — главное, ради чего его
    # смотрят: без неё демонстрировать нечего. Поэтому доступ здесь не зависит
    # от настройки ai_access, но ограничен счётчиком запусков на весь проект.
    demo = is_demo_project(project_settings) and not client_preview
    if not demo and not can_generate_ai(
        role, project_settings, is_platform_owner=owner
    ):
        return
    demo_left = demo_ai_runs_left(project_settings) if demo and not owner else None
    demo_exhausted = demo_left is not None and demo_left <= 0

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
            # Помощник по сертификату нужен ровно до тех пор, пока сертификата
            # нет: дальше он только занимает место.
            if config.provider == PROVIDER_GIGACHAT and not config.ca_bundle:
                render_certificate_helper()
            if config.is_ready and st.button(
                "Проверить подключение",
                key=f"ai_check_{project_id}",
                help="Один короткий запрос к модели — чтобы не выяснять это на реальном саммари.",
            ):
                with st.spinner("Проверяю подключение..."):
                    try:
                        st.success(check_connection(config))
                    except AIError as exc:
                        st.error(str(exc))
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

        if demo_left is not None:
            message = (
                f"Вам доступно {demo_left} запусков ИИ-генерации из "
                f"{DEMO_AI_LIMIT} на этот демонстрационный проект."
            )
            if demo_exhausted:
                st.warning(
                    "Запуски ИИ-генерации в демонстрационном проекте "
                    "закончились. Тексты, сгенерированные раньше, остаются "
                    "на месте."
                )
            else:
                st.info(message)
        elif not owner:
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

        if sentiment_unmarked(None, messages):
            st.caption(
                "В выгрузке нет разметки тональности: модель получит «Тональность: "
                "нет данных» и не будет оценивать негатив."
            )
        columns = st.columns(3)
        kinds = [KIND_SUMMARY, KIND_BRAND, KIND_RISKS]
        for column, kind in zip(columns, kinds):
            with column:
                if st.button(
                    KIND_TITLES[kind],
                    key=f"ai_generate_{kind}_{project_id}",
                    width="stretch",
                    disabled=demo_exhausted,
                    help=(
                        "Лимит запусков демонстрационного проекта исчерпан."
                        if demo_exhausted
                        else None
                    ),
                ):
                    _run_generation(
                        project_id, kind, base_args, extra, config
                    )
                    # Счёт ведётся по нажатию, а не по успеху: иначе неудачный
                    # запрос к модели, который всё равно оплачен, лимит бы не
                    # тратил и демо можно было бы крутить бесконечно.
                    if demo_left is not None:
                        _spend_demo_run(project_id, project_settings)

        for kind in kinds:
            _render_generated_block(project_id, kind, period_ids)


def _spend_demo_run(project_id: str, project_settings: dict[str, Any] | None) -> None:
    """Списать один запуск ИИ в демо-проекте.

    Счётчик живёт в настройках проекта и не сбрасывается: демо-доступ выдаётся
    многим, и обнуление по времени сделало бы лимит бесконечным. Сбросить его
    может только владелец платформы вручную в карточке проекта.
    """
    updated = dict(project_settings or {})
    updated["demo_ai_runs"] = demo_ai_runs_used(project_settings) + 1
    try:
        update_project(project_id, settings=updated)
        clear_platform_caches(project_id)
    except Exception as exc:  # noqa: BLE001
        # Списание не должно ронять уже сделанную генерацию: текст у человека
        # на экране, а несписанный запуск — меньшее зло, чем упавший раздел.
        st.warning(f"Не удалось обновить счётчик запусков: {exc}")


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
    storage_key = ai_text_storage_key(kind, period_ids)
    widget_key = f"ai_text_{kind}_{project_id}"
    # Версия замораживается при первом показе поля: пока редактор правит
    # текст, кеш с TTL может подтянуть чужое сохранение, и запись затёрла бы
    # его без предупреждения.
    versions_key = f"manual_versions::{widget_key}"
    if widget_key not in st.session_state or versions_key not in st.session_state:
        st.session_state[versions_key] = get_manual_version(
            project_id, storage_key, "ai_texts"
        )
    edited = st.text_area(
        "Текст",
        value=current,
        height=260,
        key=widget_key,
        label_visibility="collapsed",
    )

    columns = st.columns(3)
    with columns[0]:
        if st.button(
            "Сохранить", key=f"ai_save_{kind}_{project_id}", width="stretch"
        ):
            payload = dict(draft or saved or {})
            payload.update({"text": edited, "kind": kind, "period_ids": period_ids})
            try:
                save_manual(
                    project_id,
                    "ai_texts",
                    storage_key,
                    payload,
                    expected_updated_at=st.session_state.get(versions_key),
                )
            except ManualEditConflict:
                st.error(
                    "Этот текст только что сохранил другой редактор — запись "
                    "отменена, чтобы не затереть его версию. Ваш текст остался "
                    "в поле; повторное сохранение запишет его поверх."
                )
                clear_platform_caches(project_id)
                st.session_state.pop(versions_key, None)
            else:
                st.session_state.pop(versions_key, None)
                st.session_state.pop(f"ai_draft_{kind}_{project_id}", None)
                st.success("Сохранено. Текст виден в своём разделе дашборда.")
                st.rerun()
    with columns[1]:
        if kind == KIND_SUMMARY and st.button(
            "Сделать саммари периода",
            key=f"ai_promote_{kind}_{project_id}",
            width="stretch",
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
            width="stretch",
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
