"""
Имитация режима камеры «Документы» / «Фото ЧБ» на телефоне:
подавление теней и бликов → локальный контраст → бинарное ч/б (белый лист, тёмные штрихи).

Результат — BGR uint8 (три одинаковых канала), совместимый с verify_blank_image.
"""

from __future__ import annotations

import cv2
import numpy as np


def _suppress_uneven_light_bgr(img_bgr: np.ndarray) -> np.ndarray:
    """
    Подавление теней/бликов: гладкая оценка фона (dilate + medianBlur), вычитание.
    """
    h, w = img_bgr.shape[:2]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    med_k = max(15, min(51, (max(h, w) // 25) | 1))
    med_k = min(med_k, (max(h, w) // 2) | 1)
    if med_k % 2 == 0:
        med_k += 1

    planes = cv2.split(img_bgr)
    out_planes: list[np.ndarray] = []
    for plane in planes:
        dilated = cv2.dilate(plane, kernel)
        bg = cv2.medianBlur(dilated, med_k)
        diff = 255 - cv2.absdiff(plane, bg)
        norm = cv2.normalize(diff, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
        out_planes.append(norm)

    return cv2.merge(out_planes)


def _enhance_contrast_bgr(img_bgr: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l_ch = clahe.apply(l_ch)
    lab = cv2.merge((l_ch, a_ch, b_ch))
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _adaptive_block_size(h: int, w: int) -> int:
    """Размер окна для adaptiveThreshold: как у мобильных сканеров документов (локальный порог по сетке)."""
    m = min(h, w)
    if m < 5:
        return 3

    block = int(m * 0.065) | 1
    block = max(21, min(91, block))
    if block >= m:
        block = max(11, (m // 4) | 1)
    # Окно должно быть меньше кадра (требование adaptiveThreshold)
    block = min(block, m - 2)
    block = max(3, block)
    if block % 2 == 0:
        block -= 1
    return max(3, block)


def apply_mobile_document_style(img_bgr: np.ndarray) -> np.ndarray:
    """
    Цепочка в духе «Документы / фото ЧБ»:
    выравнивание освещения → CLAHE → лёгкое сглаживание → адаптивная пороговая бинаризация.

    Получается контрастное ч/б с белым фоном и чёрными элементами (как в превью телефона).
    """
    if img_bgr is None or img_bgr.size == 0:
        raise ValueError("Пустое изображение")

    adjusted = _suppress_uneven_light_bgr(img_bgr)
    adjusted = _enhance_contrast_bgr(adjusted)
    gray = cv2.cvtColor(adjusted, cv2.COLOR_BGR2GRAY)

    # Лёгкое размытие перед порогом — меньше «крошки» на бинарном слое (часто есть в мобильных пайплайнах)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)

    h, w = blur.shape[:2]
    block = _adaptive_block_size(h, w)

    # Локальный порог: тёмное (текст, линии сетки, карандаш) → 0, светлое → 255
    binary = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=block,
        C=11,
    )

    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
