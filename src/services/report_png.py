# -*- coding: utf-8 -*-
"""PNG-инфографика отчёта: один лист A4, блоки сверху вниз.

Каждый включённый блок рисуется от текущего курсора и возвращает место, где
начнётся следующий; выключенный не сдвигает курсор. Данные — payload из
report_export.summary_export_payload.
"""

from __future__ import annotations

import logging
import textwrap
from io import BytesIO
from typing import Any


from .chart_style import SENTIMENT_COLOR_RANGE
from .metrics_compute import (
    NO_SENTIMENT_REASON,
    format_int,
    metric_text,
    percent_text,
)
from .project_settings import valid_hex_color
from .report_common import (
    _adaptive_font_size,
    _export_sentiment,
    _logo_image_from_payload,
    message_types_line,
    resolve_report_sections,
    _short_label,
    sources_line,
    _VISUAL_SECTIONS,
)

LOGGER = logging.getLogger("platform.report_export")


def _draw_export_card(
    ax,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    value: str,
    subtitle: str = "",
    *,
    accent_color: str = "#2563eb",
) -> None:
    from matplotlib.patches import FancyBboxPatch

    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.014,rounding_size=0.018",
        linewidth=0.9,
        edgecolor="#d9e0ea",
        facecolor="#f8fafc",
    )
    ax.add_patch(patch)
    ax.plot(
        [x + 0.016, x + 0.016],
        [y + 0.022, y + h - 0.022],
        color=accent_color,
        linewidth=2.2,
    )
    ax.text(
        x + 0.040,
        y + h - 0.026,
        title,
        fontsize=8.8,
        color="#4b5563",
        va="top",
        ha="left",
    )
    ax.text(
        x + 0.040,
        y + h * 0.50,
        value,
        fontsize=_adaptive_font_size(value, base=15, min_size=10),
        color="#111827",
        va="center",
        ha="left",
        fontweight="bold",
    )
    if subtitle:
        ax.text(
            x + 0.040,
            y + 0.018,
            subtitle,
            fontsize=7.4,
            color="#6b7280",
            va="bottom",
            ha="left",
        )


# Отступ до подписи в футере (см. её fontsize/позицию ниже) - последний
# блок ("Главное") не должен налезать на неё, даже если саммари длинное и
# даёт 4 длинных пункта, каждый на 2 строки.
_FOOTER_CLEARANCE = 0.075


