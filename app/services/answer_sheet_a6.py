"""Параметры бланка ответов A6 (для печати и layout_json v3)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerSheetA6:
    page_w_mm: float = 105.0
    page_h_mm: float = 148.0
    margin_mm: float = 5.0
    qr_size_mm: float = 20.0
    qr_top_mm: float = 5.0
    # Фиксированная строка задания: номер + 4 квадрата в ряд (шаг по вертикали между заданиями)
    row_height_mm: float = 9.5
    number_block_mm: float = 12.0  # место под «12.»
    checkbox_outer_mm: float = 5.0
    checkbox_gap_x_mm: float = 4.0
    checkbox_inner_inset_mm: float = 1.2


A6 = AnswerSheetA6()
