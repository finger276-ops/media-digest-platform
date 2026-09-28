# -*- coding: utf-8 -*-
"""Отчёт в PDF: карточки, тональность, топы, «Главное», текст саммари.

Шрифт с кириллицей ищется среди установленных (_pdf_font_candidates):
встроенные шрифты reportlab русский текст не рисуют.
"""

from __future__ import annotations

import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Any


from .chart_style import SENTIMENT_COLOR_RANGE
from .metrics_compute import (
    NO_SENTIMENT_REASON,
    format_int,
    percent_text,
)
from .project_settings import valid_hex_color
from .report_common import (
    _adaptive_font_size,
    _classify_summary_line,
    _export_sentiment,
    _logo_image_from_payload,
    message_types_line,
    _pdf_metric_cards,
    resolve_report_sections,
    _short_label,
    top_sources_lines,
)

LOGGER = logging.getLogger("platform.report_export")


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


def _pdf_bold_font_name(font_name: str) -> str:
    """Имя жирного варианта, если он реально зарегистрирован - иначе тот же
    обычный шрифт (жирный текст останется обычным, не подменится Helvetica)."""
    from reportlab.pdfbase import pdfmetrics

    bold_name = f"{font_name}-Bold"
    try:
        pdfmetrics.getFont(bold_name)
        return bold_name
    except Exception:  # noqa: BLE001 - нет жирного варианта, работаем без него
        return font_name


class _PdfMetricsBlock:
    """Строка из 4 карточек метрик - те же данные и цвета, что на месте PNG
    раньше, но нарисованы напрямую на canvas PDF, тем же шрифтом, что и
    остальной текст страницы."""

    def __init__(self, cards, subtitle, accent_color, ink_color, muted_color, font_name, bold_font_name):
        self.cards = cards  # [(title, value, delta_subtitle), ...] до 4 штук
        self.subtitle = subtitle
        self.accent_color = accent_color
        self.ink_color = ink_color
        self.muted_color = muted_color
        self.font_name = font_name
        self.bold_font_name = bold_font_name

    def height(self, width):
        from reportlab.lib.units import cm

        return (0.5 * cm if self.subtitle else 0) + 2 * (2.05 * cm) + 0.3 * cm

    def draw(self, canv, x, y, width):
        """Рисует блок так, что (x, y) - левый ВЕРХНИЙ угол; возвращает y низа блока."""
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.units import cm

        gap = 0.3 * cm
        card_w = (width - gap) / 2
        card_h = 2.05 * cm
        top = y
        if self.subtitle:
            canv.setFillColor(self.muted_color)
            canv.setFont(self.font_name, 8.2)
            canv.drawString(x, top - 10, self.subtitle)
            top -= 0.5 * cm
        positions = [
            (x, top - card_h),
            (x + card_w + gap, top - card_h),
            (x, top - card_h - gap - card_h),
            (x + card_w + gap, top - card_h - gap - card_h),
        ]
        for (cx, cy), (card_title, value, subtitle) in zip(positions, self.cards):
            canv.setFillColor(rl_colors.HexColor("#f8fafc"))
            canv.setStrokeColor(rl_colors.HexColor("#d9e0ea"))
            canv.setLineWidth(0.7)
            canv.roundRect(cx, cy, card_w, card_h, 6, stroke=1, fill=1)
            canv.setStrokeColor(self.accent_color)
            canv.setLineWidth(2.2)
            canv.line(cx + 10, cy + 10, cx + 10, cy + card_h - 10)
            canv.setFillColor(rl_colors.HexColor("#4b5563"))
            canv.setFont(self.font_name, 9)
            canv.drawString(cx + 20, cy + card_h - 20, card_title)
            canv.setFillColor(self.ink_color)
            size = _adaptive_font_size(value, base=17, min_size=11)
            canv.setFont(self.bold_font_name, size)
            canv.drawString(cx + 20, cy + card_h / 2 - 6, value)
            if subtitle:
                canv.setFillColor(self.muted_color)
                canv.setFont(self.font_name, 7.4)
                canv.drawString(cx + 20, cy + 10, f"к пред. периоду: {subtitle}")
        return top - card_h - gap - card_h


