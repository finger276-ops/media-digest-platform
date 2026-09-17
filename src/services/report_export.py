# -*- coding: utf-8 -*-
"""Генерация выгрузок саммари: инфографика PNG, Word, PDF.

Framework-independent (без Streamlit), как services/brand_metrics.py — платформа
только собирает данные в payload, а сама отрисовка/сборка документа не знает
про Streamlit. UI-обёртка (кнопки скачивания) — в report_export_ui.py.
"""

from __future__ import annotations

import logging
import os
import re
import textwrap
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from .chart_style import SENTIMENT_COLOR_RANGE
from .dashboard_config import REPORT_TEMPLATE_OPTIONS
from .cached_store import download_storage_file
from .metrics_compute import format_int, numeric_series, percent_text
from .observability import report_failure
from .project_settings import report_branding_from_project_settings, valid_hex_color
from .tag_compute import build_tag_statistics

LOGGER = logging.getLogger("platform.report_export")


def first_existing_col(df: pd.DataFrame, columns: list[str | None]) -> str | None:
    if df is None or not isinstance(df, pd.DataFrame):
        return None
    for col in columns or []:
        if col and col in df.columns:
            return str(col)
    return None


def clean_summary_for_export(text: str) -> str:
    value = str(text or "").strip()
    value = value.replace("**", "")
    value = re.sub(r"^\s*•\s*", "", value, flags=re.MULTILINE)
    return value


def safe_export_filename(project_name: str, period_label: str, ext: str) -> str:
    raw = f"summary_{project_name}_{period_label}"
    safe = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", raw, flags=re.UNICODE).strip("_")
    return f"{safe[:140] or 'summary'}.{ext}"


def export_top_tags(
    messages: pd.DataFrame | None, limit: int = 5
) -> list[dict[str, Any]]:
    if messages is None or not isinstance(messages, pd.DataFrame) or messages.empty:
        return []
    try:
        stats = build_tag_statistics(messages).head(limit).copy()
    except Exception:  # noqa: BLE001 — выгрузка без топ-тегов лучше, чем никакая
        LOGGER.warning("Топ-теги для выгрузки не посчитались", exc_info=True)
        return []
    result: list[dict[str, Any]] = []
    for _, row in stats.iterrows():
        tag = str(row.get("Тег") or "").strip()
        if not tag:
            continue
        result.append(
            {
                "name": tag,
                "messages": int(row.get("Сообщений", 0) or 0),
                "audience": int(row.get("Аудитория", 0) or 0),
                "reach": int(row.get("Охват", 0) or 0),
                "engagement": int(row.get("Вовлеченность", 0) or 0),
            }
        )
    return result


def export_top_events(
    events: pd.DataFrame | None, limit: int = 5
) -> list[dict[str, Any]]:
    if events is None or not isinstance(events, pd.DataFrame) or events.empty:
        return []
    work = events.copy()
    title_col = first_existing_col(
        work, ["display_title", "event_title", "title", "Сюжет / инфоповод", "Сюжет"]
    )
    if title_col is None:
        return []
    work["_title"] = work[title_col].fillna("").astype(str).str.strip()
    # Технические категории не должны попадать в клиентскую инфографику.
    work = work[
        (work["_title"] != "")
        & (
            ~work["_title"]
            .str.lower()
            .isin({"без сюжета", "без_сюжета", "без темы", "прочее"})
        )
    ]
    if work.empty:
        return []
    count_col = first_existing_col(work, ["message_count", "messages", "Сообщений"])
    reach_col = first_existing_col(
        work, ["views", "reach", "Охват", "Просмотры", "Просмотров"]
    )
    engagement_col = first_existing_col(
        work, ["engagement", "Вовлеченность", "Вовлечённость"]
    )
    work["_messages"] = (
        numeric_series(work, [count_col]).astype(int) if count_col else 0
    )
    work["_reach"] = numeric_series(work, [reach_col]).astype(int) if reach_col else 0
    work["_engagement"] = (
        numeric_series(work, [engagement_col]).astype(int) if engagement_col else 0
    )
    work = work.sort_values(
        ["_messages", "_reach", "_engagement"], ascending=False
    ).head(limit)
    result: list[dict[str, Any]] = []
    for _, row in work.iterrows():
        result.append(
            {
                "name": str(row.get("_title") or "").strip(),
                "messages": int(row.get("_messages", 0) or 0),
                "reach": int(row.get("_reach", 0) or 0),
                "engagement": int(row.get("_engagement", 0) or 0),
            }
        )
    return result


