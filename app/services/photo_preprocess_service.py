"""
Имитация обработки «режим документ / камера» на телефоне:
подавление неравномерного освещения (тени/блики), усиление контраста, ч/б.

Результат — BGR uint8 (три одинаковых канала), совместимый с verify_blank_image.
"""

from __future__ import annotations

import cv2
import numpy as np


def _suppress_uneven_light_bgr(img_bgr: np.ndarray) -> np.ndarray:
    """
    Подавление теней/бликов: гладкая оценка фона (dilate + medianBlur), вычитание.
    Распространённый приём для мобильных «сканеров документов».
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


def apply_mobile_document_style(img_bgr: np.ndarray) -> np.ndarray:
    """
    Цепочка: подавление теней/бликов → контраст (CLAHE) → оттенки серого как «ч/б документ».

    Возвращает изображение BGR (серый, продублированный по каналам).
    """
    if img_bgr is None or img_bgr.size == 0:
        raise ValueError("Пустое изображение")

    adjusted = _suppress_uneven_light_bgr(img_bgr)
    adjusted = _enhance_contrast_bgr(adjusted)
    gray = cv2.cvtColor(adjusted, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=1.0)
    sharpened = cv2.addWeighted(gray, 1.25, blur, -0.25, 0)
    sharpened = np.clip(sharpened, 0, 255).astype(np.uint8)
    return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)
