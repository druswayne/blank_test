from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

import cv2
import qrcode
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, A6
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from ..models import TestBlank
from .answer_sheet_a6 import A6 as A6M
from .qr_service import make_qr_payload
from .questions_html_pdf import generate_questions_pdf_html

FONT_REGISTERED_NAME = "PdfSans"


def html_to_plain(text: str | None) -> str:
    """Текст для PDF: убираем HTML/разметку редактора, оставляем читаемую строку."""
    if not text:
        return ""
    s = unescape(str(text))
    s = re.sub(r"(?is)<script.*?>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?>.*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _find_unicode_font_path() -> Path | None:
    candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\arialuni.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    pkg = Path(__file__).resolve().parent.parent / "fonts" / "DejaVuSans.ttf"
    if pkg.is_file():
        return pkg
    return None


def _ensure_unicode_font() -> str:
    if FONT_REGISTERED_NAME in pdfmetrics.getRegisteredFontNames():
        return FONT_REGISTERED_NAME
    path = _find_unicode_font_path()
    if path is None:
        return "Helvetica"
    pdfmetrics.registerFont(TTFont(FONT_REGISTERED_NAME, str(path)))
    return FONT_REGISTERED_NAME


def _string_width(text: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, font, size)


def _wrap_line_to_width(text: str, font: str, size: float, max_w_pt: float) -> list[str]:
    if not text:
        return []
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w]) if cur else w
        if _string_width(trial, font, size) <= max_w_pt:
            cur.append(w)
            continue
        if cur:
            lines.append(" ".join(cur))
            cur = [w]
            if _string_width(w, font, size) <= max_w_pt:
                continue
            chunk = ""
            for ch in w:
                t2 = chunk + ch
                if _string_width(t2, font, size) <= max_w_pt:
                    chunk = t2
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            if chunk:
                cur = [chunk]
            else:
                cur = []
        else:
            chunk = ""
            for ch in w:
                t2 = chunk + ch
                if _string_width(t2, font, size) <= max_w_pt:
                    chunk = t2
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            if chunk:
                lines.append(chunk)
            cur = []
    if cur:
        lines.append(" ".join(cur))
    return lines


def _pt_per_line(font_size_pt: float) -> float:
    return font_size_pt * 1.35


def _draw_qr(
    c: canvas.Canvas,
    *,
    qr_payload: str,
    qr_left_mm: float,
    qr_top_mm: float,
    qr_size_mm: float,
    page_h_mm: float,
) -> None:
    qr_img = qrcode.make(qr_payload).convert("RGB")
    qr_reader = ImageReader(qr_img)
    qr_bottom_pt = (page_h_mm - (qr_top_mm + qr_size_mm)) * mm
    c.drawImage(qr_reader, qr_left_mm * mm, qr_bottom_pt, width=qr_size_mm * mm, height=qr_size_mm * mm, mask="auto")


def generate_questions_pdf(
    *,
    blank: TestBlank,
    pdf_dir: str,
    qr_secret: str,
    qr_payload_version: str,
) -> str:
    """A4: название, QR, вопросы с вариантами A–D — HTML из редактора (Quill) в PDF через xhtml2pdf."""
    return generate_questions_pdf_html(
        blank=blank,
        pdf_dir=pdf_dir,
        qr_secret=qr_secret,
        qr_payload_version=qr_payload_version,
    )


def _layout_v3_for_answers(*, rows_meta: list[dict], options_left_mm: float, markers: list[dict]) -> dict:
    return {
        "version": 3,
        "sheet": "a6_answer",
        "page_w_mm": float(A6M.page_w_mm),
        "page_h_mm": float(A6M.page_h_mm),
        "layout": "horizontal",
        "checkbox_outer_mm": float(A6M.checkbox_outer_mm),
        "checkbox_gap_x_mm": float(A6M.checkbox_gap_x_mm),
        "checkbox_inner_inset_mm": float(A6M.checkbox_inner_inset_mm),
        "options_left_mm": float(options_left_mm),
        "qr_left_mm": float(A6M.page_w_mm - A6M.margin_mm - A6M.qr_size_mm),
        "qr_top_mm": float(A6M.qr_top_mm),
        "qr_size_mm": float(A6M.qr_size_mm),
        "aruco_dict": "DICT_4X4_50",
        "markers": markers,
        "rows": rows_meta,
    }