def summary_highlights(summary_text: str, limit: int = 4) -> list[str]:
    lines: list[str] = []
    for raw in str(summary_text or "").replace("\r", "\n").split("\n"):
        line = raw.strip().strip("•-").strip()
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= limit:
            break
    if lines:
        return lines
    return ["Саммари пока не заполнено."]


def _load_report_logo_bytes(branding: dict[str, Any] | None) -> bytes:
    """Load logo bytes from Storage path or, as fallback, from public URL."""
    branding = branding or {}
    storage_path = str(branding.get("logo_storage_path") or "").strip()
    if storage_path:
        try:
            return download_storage_file(storage_path)
        except Exception:  # noqa: BLE001 — ниже есть запасной путь по ссылке
            LOGGER.warning("Логотип из Storage не скачался: %s", storage_path)
    url = str(branding.get("logo_url") or "").strip()
    if url.startswith(("http://", "https://")):
        try:
            from urllib.request import urlopen

            with urlopen(
                url, timeout=6
            ) as response:  # nosec - user-provided report asset URL
                return response.read()
        except Exception:  # noqa: BLE001 — отчёт без логотипа лучше, чем без отчёта
            LOGGER.warning("Логотип по ссылке не скачался: %s", url)
            return b""
    return b""


def _logo_image_from_payload(payload: dict[str, Any]):
    logo_bytes = payload.get("logo_bytes") or b""
    if not logo_bytes:
        return None
    try:
        from PIL import Image as PILImage

        return PILImage.open(BytesIO(logo_bytes)).convert("RGBA")
    except Exception:  # noqa: BLE001 — битая картинка не должна ломать выгрузку
        LOGGER.warning("Логотип не открылся как изображение", exc_info=True)
        return None


