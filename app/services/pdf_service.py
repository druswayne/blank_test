from __future__ import annotations

import json
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
from .template_constants import METRICS

FONT_REGISTERED_NAME = "PdfSans"


def _find_unicode_font_path() -> Path | None:
    candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\arialuni.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
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


def _questions_payload(blank: TestBlank) -> list[dict]:
    return [
        {
            "question_text": q.question_text,
            "A": q.option_a,
            "B": q.option_b,
            "C": q.option_c,
            "D": q.option_d,
        }
        for q in sorted(blank.questions, key=lambda x: x.question_number)
    ]


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


def _compute_rows_questions_only(
    *,
    questions_payload: list[dict],
    font_name: str,
    font_size: float,
    margin_mm: float,
    text_right_mm: float,
    header_bottom_mm: float,
    page_h_mm: float,
) -> tuple[list[dict], float]:
    line_h_pt = _pt_per_line(font_size)
    text_x_mm = margin_mm + 6.0
    text_max_w_pt = max(30.0, (text_right_mm - text_x_mm - 2.0) * mm)
    rows: list[dict] = []
    y_mm = header_bottom_mm + 2.0
    for i, q in enumerate(questions_payload):
        q_lines = _wrap_line_to_width(q["question_text"], font_name, font_size + 0.5, text_max_w_pt)
        text_h_mm = len(q_lines) * (line_h_pt / mm)
        row_h_mm = max(text_h_mm + 2 * 3.0, 10.0)
        rows.append(
            {
                "index": i,
                "row_top_mm": float(y_mm),
                "row_bottom_mm": float(y_mm + row_h_mm),
                "_q_lines": q_lines,
                "_row_h_mm": float(row_h_mm),
            }
        )
        y_mm += row_h_mm + 1.2
    return rows, y_mm


