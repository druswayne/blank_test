from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateMetrics:
    # A4 in millimeters (портрет)
    page_w_mm: float = 210.0
    page_h_mm: float = 297.0

    # Layout (legacy-сетка для бланков без layout_json)
    max_questions: int = 10
    top_margin_mm: float = 22.0
    bottom_margin_mm: float = 18.0

    # Левый край колонки чекбоксов A при фиксированной сетке (≈ page_w - поля - блок 24 мм)
    options_left_mm: float = 178.0
    checkbox_outer_mm: float = 6.0
    checkbox_gap_x_mm: float = 8.0

    checkbox_top_padding_mm: float = 0.2  # padding from row top to top checkbox outer edge
    checkbox_gap_y_mm: float = 5.0

    # Inner analysis area: smaller inset to avoid borders/lines
    checkbox_inner_inset_mm: float = 1.6

    # QR (legacy; в PDF позиция считается от актуальной ширины страницы)
    qr_left_mm: float = 178.0
    qr_top_mm: float = 8.0
    qr_size_mm: float = 22.0

    @property
    def questions_area_h_mm(self) -> float:
        return self.page_h_mm - self.top_margin_mm - self.bottom_margin_mm

    @property
    def row_h_mm(self) -> float:
        return self.questions_area_h_mm / self.max_questions


METRICS = TemplateMetrics()