def _draw_metrics_section(ax, payload, comparison, accent, top: float) -> float:
    """Крупные карточки шапки — сумма по всей выбранной области, не разбивка.

    comparison_sequence — это точки ВНУТРИ уже выбранных данных (день, неделя,
    месяц или файл целиком, смотря что выбрано в «Гранулярности»), а не
    период до выбранного диапазона. Раньше при len(comparison) >= 2 — то есть
    при любой выгрузке длиннее одного дня с гранулярностью по умолчанию
    («День») — карточки и подпись «Последний период: …» показывали только
    ПОСЛЕДНЮЮ точку разбивки, а не итог. На реальной выгрузке 17.09–24.09 это
    выглядело как «Сообщения 54» здесь и «724» на странице «Главное» того же
    документа. Динамика внутри диапазона — дело графика на экране, а не
    противоречащих друг другу карточек в отчёте.
    """
    metric_cards = [
        ("Сообщения", format_int(payload.get("messages", 0)), ""),
        ("Аудитория", metric_text(payload, "audience"), ""),
        ("Охват", metric_text(payload, "reach"), ""),
        ("Вовлеченность", metric_text(payload, "engagement"), ""),
    ]

    xs = [0.060, 0.525]
    ys = [top - 0.107, top - 0.227]
    for idx, (title, value, subtitle) in enumerate(metric_cards):
        _draw_export_card(
            ax,
            xs[idx % 2],
            ys[idx // 2],
            0.405,
            0.095,
            title,
            value,
            f"к пред. периоду: {subtitle}" if subtitle else "",
            accent_color=accent,
        )
    # 0.227 - высота самого блока (до низа второй строки карточек), + 0.050 -
    # зазор до заголовка следующего блока в исходной раскладке (0.862 -> 0.585).
    # Типы сообщений и площадки — строками под карточками, в зазоре до
    # следующего блока: ещё ряд карточек или отдельный блок вытеснили бы
    # «Главное» с листа. Первая строка сдвигает блоки ниже на 0.010, вторая —
    # ещё на 0.010: при полном наборе блоков это одна строка «Главного».
    lines = [message_types_line(payload)]
    if "top_sources" in set(payload.get("sections") or []):
        lines.append(sources_line(payload))
    lines = [line for line in lines if line]
    if not lines:
        return top - 0.277
    for index, line in enumerate(lines):
        ax.text(
            0.060,
            top - 0.251 - index * 0.019,
            _short_label(line, 120),
            fontsize=8.4,
            color="#374151",
            va="top",
            ha="left",
        )
    return top - 0.287 - (len(lines) - 1) * 0.010


def _draw_sentiment_section(ax, fig, payload, comparison, top: float) -> float:
    from matplotlib.patches import Rectangle

    pos, neu, neg, total, unmarked = _export_sentiment(payload, comparison)
    total = max(1, total)

    ax.text(
        0.060,
        top,
        "Тональность",
        fontsize=12,
        fontweight="bold",
        color="#111827",
        va="top",
        ha="left",
    )
    if unmarked:
        # Сплошное кольцо «Нейтрал 100 %» выглядело бы как «негатива нет».
        ax.text(
            0.060,
            top - 0.032,
            textwrap.fill(NO_SENTIMENT_REASON, 70),
            fontsize=9,
            color="#6b7280",
            va="top",
            ha="left",
        )
        return top - 0.085
    pie_bottom = top - 0.158
    pie_ax = fig.add_axes([0.070, pie_bottom, 0.220, 0.145])
    pie_ax.axis("equal")
    values = [max(pos, 0), max(neu, 0), max(neg, 0)]
    # Те же цвета, что и на живом дашборде (services/chart_style.py) - иначе
    # тональность выглядела бы разными оттенками зелёного/красного на экране
    # и в выгруженном PNG/PDF/DOCX одного и того же периода.
    colors = list(SENTIMENT_COLOR_RANGE)
    labels = ["Позитив", "Нейтрал", "Негатив"]
    if sum(values) <= 0:
        values = [1]
        pie_colors = ["#d1d5db"]
    else:
        pie_colors = colors
    pie_ax.pie(
        values,
        colors=pie_colors,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "white"},
    )
    pie_ax.text(
        0,
        0.05,
        format_int(total),
        ha="center",
        va="center",
        fontsize=12.5,
        fontweight="bold",
        color="#111827",
    )
    pie_ax.text(
        0, -0.13, "сообщений", ha="center", va="center", fontsize=7.5, color="#6b7280"
    )
    pie_ax.set_xticks([])
    pie_ax.set_yticks([])

    y0 = top - 0.040
    for i, (lab, val, col) in enumerate(zip(labels, [pos, neu, neg], colors)):
        yy = y0 - i * 0.041
        ax.add_patch(
            Rectangle(
                (0.330, yy - 0.010), 0.014, 0.014, facecolor=col, edgecolor="none"
            )
        )
        ax.text(0.352, yy, lab, fontsize=9.2, color="#111827", va="center", ha="left")
        ax.text(
            0.490,
            yy,
            format_int(val),
            fontsize=9.2,
            color="#111827",
            va="center",
            ha="right",
            fontweight="bold",
        )
        ax.text(
            0.510,
            yy,
            percent_text(val, total),
            fontsize=8.4,
            color="#6b7280",
            va="center",
            ha="left",
        )
    # pie_bottom (top - 0.158) - низ самого донат-графика; + 0.045 - зазор до
    # заголовка следующего блока в исходной раскладке (0.585 -> 0.382... то
    # есть до низа доната остаётся 0.427, а следующий блок стартовал на 0.382).
    return pie_bottom - 0.045


