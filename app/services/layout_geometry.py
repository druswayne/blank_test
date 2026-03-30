"""Общая геометрия чекбоксов A/B/C/D (2×2) относительно якоря строки."""

from .template_constants import METRICS


def checkbox_inner_rect_mm(
    *,
    row_anchor_top_mm: float,
    options_left_mm: float,
    opt_index: int,
) -> tuple[float, float, float]:
    """
    Возвращает (left_mm, top_mm, side_mm) от верхнего края страницы.
    По требованию используем всю клетку (без внутреннего inset).
    """
    outer = METRICS.checkbox_outer_mm
    col = opt_index % 2
    row = opt_index // 2
    x_outer_left_mm = options_left_mm + col * (METRICS.checkbox_outer_mm + METRICS.checkbox_gap_x_mm)
    y_outer_top_mm = row_anchor_top_mm + METRICS.checkbox_top_padding_mm + row * (
        METRICS.checkbox_outer_mm + METRICS.checkbox_gap_y_mm
    )
    return (
        x_outer_left_mm,
        y_outer_top_mm,
        outer,
    )


def checkbox_inner_rect_horizontal(
    *,
    row_anchor_top_mm: float,
    options_left_mm: float,
    opt_index: int,
    outer_mm: float,
    inset_mm: float,
    gap_x_mm: float,
) -> tuple[float, float, float]:
    """Один ряд A B C D: квадрат для анализа по всей клетке."""
    x_outer_left_mm = options_left_mm + opt_index * (outer_mm + gap_x_mm)
    y_outer_top_mm = row_anchor_top_mm
    return (
        x_outer_left_mm,
        y_outer_top_mm,
        outer_mm,
    )