def summary_export_payload(
    project_name: str,
    period_label: str,
    summary_text: str,
    metrics: dict[str, Any],
    messages: pd.DataFrame | None = None,
    events_agg: pd.DataFrame | None = None,
    *,
    report_template: str = "summary",
    branding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sent = metrics.get("sentiment", {}) if isinstance(metrics, dict) else {}
    report_template = (
        report_template if report_template in REPORT_TEMPLATE_OPTIONS else "summary"
    )
    branding = report_branding_from_project_settings(
        {"report_branding": branding or {}}, project_name=project_name
    )
    return {
        "project_name": project_name,
        "client_name": branding.get("client_name") or project_name,
        "report_title": branding.get("report_title") or "Дайджест упоминаний",
        "accent_color": branding.get("accent_color") or "#2563eb",
        "background_color": branding.get("background_color") or "#ffffff",
        "footer_text": branding.get("footer_text") or "",
        "logo_url": branding.get("logo_url") or "",
        "logo_storage_path": branding.get("logo_storage_path") or "",
        "logo_filename": branding.get("logo_filename") or "",
        "logo_mime_type": branding.get("logo_mime_type") or "",
        "logo_bytes": _load_report_logo_bytes(branding),
        "report_template": report_template,
        "report_template_label": REPORT_TEMPLATE_OPTIONS.get(
            report_template, report_template
        ),
        "period_label": period_label,
        "summary_text": clean_summary_for_export(summary_text),
        "summary_highlights": summary_highlights(summary_text),
        "messages": int(metrics.get("messages", 0) or 0),
        "audience": int(metrics.get("audience", 0) or 0),
        "reach": int(metrics.get("reach", 0) or 0),
        "engagement": int(metrics.get("engagement", 0) or 0),
        "positive": int(sent.get("positive", 0) or 0),
        "neutral": int(sent.get("neutral", 0) or 0),
        "negative": int(sent.get("negative", 0) or 0),
        "total": int(sent.get("total", 0) or 0),
        "comparison_sequence": metrics.get("comparison_sequence") or [],
        "top_tags": export_top_tags(
            messages, limit=8 if report_template == "full" else 5
        ),
        "top_events": export_top_events(
            events_agg, limit=8 if report_template == "full" else 5
        ),
        "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }


def _short_label(value: Any, max_len: int = 34) -> str:
    text = str(value or "").strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def _metric_delta_for_export(current: Any, previous: Any) -> str:
    try:
        cur = float(current or 0)
        prev = float(previous or 0)
    except (TypeError, ValueError):
        return ""
    diff = cur - prev
    if prev:
        pct = diff / abs(prev) * 100
        return f"{diff:+,.0f} / {pct:+.1f}%".replace(",", " ")
    if diff:
        return f"{diff:+,.0f}".replace(",", " ")
    return "0"


def _adaptive_font_size(value: str, *, base: int = 15, min_size: int = 10) -> int:
    """Keep large metric values readable inside report cards."""
    length = len(str(value or ""))
    if length > 15:
        return max(min_size, base - 5)
    if length > 12:
        return max(min_size, base - 4)
    if length > 9:
        return max(min_size, base - 2)
    return base


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
    template_label = _short_label(payload.get("report_template_label") or "", 34)
    period = _short_label(payload.get("period_label") or "выбранный период", 56)
    created = str(payload.get("created_at") or "")
    comparison = payload.get("comparison_sequence") or []

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
        f"{template_label} · {period}".strip(" ·"),
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

    # Metrics: two rows, enough height for large numbers and deltas.
    if len(comparison) >= 2:
        previous, current = comparison[-2], comparison[-1]
        metric_cards = [
            (
                "Сообщения",
                current.get("messages", 0),
                _metric_delta_for_export(
                    current.get("messages", 0), previous.get("messages", 0)
                ),
            ),
            (
                "Аудитория",
                current.get("audience", 0),
                _metric_delta_for_export(
                    current.get("audience", 0), previous.get("audience", 0)
                ),
            ),
            (
                "Охват",
                current.get("reach", 0),
                _metric_delta_for_export(
                    current.get("reach", 0), previous.get("reach", 0)
                ),
            ),
            (
                "Вовлеченность",
                current.get("engagement", 0),
                _metric_delta_for_export(
                    current.get("engagement", 0), previous.get("engagement", 0)
                ),
            ),
        ]
        ax.text(
            0.060,
            0.862,
            f"Последний период: {_short_label(current.get('label'), 48)}",
            fontsize=8.2,
            color="#6b7280",
            va="top",
            ha="left",
        )
    else:
        metric_cards = [
            ("Сообщения", payload.get("messages", 0), ""),
            ("Аудитория", payload.get("audience", 0), ""),
            ("Охват", payload.get("reach", 0), ""),
            ("Вовлеченность", payload.get("engagement", 0), ""),
        ]

    xs = [0.060, 0.525]
    ys = [0.755, 0.635]
    for idx, (title, value, subtitle) in enumerate(metric_cards):
        _draw_export_card(
            ax,
            xs[idx % 2],
            ys[idx // 2],
            0.405,
            0.095,
            title,
            format_int(value),
            f"к пред. периоду: {subtitle}" if subtitle else "",
            accent_color=accent,
        )

    # Sentiment block
    total = max(1, int(payload.get("total", 0) or 0))
    pos = int(payload.get("positive", 0) or 0)
    neu = int(payload.get("neutral", 0) or 0)
    neg = int(payload.get("negative", 0) or 0)
    if len(comparison) >= 2:
        sent = comparison[-1].get("sentiment", {}) or {}
        total = max(1, int(sent.get("total", 0) or 0))
        pos = int(sent.get("positive", 0) or 0)
        neu = int(sent.get("neutral", 0) or 0)
        neg = int(sent.get("negative", 0) or 0)

    ax.text(
        0.060,
        0.585,
        "Тональность",
        fontsize=12,
        fontweight="bold",
        color="#111827",
        va="top",
        ha="left",
    )
    pie_ax = fig.add_axes([0.070, 0.427, 0.220, 0.145])
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

    y0 = 0.545
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

    # Top lists: safer fixed columns and shorter labels to avoid overlap.
    top_tags = payload.get("top_tags") or []
    top_events = payload.get("top_events") or []
    ax.text(
        0.060,
        0.382,
        "Топ тегов",
        fontsize=11.5,
        fontweight="bold",
        color="#111827",
        va="top",
        ha="left",
    )
    y = 0.354
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

    ax.text(
        0.525,
        0.382,
        "Топ инфоповодов",
        fontsize=11.5,
        fontweight="bold",
        color="#111827",
        va="top",
        ha="left",
    )
    y = 0.354
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

    # Summary highlights: limited lines with consistent spacing.
    ax.text(
        0.060,
        0.205,
        "Главное",
        fontsize=11.5,
        fontweight="bold",
        color="#111827",
        va="top",
        ha="left",
    )
    summary_y = 0.178
    line_count = 0
    for block in (payload.get("summary_highlights") or [])[:4]:
        wrapped = textwrap.wrap(str(block), width=86) or [str(block)]
        bullet = True
        for seg in wrapped[:2]:
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


def generate_summary_docx(payload: dict[str, Any]) -> bytes:
    try:
        from docx import Document
        from docx.shared import Pt, Inches, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except Exception as exc:
        raise RuntimeError(
            "Для выгрузки Word добавьте python-docx в requirements.txt."
        ) from exc

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(1.6)
    section.bottom_margin = Cm(1.6)
    section.left_margin = Cm(1.7)
    section.right_margin = Cm(1.7)

    styles = doc.styles
    styles["Normal"].font.name = "Arial"
    styles["Normal"].font.size = Pt(10.5)
    styles["Heading 1"].font.name = "Arial"
    styles["Heading 1"].font.size = Pt(16)
    styles["Heading 2"].font.name = "Arial"
    styles["Heading 2"].font.size = Pt(13)

    p = doc.add_paragraph()
    r = p.add_run(str(payload.get("report_title") or "Дайджест упоминаний"))
    r.bold = True
    r.font.size = Pt(16)
    p.paragraph_format.space_after = Pt(4)

    p2 = doc.add_paragraph()
    r2 = p2.add_run(
        str(payload.get("client_name") or payload.get("project_name") or "Проект")
    )
    r2.bold = True
    r2.font.size = Pt(13)
    p2.paragraph_format.space_after = Pt(6)

    meta = doc.add_paragraph()
    meta.add_run(f"Шаблон: {payload.get('report_template_label') or ''}\n")
    meta.add_run(f"Период: {payload.get('period_label') or 'выбранный период'}\n")
    meta.add_run(f"Дата выгрузки: {payload.get('created_at') or ''}")
    meta.paragraph_format.space_after = Pt(8)

    try:
        infographic_png = generate_summary_infographic_png(payload)
        pic_p = doc.add_paragraph()
        pic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = pic_p.add_run()
        run.add_picture(BytesIO(infographic_png), width=Inches(6.4))
        doc.add_page_break()
    except Exception as exc:  # noqa: BLE001 — Word без инфографики лучше, чем без Word
        # Клиент получит документ и не узнает, что страницы не хватает, —
        # поэтому владелец должен узнать вместо него.
        LOGGER.warning("Инфографика для Word не собралась", exc_info=True)
        report_failure("выгрузка Word: инфографика не собралась", exc)

    total = max(1, int(payload.get("total", 0) or 0))
    doc.add_heading("Основные метрики", level=2)
    doc.add_paragraph(
        f"Сообщений — {format_int(payload.get('messages', 0))}; "
        f"аудитория — {format_int(payload.get('audience', 0))}; "
        f"охват — {format_int(payload.get('reach', 0))}; "
        f"вовлеченность — {format_int(payload.get('engagement', 0))}."
    )
    doc.add_paragraph(
        f"Тональность: позитив — {percent_text(int(payload.get('positive', 0) or 0), total)}; "
        f"нейтрал — {percent_text(int(payload.get('neutral', 0) or 0), total)}; "
        f"негатив — {percent_text(int(payload.get('negative', 0) or 0), total)}."
    )

    if payload.get("report_template") in {"client_overview", "comparison", "full"}:
        doc.add_heading("Что включить в отчет", level=2)
        top_tags = payload.get("top_tags") or []
        top_events = payload.get("top_events") or []
        if top_tags:
            doc.add_paragraph(
                "Топ тегов: "
                + "; ".join(
                    str(x.get("name") or "") for x in top_tags[:5] if x.get("name")
                )
            )
        if top_events:
            doc.add_paragraph(
                "Топ инфоповодов: "
                + "; ".join(
                    str(x.get("name") or "") for x in top_events[:5] if x.get("name")
                )
            )

    doc.add_heading("Саммари периода", level=2)
    for block in str(payload.get("summary_text") or "").split("\n"):
        block = block.strip()
        if block:
            para = doc.add_paragraph(block)
            para.paragraph_format.space_after = Pt(4)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _pdf_font_candidates() -> list[tuple[str, str | None]]:
    """Return regular/bold TTF candidates that support Cyrillic.

    Streamlit Cloud images do not always include system DejaVu/Noto fonts. If we
    fall back to ReportLab core Helvetica, Cyrillic is rendered as black squares.
    To make PDF export portable, we also look for the DejaVu Sans bundled with
    matplotlib when the package is installed.
    """
    candidates: list[tuple[str, str | None]] = []

    env_path = os.getenv("PLATFORM_PDF_FONT_PATH", "").strip()
    if env_path:
        candidates.append((env_path, None))

    # Optional project-local fonts. Do not commit font files unless licensing is clear;
    # this path is only for private deployments that provide their own font.
    here = Path(__file__).resolve().parent.parent
    candidates.extend(
        [
            (
                str(here / "assets" / "fonts" / "DejaVuSans.ttf"),
                str(here / "assets" / "fonts" / "DejaVuSans-Bold.ttf"),
            ),
            (
                str(here.parent / "assets" / "fonts" / "DejaVuSans.ttf"),
                str(here.parent / "assets" / "fonts" / "DejaVuSans-Bold.ttf"),
            ),
        ]
    )

    # Common Linux/Streamlit Cloud system fonts.
    candidates.extend(
        [
            (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            ),
        ]
    )

    try:
        import matplotlib  # type: ignore

        mpl_fonts = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        candidates.append(
            (str(mpl_fonts / "DejaVuSans.ttf"), str(mpl_fonts / "DejaVuSans-Bold.ttf"))
        )
    except Exception:  # noqa: BLE001 — matplotlib необязателен, это запасной шрифт
        pass

    return candidates


def _pdf_font_name() -> str:
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception as exc:
        raise RuntimeError(
            "Для выгрузки PDF добавьте reportlab в requirements.txt."
        ) from exc

    for regular_path, bold_path in _pdf_font_candidates():
        regular = Path(regular_path)
        if not regular.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("PlatformSans", str(regular)))
            bold = Path(bold_path) if bold_path else None
            if bold is not None and bold.exists():
                pdfmetrics.registerFont(TTFont("PlatformSans-Bold", str(bold)))
                try:
                    pdfmetrics.registerFontFamily(
                        "PlatformSans",
                        normal="PlatformSans",
                        bold="PlatformSans-Bold",
                        italic="PlatformSans",
                        boldItalic="PlatformSans-Bold",
                    )
                except Exception:  # noqa: BLE001 — без семейства жирный заменится обычным
                    pass
            return "PlatformSans"
        except Exception:  # noqa: BLE001 — кандидат не подошёл, пробуем следующий
            continue

    raise RuntimeError(
        "Не найден TTF-шрифт с поддержкой кириллицы для PDF. "
        "Проверьте, что установлен matplotlib>=3.8 или задайте PLATFORM_PDF_FONT_PATH."
    )


def generate_summary_pdf(payload: dict[str, Any]) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Image,
            PageBreak,
        )
        from xml.sax.saxutils import escape as xml_escape
    except Exception as exc:
        raise RuntimeError(
            "Для выгрузки PDF добавьте reportlab в requirements.txt."
        ) from exc

    font_name = _pdf_font_name()
    out = BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=1.7 * cm,
        rightMargin=1.7 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    base = getSampleStyleSheet()
    normal = ParagraphStyle(
        "PlatformNormal",
        parent=base["Normal"],
        fontName=font_name,
        fontSize=10,
        leading=14,
    )
    title = ParagraphStyle(
        "PlatformTitle",
        parent=normal,
        fontName=font_name,
        fontSize=16,
        leading=20,
        spaceAfter=10,
    )
    heading = ParagraphStyle(
        "PlatformHeading",
        parent=normal,
        fontName=font_name,
        fontSize=12,
        leading=16,
        spaceBefore=8,
        spaceAfter=6,
    )

    story = []
    infographic_added = False
    try:
        infographic_png = generate_summary_infographic_png(payload)
        infographic_io = BytesIO(infographic_png)
        infographic_io.seek(0)
        # Инфографика теперь первая страница PDF, без дублирующей текстовой страницы.
        story.append(Image(infographic_io, width=17.2 * cm, height=24.35 * cm))
        story.append(PageBreak())
        infographic_added = True
    except Exception as exc:  # noqa: BLE001 — PDF без инфографики лучше, чем без PDF
        LOGGER.warning("Инфографика для PDF не собралась", exc_info=True)
        report_failure("выгрузка PDF: инфографика не собралась", exc)

    total = max(1, int(payload.get("total", 0) or 0))
    if not infographic_added:
        story.extend(
            [
                Paragraph(
                    f"<b>{xml_escape(str(payload.get('report_title') or 'Дайджест упоминаний'))}</b>",
                    title,
                ),
                Paragraph(
                    xml_escape(
                        str(
                            payload.get("client_name")
                            or payload.get("project_name")
                            or "Проект"
                        )
                    ),
                    heading,
                ),
                Paragraph(
                    xml_escape(f"Шаблон: {payload.get('report_template_label') or ''}"),
                    normal,
                ),
                Paragraph(
                    xml_escape(
                        f"Период: {payload.get('period_label') or 'выбранный период'}"
                    ),
                    normal,
                ),
                Paragraph(
                    xml_escape(f"Дата выгрузки: {payload.get('created_at') or ''}"),
                    normal,
                ),
                Spacer(1, 8),
                Paragraph("<b>Основные метрики</b>", heading),
                Paragraph(
                    xml_escape(
                        f"Сообщений — {format_int(payload.get('messages', 0))}; "
                        f"аудитория — {format_int(payload.get('audience', 0))}; "
                        f"охват — {format_int(payload.get('reach', 0))}; "
                        f"вовлеченность — {format_int(payload.get('engagement', 0))}."
                    ),
                    normal,
                ),
                Paragraph(
                    xml_escape(
                        f"Тональность: позитив — {percent_text(int(payload.get('positive', 0) or 0), total)}; "
                        f"нейтрал — {percent_text(int(payload.get('neutral', 0) or 0), total)}; "
                        f"негатив — {percent_text(int(payload.get('negative', 0) or 0), total)}."
                    ),
                    normal,
                ),
                Spacer(1, 8),
            ]
        )

    story.append(Paragraph("<b>Саммари периода</b>", heading))
    for block in str(payload.get("summary_text") or "").split("\n"):
        block = block.strip()
        if block:
            story.append(Paragraph(xml_escape(block), normal))
    doc.build(story)
    return out.getvalue()