def _draw_top_lists_section(
    ax, payload, top: float, *, show_tags: bool, show_events: bool
) -> float:
    if show_tags:
        top_tags = payload.get("top_tags") or []
        ax.text(
            0.060,
            top,
            "Топ тегов",
            fontsize=11.5,
            fontweight="bold",
            color="#111827",
            va="top",
            ha="left",
        )
        y = top - 0.028
        if top_tags:
            for item in top_tags[:5]:
                ax.text(
                    0.070,
                    y,
                    f"• {_short_label(item.get('name'), 28)}",
                    fontsize=8.4,
                    color="#111827",
                    va="top",
                    ha="left",
                )
                ax.text(
                    0.430,
                    y,
                    f"{format_int(item.get('messages', 0))} сообщ.",
                    fontsize=7.8,
                    color="#6b7280",
                    va="top",
                    ha="right",
                )
                y -= 0.028
        else:
            ax.text(
                0.070,
                y,
                "Нет тегов для отображения",
                fontsize=8.4,
                color="#6b7280",
                va="top",
                ha="left",
            )

    if show_events:
        top_events = payload.get("top_events") or []
        ax.text(
            0.525,
            top,
            "Топ инфоповодов",
            fontsize=11.5,
            fontweight="bold",
            color="#111827",
            va="top",
            ha="left",
        )
        y = top - 0.028
        if top_events:
            for item in top_events[:5]:
                ax.text(
                    0.535,
                    y,
                    f"• {_short_label(item.get('name'), 29)}",
                    fontsize=8.4,
                    color="#111827",
                    va="top",
                    ha="left",
                )
                ax.text(
                    0.935,
                    y,
                    f"{format_int(item.get('messages', 0))} сообщ.",
                    fontsize=7.8,
                    color="#6b7280",
                    va="top",
                    ha="right",
                )
                y -= 0.028
        else:
            ax.text(
                0.535,
                y,
                "Нет инфоповодов для отображения",
                fontsize=8.4,
                color="#6b7280",
                va="top",
                ha="left",
            )
    # Фиксированная высота блока (под 5 позиций) вне зависимости от того,
    # сколько реально показано, - так же, как было в исходной раскладке.
    return top - 0.177


def _draw_sources_section(ax, payload, top: float) -> float:
    """Площадки отдельной строкой — когда блок «Основные метрики» выключен.

    С ним строка стоит под карточками метрик (_draw_metrics_section).
    """
    line = sources_line(payload)
    if not line:
        return top
    ax.text(0.060, top, _short_label(line, 120), fontsize=8.4, color="#374151", va="top", ha="left")
    return top - 0.040


def _draw_highlights_section(ax, payload, top: float) -> float:
    ax.text(
        0.060,
        top,
        "Главное",
        fontsize=11.5,
        fontweight="bold",
        color="#111827",
        va="top",
        ha="left",
    )
    summary_y = top - 0.027
    line_count = 0
    # Раньше единственным ограничителем было "не больше 8 строк" - на
    # длинном автосаммари (4 пункта по 2 строки) это давало текст, который
    # реально наезжал на подпись в футере (она стоит на фиксированной
    # 0.045 независимо от того, сколько текста выше). Останавливаемся,
    # как только следующая строка попала бы в зону футера, а не только по
    # счётчику строк.
    for block in (payload.get("summary_highlights") or [])[:4]:
        if summary_y < _FOOTER_CLEARANCE:
            break
        wrapped = textwrap.wrap(str(block), width=86) or [str(block)]
        bullet = True
        for seg in wrapped[:2]:
            if summary_y < _FOOTER_CLEARANCE:
                break
            prefix = "• " if bullet else "  "
            ax.text(
                0.070,
                summary_y,
                prefix + seg,
                fontsize=8.4,
                color="#111827",
                va="top",
                ha="left",
            )
            summary_y -= 0.021
            line_count += 1
            bullet = False
            if line_count >= 8:
                break
        summary_y -= 0.004
        if line_count >= 8:
            break
    return summary_y