def generate_questions_pdf(
    *,
    blank: TestBlank,
    pdf_dir: str,
    qr_secret: str,
    qr_payload_version: str,
) -> str:
    """A4: название, QR, таблица только с текстом вопросов (без вариантов и квадратов)."""
    _ensure_unicode_font()
    font_name = FONT_REGISTERED_NAME if FONT_REGISTERED_NAME in pdfmetrics.getRegisteredFontNames() else "Helvetica"

    pdf_path = Path(pdf_dir) / f"{blank.uuid}_questions.pdf"
    page_h_mm = METRICS.page_h_mm
    page_w_mm = METRICS.page_w_mm
    margin_mm = 8.0
    qr_size_mm = 20.0
    qr_left_mm = page_w_mm - margin_mm - qr_size_mm
    qr_top_mm = 6.0
    text_right_mm = page_w_mm - margin_mm
    bottom_limit_mm = page_h_mm - margin_mm

    qr_payload = make_qr_payload(
        version=qr_payload_version,
        blank_uuid=blank.uuid,
        secret=qr_secret,
    )
    questions_payload = _questions_payload(blank)

    def build(fs: float) -> None:
        c = canvas.Canvas(str(pdf_path), pagesize=A4)
        c.setTitle((blank.title or "Вопросы") + f" — {blank.uuid}")
        c.setStrokeColor(colors.black)

        _draw_qr(c, qr_payload=qr_payload, qr_left_mm=qr_left_mm, qr_top_mm=qr_top_mm, qr_size_mm=qr_size_mm, page_h_mm=page_h_mm)

        title = (blank.title or "Тестовый бланк").strip()
        title_max_w_pt = max(20.0, (qr_left_mm - margin_mm - 2.0) * mm)
        title_lines = _wrap_line_to_width(title, font_name, fs + 1.0, title_max_w_pt)
        title_y_pt = (page_h_mm - 10.0) * mm
        c.setFont(font_name, fs + 1.0)
        for line in title_lines:
            c.drawString(margin_mm * mm, title_y_pt, line)
            title_y_pt -= (fs + 1.0) * 1.35

        c.setFont(font_name, fs - 0.5)
        c.drawString(
            margin_mm * mm,
            (page_h_mm - 10.0 - len(title_lines) * 4.6 - 4.5) * mm,
            "Для ответов используйте отдельный бланк A6 с QR-кодом.",
        )

        header_bottom_mm = max(10.0 + len(title_lines) * 4.2 + 6.0, qr_top_mm + qr_size_mm + 2.0)

        rows, y_end = _compute_rows_questions_only(
            questions_payload=questions_payload,
            font_name=font_name,
            font_size=fs,
            margin_mm=margin_mm,
            text_right_mm=text_right_mm,
            header_bottom_mm=header_bottom_mm,
            page_h_mm=page_h_mm,
        )
        if rows and rows[-1]["row_bottom_mm"] > bottom_limit_mm:
            raise ValueError("overflow")

        for idx, row in enumerate(rows):
            row_top_mm = row["row_top_mm"]
            row_bottom_mm = row["row_bottom_mm"]
            row_h_mm = row["_row_h_mm"]
            q_lines = row["_q_lines"]

            x0 = margin_mm * mm
            y0 = (page_h_mm - row_bottom_mm) * mm
            w0 = (page_w_mm - 2 * margin_mm) * mm
            h0 = row_h_mm * mm
            c.rect(x0, y0, w0, h0, stroke=1, fill=0)

            inner_top_mm = row_top_mm + 3.0
            y_text_pt = (page_h_mm - inner_top_mm) * mm
            text_x_mm = margin_mm + 6.0
            c.setFont(font_name, fs + 0.5)
            c.drawString(margin_mm * mm, y_text_pt, f"{idx + 1}.")
            if q_lines:
                c.drawString(text_x_mm * mm, y_text_pt, q_lines[0])
            y_text_pt -= (fs + 0.5) * 1.35
            for line in q_lines[1:]:
                c.drawString(text_x_mm * mm, y_text_pt, line)
                y_text_pt -= (fs + 0.5) * 1.35

        c.showPage()
        c.save()

    try:
        build(9.0)
    except ValueError:
        build(8.0)

    return str(pdf_path)


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
    A6: название, ФИО, класс, QR; задания столбиком — номер и 4 квадрата в строку (фикс. шаг).
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

    _draw_qr(c, qr_payload=qr_payload, qr_left_mm=qr_left_mm, qr_top_mm=qr_top_mm, qr_size_mm=qr_size, page_h_mm=page_h_mm)

    title = (blank.title or "Тест").strip()
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
    c.drawString(lx, y_fio_pt, "ФИО: ________________________________________________")
    y_cursor_mm += 5.0
    y_cls_pt = (page_h_mm - y_cursor_mm) * mm
    c.drawString(lx, y_cls_pt, "Класс: ______________________________________________")
    y_cursor_mm += 7.0

    header_bottom_mm = max(qr_top_mm + qr_size + 1.5, y_cursor_mm)
    content_top_mm = header_bottom_mm + 2.0

    options_left_mm = m + A6M.number_block_mm
    checkbox_outer_pt = outer * mm
    labels = ["A", "B", "C", "D"]
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
    marker_size_mm = 6.0
    # Сдвигаем ArUco дальше от полей ответов, чтобы не перекрывать зоны выбора.
    marker_offset_mm = 6.5
    marker_defs = [
        ("tl", 11, grid_left_mm - marker_offset_mm, grid_top_mm - marker_offset_mm),
        ("tr", 12, grid_right_mm + marker_offset_mm - marker_size_mm, grid_top_mm - marker_offset_mm),
        ("bl", 13, grid_left_mm - marker_offset_mm, grid_bottom_mm + marker_offset_mm - marker_size_mm),
        ("br", 14, grid_right_mm + marker_offset_mm - marker_size_mm, grid_bottom_mm + marker_offset_mm - marker_size_mm),
    ]
    markers: list[dict] = []
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    marker_px = 220
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

    c.showPage()
    c.save()

    layout = _layout_v3_for_answers(rows_meta=rows_meta, options_left_mm=options_left_mm, markers=markers)
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