class _PdfSentimentBlock:
    """Донат-диаграмма тональности + легенда - те же цвета, что и на живом
    дашборде (SENTIMENT_COLOR_RANGE), нарисованные через canvas.wedge вместо
    растровой картинки."""

    HEIGHT_CM = 3.6

    def __init__(self, values, colors_hex, labels, total, background_color, font_name, bold_font_name):
        self.values = values
        self.colors_hex = colors_hex
        self.labels = labels
        self.total = max(1, total)
        self.background_color = background_color
        self.font_name = font_name
        self.bold_font_name = bold_font_name

    def height(self, width):
        from reportlab.lib.units import cm

        return self.HEIGHT_CM * cm

    def draw(self, canv, x, y, width):
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.units import cm

        block_h = self.height(width)
        r = block_h / 2 - 0.15 * cm
        cx, cy = x + r + 0.2 * cm, y - block_h / 2
        total_v = sum(self.values) or 1
        start = 90.0
        any_positive = any(v > 0 for v in self.values)
        colors_list = self.colors_hex if any_positive else ["#d1d5db"]
        values = self.values if any_positive else [1]
        for val, col in zip(values, colors_list):
            if val <= 0:
                continue
            extent = -360.0 * (val / total_v)
            canv.setFillColor(rl_colors.HexColor(col))
            canv.setStrokeColor(self.background_color)
            canv.setLineWidth(1.4)
            canv.wedge(cx - r, cy - r, cx + r, cy + r, start, extent, stroke=1, fill=1)
            start += extent
        canv.setFillColor(self.background_color)
        hole_r = r * 0.56
        canv.circle(cx, cy, hole_r, stroke=0, fill=1)
        canv.setFillColor(rl_colors.HexColor("#111827"))
        canv.setFont(self.bold_font_name, 13)
        canv.drawCentredString(cx, cy + 2, format_int(self.total))
        canv.setFillColor(rl_colors.HexColor("#6b7280"))
        canv.setFont(self.font_name, 7)
        canv.drawCentredString(cx, cy - 11, "сообщений")

        legend_x = cx + r + 1.1 * cm
        row_h = block_h / max(1, len(self.labels))
        ly = y - row_h / 2 + 3
        for lab, val, col in zip(self.labels, self.values, self.colors_hex):
            canv.setFillColor(rl_colors.HexColor(col))
            canv.rect(legend_x, ly - 5, 9, 9, stroke=0, fill=1)
            canv.setFillColor(rl_colors.HexColor("#111827"))
            canv.setFont(self.font_name, 9.2)
            canv.drawString(legend_x + 15, ly - 4, lab)
            canv.setFont(self.bold_font_name, 9.2)
            canv.drawRightString(legend_x + 3.9 * cm, ly - 4, format_int(val))
            canv.setFillColor(rl_colors.HexColor("#6b7280"))
            canv.setFont(self.font_name, 8.4)
            canv.drawString(legend_x + 4.05 * cm, ly - 4, percent_text(val, self.total))
            ly -= row_h
        return y - block_h


class _PdfTopListsBlock:
    """«Топ тегов» / «Топ инфоповодов» - две колонки, рисуются напрямую, без
    ручного textwrap: короткие строки, перенос не нужен (см. _short_label)."""

    HEIGHT_CM = 4.1

    def __init__(self, tags, events, show_tags, show_events, ink_color, muted_color, font_name, bold_font_name):
        self.tags = tags
        self.events = events
        self.show_tags = show_tags
        self.show_events = show_events
        self.ink_color = ink_color
        self.muted_color = muted_color
        self.font_name = font_name
        self.bold_font_name = bold_font_name

    def height(self, width):
        from reportlab.lib.units import cm

        return self.HEIGHT_CM * cm

    def _draw_column(self, canv, x, top, col_w, heading_text, items, name_key, count_label_fmt):

        canv.setFillColor(self.ink_color)
        canv.setFont(self.bold_font_name, 11)
        canv.drawString(x, top - 12, heading_text)
        y = top - 32
        if items:
            for item in items[:5]:
                canv.setFillColor(self.ink_color)
                canv.setFont(self.font_name, 8.6)
                canv.drawString(x, y, f"• {_short_label(item.get('name'), 30)}")
                canv.setFillColor(self.muted_color)
                canv.setFont(self.font_name, 7.8)
                canv.drawRightString(
                    x + col_w, y, count_label_fmt(item)
                )
                y -= 17
        else:
            canv.setFillColor(self.muted_color)
            canv.setFont(self.font_name, 8.6)
            canv.drawString(x, y, "Нет данных для отображения")
        return y

    def draw(self, canv, x, y, width):
        from reportlab.lib.units import cm

        gap = 0.6 * cm
        col_w = (width - gap) / 2
        if self.show_tags:
            self._draw_column(
                canv,
                x,
                y,
                col_w,
                "Топ тегов",
                self.tags,
                "name",
                lambda item: f"{format_int(item.get('messages', 0))} сообщ.",
            )
        if self.show_events:
            self._draw_column(
                canv,
                x + col_w + gap,
                y,
                col_w,
                "Топ инфоповодов",
                self.events,
                "name",
                lambda item: f"{format_int(item.get('messages', 0))} сообщ.",
            )
        return y - self.height(width)