def generate_summary_infographic_png(payload: dict[str, Any]) -> bytes:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception as exc:
        raise RuntimeError(
            "Для инфографики нужен matplotlib>=3.8 в requirements.txt."
        ) from exc

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.unicode_minus": False,
        }
    )
    fig = plt.figure(figsize=(8.27, 11.69), dpi=170)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    background = valid_hex_color(payload.get("background_color"), "#ffffff")
    accent = valid_hex_color(payload.get("accent_color"), "#2563eb")
    fig.patch.set_facecolor(background)
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=background, edgecolor="none"))

    project = _short_label(
        payload.get("client_name") or payload.get("project_name") or "Проект", 42
    )
    report_title = _short_label(
        payload.get("report_title") or "Дайджест упоминаний", 44
    )
    period = _short_label(payload.get("period_label") or "выбранный период", 56)
    created = str(payload.get("created_at") or "")
    comparison = payload.get("comparison_sequence") or []
    sections = set(resolve_report_sections(payload.get("sections")))

    # Header
    ax.add_patch(
        Rectangle((0, 0.885), 1, 0.115, facecolor=accent, edgecolor="none", alpha=0.96)
    )
    ax.text(
        0.060,
        0.965,
        report_title,
        fontsize=11.5,
        fontweight="bold",
        color="white",
        va="top",
        ha="left",
    )
    ax.text(
        0.060,
        0.935,
        project,
        fontsize=19,
        fontweight="bold",
        color="white",
        va="top",
        ha="left",
    )
    ax.text(
        0.060,
        0.904,
        period,
        fontsize=8.6,
        color="#e5e7eb",
        va="top",
        ha="left",
    )
    ax.text(0.935, 0.965, created, fontsize=8.0, color="#e5e7eb", va="top", ha="right")

    logo_img = _logo_image_from_payload(payload)
    if logo_img is not None:
        from matplotlib.patches import FancyBboxPatch

        box = FancyBboxPatch(
            (0.785, 0.902),
            0.150,
            0.055,
            boxstyle="round,pad=0.006,rounding_size=0.010",
            linewidth=0,
            facecolor="white",
            alpha=0.94,
        )
        ax.add_patch(box)
        logo_ax = fig.add_axes([0.795, 0.910, 0.130, 0.040])
        logo_ax.imshow(logo_img)
        logo_ax.axis("off")

    # Тело инфографики - курсор сверху вниз: каждый включённый блок рисуется
    # от текущего cursor и возвращает позицию, где должен начаться следующий
    # (зазор до следующего блока уже включён в возврат - см. комментарии в
    # каждой _draw_*_section). Выключенный блок просто не сдвигает курсор -
    # следующий встаёт на его место, без дыр.
    cursor = 0.862
    show_tags = "top_tags" in sections
    show_events = "top_events" in sections

    if "metrics" in sections:
        cursor = _draw_metrics_section(ax, payload, comparison, accent, cursor)

    if "sentiment" in sections:
        cursor = _draw_sentiment_section(ax, fig, payload, comparison, cursor)

    if show_tags or show_events:
        cursor = _draw_top_lists_section(
            ax, payload, cursor, show_tags=show_tags, show_events=show_events
        )

    if "top_sources" in sections and "metrics" not in sections:
        cursor = _draw_sources_section(ax, payload, cursor)

    if "highlights" in sections:
        cursor = _draw_highlights_section(ax, payload, cursor)

    if not (sections & _VISUAL_SECTIONS):
        ax.text(
            0.060,
            cursor,
            "Все аналитические блоки отключены в настройках выгрузки.",
            fontsize=9,
            color="#6b7280",
            va="top",
            ha="left",
        )

    footer_text = str(
        payload.get("footer_text")
        or "Инфографика сформирована автоматически на основе выбранного периода и текущего саммари."
    )
    ax.text(
        0.060,
        0.045,
        _short_label(footer_text, 110),
        fontsize=7.8,
        color="#6b7280",
        va="bottom",
        ha="left",
    )

    out = BytesIO()
    # Do not use bbox_inches="tight": it changes image geometry and can cause PDF scaling/cropping.
    fig.savefig(out, format="png", facecolor=background)
    plt.close(fig)
    return out.getvalue()