def generate_answers_pdf_a6(
    *,
    blank: TestBlank,
    pdf_dir: str,
    qr_secret: str,
    qr_payload_version: str,
) -> tuple[str, dict]:
    """
    A6: название, ФИО, класс, строка квадратиков для отметок учителя по вопросам, QR;
    задания столбиком — номер и 4 квадрата в строку (фикс. шаг).
    """
    _ensure_unicode_font()
    font_name = FONT_REGISTERED_NAME if FONT_REGISTERED_NAME in pdfmetrics.getRegisteredFontNames() else "Helvetica"

    pdf_path = Path(pdf_dir) / f"{blank.uuid}_answers.pdf"
    page_w_mm = A6M.page_w_mm
    page_h_mm = A6M.page_h_mm
    m = A6M.margin_mm
    qr_size = A6M.qr_size_mm
    qr_left_mm = page_w_mm - m - qr_size
    qr_top_mm = A6M.qr_top_mm

    qr_payload = make_qr_payload(
        version=qr_payload_version,
        blank_uuid=blank.uuid,
        secret=qr_secret,
    )

    n = blank.question_count
    row_h = A6M.row_height_mm
    outer = A6M.checkbox_outer_mm
    gap_x = A6M.checkbox_gap_x_mm

    c = canvas.Canvas(str(pdf_path), pagesize=A6)
    c.setTitle(f"Ответы — {blank.uuid}")
    c.setStrokeColor(colors.black)

    options_left_mm = m + A6M.number_block_mm
    checkbox_outer_pt = outer * mm
    labels = ["A", "B", "C", "D"]
    marker_size_mm = 6.0
    marker_offset_mm = 6.5
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    marker_px = 220
    rows_meta_first_page: list[dict] = []
    markers_first_page: list[dict] = []

    def _draw_answer_page(page_idx: int) -> tuple[list[dict], list[dict]]:
        _draw_qr(c, qr_payload=qr_payload, qr_left_mm=qr_left_mm, qr_top_mm=qr_top_mm, qr_size_mm=qr_size, page_h_mm=page_h_mm)

        title = html_to_plain((blank.title or "Тест").strip())
        title_max_w_pt = max(16.0, (qr_left_mm - m - 2.0) * mm)
        title_lines = _wrap_line_to_width(title, font_name, 10.0, title_max_w_pt)
        ty = (page_h_mm - 8.0) * mm
        c.setFont(font_name, 10.0)
        for line in title_lines[:4]:
            c.drawString(m * mm, ty, line)
            ty -= 11.0

        y_cursor_mm = 8.0 + len(title_lines[:4]) * 3.8 + 2.0
        lx = m * mm
        y_fio_pt = (page_h_mm - y_cursor_mm) * mm
        c.setFont(font_name, 8.0)
        # Линии ФИО/Класс ограничиваем до левого края QR, чтобы не было наложения.
        text_right_mm = qr_left_mm - 2.0
        c.drawString(lx, y_fio_pt, "ФИО:")
        fio_line_start_pt = lx + _string_width("ФИО: ", font_name, 8.0)
        fio_line_end_pt = text_right_mm * mm
        if fio_line_end_pt > fio_line_start_pt:
            c.line(fio_line_start_pt, y_fio_pt - 1.0, fio_line_end_pt, y_fio_pt - 1.0)
        y_cursor_mm += 5.0
        y_cls_pt = (page_h_mm - y_cursor_mm) * mm
        c.drawString(lx, y_cls_pt, "Класс:")
        cls_line_start_pt = lx + _string_width("Класс: ", font_name, 8.0)
        cls_line_end_pt = text_right_mm * mm
        if cls_line_end_pt > cls_line_start_pt:
            c.line(cls_line_start_pt, y_cls_pt - 1.0, cls_line_end_pt, y_cls_pt - 1.0)
        # 10px ↔ мм (96 dpi) — один коэффициент для отступов PDF
        px10_to_mm = 10.0 * 25.4 / 96.0
        # Отступ после «Класс:» до полоски с клетками учителя (на 10px меньше прежних 4 мм).
        y_cursor_mm += max(1.0, 4.0 - px10_to_mm)

        # Пустые квадраты под отметки учителя (верно/неверно); над каждым — номер вопроса по центру.
        teacher_top_mm = y_cursor_mm + 0.5
        teacher_label = "Для учителя:"
        teacher_label_font_pt = 6.6
        teacher_label_gap_mm = 1.1
        c.setFont(font_name, teacher_label_font_pt)
        teacher_label_w_mm = _string_width(teacher_label, font_name, teacher_label_font_pt) / mm
        teacher_squares_left_mm = m + teacher_label_w_mm + teacher_label_gap_mm
        usable_w_mm = max(12.0, (text_right_mm - teacher_squares_left_mm))
        square_gap_mm = 1.0 if n <= 24 else 0.65
        raw_sq = (usable_w_mm - max(0, n - 1) * square_gap_mm) / max(1, n)
        teacher_square_mm = max(2.3, min(float(A6M.checkbox_outer_mm), raw_sq))
        num_row_h_mm = 3.2
        gap_num_sq_mm = 0.75
        square_top_mm = teacher_top_mm + num_row_h_mm + gap_num_sq_mm
        teacher_num_font_pt = 5.5
        c.setFont(font_name, teacher_num_font_pt)
        # Базовая линия подписи так, чтобы текст был по центру квадратиков по вертикали.
        teacher_label_center_mm = square_top_mm + teacher_square_mm / 2.0
        teacher_label_baseline_mm = teacher_label_center_mm + (teacher_label_font_pt * 0.36 / mm)
        c.setFont(font_name, teacher_label_font_pt)
        c.drawString(m * mm, (page_h_mm - teacher_label_baseline_mm) * mm, teacher_label)
        c.setFont(font_name, teacher_num_font_pt)
        num_baseline_from_top_mm = teacher_top_mm + num_row_h_mm - 0.9
        y_num_pt = (page_h_mm - num_baseline_from_top_mm) * mm
        for idx in range(n):
            left_mm = teacher_squares_left_mm + idx * (teacher_square_mm + square_gap_mm)
            cx_pt = (left_mm + teacher_square_mm / 2.0) * mm
            c.drawCentredString(cx_pt, y_num_pt, str(idx + 1))
            x_sq = left_mm * mm
            y_sq = (page_h_mm - square_top_mm - teacher_square_mm) * mm
            c.rect(x_sq, y_sq, teacher_square_mm * mm, teacher_square_mm * mm, stroke=1, fill=0)
        c.setFont(font_name, 8.0)

        y_cursor_mm = square_top_mm + teacher_square_mm + 1.2
        teacher_square_bottom_mm = square_top_mm + teacher_square_mm

        header_bottom_mm = max(qr_top_mm + qr_size + 1.5, y_cursor_mm)
        # ArUco: верх листа метки (my) = content_top - marker_offset. Нижний край квадратов учителя
        # должен быть выше верха метки, иначе зона распознавания клеток A–D пересекается с «шапкой».
        # Зазор после квадратов учителя: базовый + 10px (96 dpi → мм)
        min_content_top_clear_markers_mm = (
            teacher_square_bottom_mm + marker_offset_mm + 0.5 + px10_to_mm
        )
        content_top_mm = max(
            header_bottom_mm + 2.0,
            min_content_top_clear_markers_mm,
        )

        rows_meta: list[dict] = []

        # Для drawString(y) — это базовая линия; смещение от центра глифа до базовой линии (~центр цифры).
        num_font_pt = 8.5
        baseline_from_visual_center_pt = num_font_pt * 0.38

        for i in range(n):
            y_row_top = content_top_mm + i * row_h
            y_outer_top = y_row_top + max(0.0, (row_h - outer) / 2.0)
            rows_meta.append({"index": i, "checkbox_anchor_top_mm": float(y_outer_top)})

            # Центр блока квадратов по вертикали (от верхнего края листа, мм) → совпадает с центром номера.
            y_center_from_top_mm = y_outer_top + outer / 2.0
            y_center_canvas = (page_h_mm - y_center_from_top_mm) * mm
            num_y_pt = y_center_canvas - baseline_from_visual_center_pt
            c.setFont(font_name, num_font_pt)
            c.drawString(m * mm, num_y_pt, f"{i + 1}.")

            for opt_index in range(4):
                x_outer = (options_left_mm + opt_index * (outer + gap_x)) * mm
                y_ob = (page_h_mm - y_outer_top - outer) * mm
                c.rect(x_outer, y_ob, checkbox_outer_pt, checkbox_outer_pt, stroke=1, fill=0)
                c.setFont(font_name, 6.5)
                cx = x_outer + checkbox_outer_pt / 2 - 1.5 * mm
                cy = y_ob + checkbox_outer_pt + 0.45 * mm
                c.drawString(cx, cy, labels[opt_index])
                c.setFont(font_name, num_font_pt)

        # ArUco-метки для точной геометрической калибровки на сервере
        grid_top_mm = content_top_mm
        grid_bottom_mm = content_top_mm + n * row_h
        grid_left_mm = options_left_mm
        grid_right_mm = options_left_mm + 3 * (outer + gap_x) + outer
        marker_defs = [
            ("tl", 11, grid_left_mm - marker_offset_mm, grid_top_mm - marker_offset_mm),
            ("tr", 12, grid_right_mm + marker_offset_mm - marker_size_mm, grid_top_mm - marker_offset_mm),
            ("bl", 13, grid_left_mm - marker_offset_mm, grid_bottom_mm + marker_offset_mm - marker_size_mm),
            ("br", 14, grid_right_mm + marker_offset_mm - marker_size_mm, grid_bottom_mm + marker_offset_mm - marker_size_mm),
        ]
        markers: list[dict] = []
        for name, marker_id, mx, my in marker_defs:
            marker_img = cv2.aruco.generateImageMarker(aruco_dict, marker_id, marker_px)
            pil_img = Image.fromarray(marker_img)
            yb = (page_h_mm - my - marker_size_mm) * mm
            c.drawImage(ImageReader(pil_img), mx * mm, yb, width=marker_size_mm * mm, height=marker_size_mm * mm, mask="auto")
            markers.append(
                {
                    "name": name,
                    "id": int(marker_id),
                    "x_mm": float(mx + marker_size_mm / 2.0),
                    "y_mm": float(my + marker_size_mm / 2.0),
                    "size_mm": float(marker_size_mm),
                }
            )

        last_bottom = content_top_mm + n * row_h + m
        if last_bottom > page_h_mm:
            c.setFont(font_name, 7.0)
            c.setFillColor(colors.red)
            c.drawString(m * mm, m * mm, "Предупреждение: не все задания поместились на лист.")
            c.setFillColor(colors.black)

        if page_idx == 0:
            rows_meta_first_page.extend(rows_meta)
            markers_first_page.extend(markers)
        return rows_meta, markers

    for page_idx in range(4):
        _draw_answer_page(page_idx)
        c.showPage()

    c.save()

    layout = _layout_v3_for_answers(
        rows_meta=rows_meta_first_page,
        options_left_mm=options_left_mm,
        markers=markers_first_page,
    )
    return str(pdf_path), layout


def generate_blank_pdfs(
    *,
    blank: TestBlank,
    pdf_dir: str,
    qr_secret: str,
    qr_payload_version: str,
) -> tuple[str, str, str]:
    """
    Генерирует два PDF: лист вопросов (A4) и бланк ответов (A6).
    Возвращает (путь_вопросы, путь_ответы, layout_json) — в layout только бланк A6 (проверка по нему).
    """
    pq = generate_questions_pdf(
        blank=blank, pdf_dir=pdf_dir, qr_secret=qr_secret, qr_payload_version=qr_payload_version
    )
    pa, layout = generate_answers_pdf_a6(
        blank=blank, pdf_dir=pdf_dir, qr_secret=qr_secret, qr_payload_version=qr_payload_version
    )
    layout_str = json.dumps(layout, ensure_ascii=False)
    return pq, pa, layout_str