def generate_summary_pdf(payload: dict[str, Any]) -> bytes:
    try:
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
        from reportlab.platypus.flowables import Flowable
        from xml.sax.saxutils import escape as xml_escape
    except Exception as exc:
        raise RuntimeError(
            "Для выгрузки PDF добавьте reportlab в requirements.txt."
        ) from exc

    font_name = _pdf_font_name()
    bold_font_name = _pdf_bold_font_name(font_name)
    accent = valid_hex_color(payload.get("accent_color"), "#2563eb")
    background = valid_hex_color(payload.get("background_color"), "#ffffff")
    accent_color = rl_colors.HexColor(accent)
    background_color = rl_colors.HexColor(background)
    ink_color = rl_colors.HexColor("#111827")
    muted_color = rl_colors.HexColor("#6b7280")

    sections = set(resolve_report_sections(payload.get("sections")))
    total = max(1, int(payload.get("total", 0) or 0))
    comparison = payload.get("comparison_sequence") or []

    # --- шапка/футер: рисуются на КАЖДОЙ странице через onPage-колбэк, а не
    # один раз как первая страница-картинка - если саммари длинное и уходит
    # на страницу 2+, брендирование не теряется. ---
    report_title_text = _short_label(
        payload.get("report_title") or "Дайджест упоминаний", 60
    )
    project_text = _short_label(
        payload.get("client_name") or payload.get("project_name") or "Проект", 46
    )
    meta_text = str(payload.get("period_label") or "выбранный период")
    created_text = str(payload.get("created_at") or "")
    footer_text = _short_label(
        str(
            payload.get("footer_text")
            or "Сформировано автоматически на основе выбранного периода и текущего саммари."
        ),
        140,
    )
    logo_img = _logo_image_from_payload(payload)
    HEADER_H = 2.7 * cm
    FOOTER_H = 0.9 * cm
    MARGIN = 1.7 * cm

    def _draw_page_frame(canv, doc):
        canv.saveState()
        page_w, page_h = doc.pagesize
        canv.setFillColor(background_color)
        canv.rect(0, 0, page_w, page_h, stroke=0, fill=1)
        canv.setFillColor(accent_color)
        canv.rect(0, page_h - HEADER_H, page_w, HEADER_H, stroke=0, fill=1)
        canv.setFillColor(rl_colors.white)
        canv.setFont(font_name, 10)
        canv.drawString(MARGIN, page_h - 0.85 * cm, report_title_text)
        canv.setFont(bold_font_name, 15)
        canv.drawString(MARGIN, page_h - 1.55 * cm, project_text)
        canv.setFont(font_name, 8.3)
        canv.setFillColor(rl_colors.HexColor("#e5e7eb"))
        canv.drawString(MARGIN, page_h - 2.05 * cm, meta_text)
        canv.setFont(font_name, 7.8)
        canv.drawRightString(page_w - MARGIN, page_h - 0.85 * cm, created_text)
        if logo_img is not None:
            try:
                from reportlab.lib.utils import ImageReader

                logo_w, logo_h = 2.6 * cm, 1.0 * cm
                canv.drawImage(
                    ImageReader(logo_img),
                    page_w - MARGIN - logo_w,
                    page_h - 2.0 * cm,
                    width=logo_w,
                    height=logo_h,
                    mask="auto",
                    preserveAspectRatio=True,
                    anchor="c",
                )
            except Exception:  # noqa: BLE001 - без логотипа PDF всё равно нужен
                LOGGER.warning("Логотип не встроился в PDF", exc_info=True)
        canv.setFillColor(muted_color)
        canv.setFont(font_name, 7.6)
        canv.drawString(MARGIN, FOOTER_H / 2, footer_text)
        canv.restoreState()

    out = BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=HEADER_H + 0.5 * cm,
        bottomMargin=FOOTER_H + 0.4 * cm,
    )
    base = getSampleStyleSheet()
    normal = ParagraphStyle(
        "PlatformNormal",
        parent=base["Normal"],
        fontName=font_name,
        fontSize=10,
        leading=14,
        textColor=ink_color,
    )
    heading = ParagraphStyle(
        "PlatformHeading",
        parent=normal,
        fontName=bold_font_name,
        fontSize=12,
        leading=16,
        spaceBefore=4,
        spaceAfter=6,
        textColor=accent_color,
    )
    # Подзаголовки ВНУТРИ текста саммари ("## ..." от build_auto_summary) -
    # на ступень мельче heading, чтобы не спорить визуально с заголовками
    # разделов отчёта ("Саммари периода" и т.д.).
    subheading = ParagraphStyle(
        "PlatformSubheading",
        parent=normal,
        fontName=bold_font_name,
        fontSize=10.5,
        leading=14,
        spaceBefore=8,
        spaceAfter=3,
        textColor=accent_color,
    )
    bullet_style = ParagraphStyle(
        "PlatformBullet",
        parent=normal,
        leftIndent=12,
        bulletIndent=0,
        spaceAfter=2,
    )

    # Обёртка, которая просто зовёт draw(canv, x, top, width) у одного из
    # блоков выше - так каждый блок (карточки/донат/топ-списки) остаётся
    # ОБЫЧНЫМ платипус-флоублом и участвует в общей вёрстке страницы:
    # не помещается - переносится на следующую, как любой другой абзац.
    class _BlockFlowable(Flowable):
        def __init__(self, block):
            Flowable.__init__(self)
            self.block = block
            self.width = 0
            self._h = 0

        def wrap(self, avail_width, avail_height):
            self.width = avail_width
            self._h = self.block.height(avail_width)
            return self.width, self._h

        def draw(self):
            self.block.draw(self.canv, 0, self._h, self.width)

    story: list[Any] = []

    if "metrics" in sections:
        cards, subtitle = _pdf_metric_cards(payload)
        story.append(
            _BlockFlowable(
                _PdfMetricsBlock(cards, subtitle, accent_color, ink_color, muted_color, font_name, bold_font_name)
            )
        )
        types_line = message_types_line(payload)
        if types_line:
            story.append(Spacer(1, 4))
            story.append(Paragraph(xml_escape(types_line), normal))
        story.append(Spacer(1, 10))

    if "sentiment" in sections:
        pos, neu, neg, sent_total, unmarked = _export_sentiment(payload, comparison)
        sent_total = max(1, sent_total)
        story.append(Paragraph("Тональность", heading))
        if unmarked:
            # Донат «Нейтрал 100 %» выглядел бы как «негатива нет».
            muted_style = ParagraphStyle(
                "PlatformMuted", parent=normal, fontSize=9, textColor=muted_color
            )
            story.append(Paragraph(xml_escape(NO_SENTIMENT_REASON), muted_style))
        else:
            story.append(
                _BlockFlowable(
                    _PdfSentimentBlock(
                        [pos, neu, neg],
                        list(SENTIMENT_COLOR_RANGE),
                        ["Позитив", "Нейтрал", "Негатив"],
                        sent_total,
                        background_color,
                        font_name,
                        bold_font_name,
                    )
                )
            )
        story.append(Spacer(1, 10))

    show_tags = "top_tags" in sections
    show_events = "top_events" in sections
    if show_tags or show_events:
        story.append(
            _BlockFlowable(
                _PdfTopListsBlock(
                    payload.get("top_tags") or [],
                    payload.get("top_events") or [],
                    show_tags,
                    show_events,
                    ink_color,
                    muted_color,
                    font_name,
                    bold_font_name,
                )
            )
        )
        story.append(Spacer(1, 10))

    source_lines = top_sources_lines(payload)
    if "top_sources" in sections and source_lines:
        story.append(Paragraph("Площадки", heading))
        for line in source_lines:
            story.append(Paragraph("• " + xml_escape(line), normal))
        story.append(Spacer(1, 10))

    if "highlights" in sections:
        story.append(Paragraph("Главное", heading))
        highlights = (payload.get("summary_highlights") or [])[:4]
        for block in highlights:
            story.append(Paragraph("• " + xml_escape(str(block)), normal))
        story.append(Spacer(1, 6))

    if "summary_text" in sections:
        if story:
            story.append(PageBreak())
        story.append(Paragraph("Саммари периода", heading))
        for raw in str(payload.get("summary_text") or "").split("\n"):
            kind, text = _classify_summary_line(raw)
            if not text:
                continue
            if kind == "heading":
                story.append(Paragraph(xml_escape(text), subheading))
            elif kind == "bullet":
                story.append(Paragraph(xml_escape(text), bullet_style, bulletText="•"))
            else:
                story.append(Paragraph(xml_escape(text), normal))

    if not story:
        # Аналитик снял вообще все разделы - пустой PDF выглядел бы как баг,
        # а не как осознанный (пустой) выбор.
        story.append(
            Paragraph("Все разделы отчёта отключены в настройках выгрузки.", normal)
        )

    doc.build(story, onFirstPage=_draw_page_frame, onLaterPages=_draw_page_frame)
    return out.getvalue()
