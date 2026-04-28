from __future__ import annotations

import json

import cv2
import numpy as np

from ..models import TestBlank
from .answer_sheet_a6 import A6 as A6M
from .layout_geometry import checkbox_inner_rect_horizontal, checkbox_inner_rect_mm
from .qr_service import verify_qr_payload
from .template_constants import METRICS


OPTION_LABELS = ["A", "B", "C", "D"]

# Базовая высота выпрямлённого растра; ширина считается из соотношения сторон листа (как в PDF).
_DEFAULT_RASTER_H = 2400


def _order_points(pts: np.ndarray) -> np.ndarray:
    # Ожидаем shape (4,2)
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]  # top-left
    rect[2] = pts[np.argmax(s)]  # bottom-right
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # top-right
    rect[3] = pts[np.argmax(diff)]  # bottom-left
    return rect


def _detect_paper_corners(warp_src: np.ndarray) -> np.ndarray:
    """
    Пытается найти контур листа (4 угла) и вернуть их в исходных координатах.
    """
    img = warp_src.copy()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    # Canny+контуры часто дают стабильные углы на фото листа
    edges = cv2.Canny(gray, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("Не удалось найти контур листа")

    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    doc_contour = contours[0]

    peri = cv2.arcLength(doc_contour, True)
    approx = cv2.approxPolyDP(doc_contour, 0.02 * peri, True)

    if len(approx) == 4:
        pts = approx.reshape(4, 2).astype(np.float32)
        return _order_points(pts)

    # Fallback: минимальный повернутый прямоугольник
    rect = cv2.minAreaRect(doc_contour)
    box = cv2.boxPoints(rect)
    pts = box.astype(np.float32)
    return _order_points(pts)


def _warp_to_page(img_bgr: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    corners = _detect_paper_corners(img_bgr)
    dst = np.array(
        [
            [0, 0],
            [target_w - 1, 0],
            [target_w - 1, target_h - 1],
            [0, target_h - 1],
        ],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(img_bgr, m, (target_w, target_h))


def _warp_to_page_with_matrix(img_bgr: np.ndarray, target_w: int, target_h: int) -> tuple[np.ndarray, np.ndarray]:
    corners = _detect_paper_corners(img_bgr)
    dst = np.array(
        [
            [0, 0],
            [target_w - 1, 0],
            [target_w - 1, target_h - 1],
            [0, target_h - 1],
        ],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(img_bgr, m, (target_w, target_h)), m


def _decode_qr_text_with_points(img_bgr: np.ndarray) -> tuple[str, np.ndarray]:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    detector = cv2.QRCodeDetector()

    def try_decode(img_gray: np.ndarray) -> tuple[str, np.ndarray] | None:
        # OpenCV Python: detectAndDecode -> (decoded_text, points, straight_qrcode)
        try:
            decoded_text, points, _straight = detector.detectAndDecode(img_gray)
            if decoded_text and points is not None:
                pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
                if pts.shape[0] == 4:
                    return decoded_text, _order_points(pts)
        except Exception:
            pass

        # detectAndDecodeMulti -> (retval, decoded_info, points, straight_qrcode)
        try:
            retval, decoded_infos, points, _straight = detector.detectAndDecodeMulti(img_gray)
            if retval and decoded_infos and points is not None:
                for idx, item in enumerate(decoded_infos):
                    if item:
                        pts = np.asarray(points[idx], dtype=np.float32).reshape(-1, 2)
                        if pts.shape[0] == 4:
                            return item, _order_points(pts)
        except Exception:
            pass
        return None

    # Несколько попыток: исходный grayscale, контраст и масштаб.
    variants = [
        gray,
        cv2.equalizeHist(gray),
    ]
    for base in variants:
        for scale in (1.0, 1.4, 1.8, 2.2):
            if scale == 1.0:
                img_try = base
            else:
                img_try = cv2.resize(base, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            got = try_decode(img_try)
            if got is not None:
                text, pts_scaled = got
                if scale != 1.0:
                    pts_scaled = pts_scaled / scale
                return text, pts_scaled.astype(np.float32)

    raise ValueError("QR-код не найден")


def _decode_qr_text(img_bgr: np.ndarray) -> str:
    text, _pts = _decode_qr_text_with_points(img_bgr)
    return text


def _detect_qr_points_only(img_bgr: np.ndarray) -> np.ndarray | None:
    """
    Пытается найти только 4 угла QR (без декодирования текста).
    Нужен, когда payload пришел с телефона, но в фото нужно геометрическое выравнивание.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    detector = cv2.QRCodeDetector()

    variants = [
        gray,
        cv2.equalizeHist(gray),
    ]
    for base in variants:
        for scale in (1.0, 1.4, 1.8, 2.2):
            if scale == 1.0:
                img_try = base
            else:
                img_try = cv2.resize(base, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            try:
                ok, points = detector.detect(img_try)
                if ok and points is not None:
                    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
                    if pts.shape[0] == 4:
                        if scale != 1.0:
                            pts = pts / scale
                        return _order_points(pts.astype(np.float32))
            except Exception:
                pass
    return None


def _warp_by_qr_anchor(
    *,
    img_bgr: np.ndarray,
    qr_points_img: np.ndarray,
    target_w: int,
    target_h: int,
    page_w_mm: float,
    page_h_mm: float,
    qr_left_mm: float,
    qr_top_mm: float,
    qr_size_mm: float,
) -> np.ndarray:
    src = _order_points(qr_points_img.astype(np.float32))
    x0 = (qr_left_mm / page_w_mm) * target_w
    y0 = (qr_top_mm / page_h_mm) * target_h
    x1 = ((qr_left_mm + qr_size_mm) / page_w_mm) * target_w
    y1 = ((qr_top_mm + qr_size_mm) / page_h_mm) * target_h
    dst = np.array(
        [
            [x0, y0],
            [x1, y0],
            [x1, y1],
            [x0, y1],
        ],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img_bgr, m, (target_w, target_h))


def _warp_by_qr_anchor_with_matrix(
    *,
    img_bgr: np.ndarray,
    qr_points_img: np.ndarray,
    target_w: int,
    target_h: int,
    page_w_mm: float,
    page_h_mm: float,
    qr_left_mm: float,
    qr_top_mm: float,
    qr_size_mm: float,
) -> tuple[np.ndarray, np.ndarray]:
    src = _order_points(qr_points_img.astype(np.float32))
    x0 = (qr_left_mm / page_w_mm) * target_w
    y0 = (qr_top_mm / page_h_mm) * target_h
    x1 = ((qr_left_mm + qr_size_mm) / page_w_mm) * target_w
    y1 = ((qr_top_mm + qr_size_mm) / page_h_mm) * target_h
    dst = np.array(
        [
            [x0, y0],
            [x1, y0],
            [x1, y1],
            [x0, y1],
        ],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img_bgr, m, (target_w, target_h)), m


def _mm_to_px_x(x_mm: float, *, page_w_mm: float, target_w: int) -> int:
    return int(x_mm / page_w_mm * target_w)


def _mm_to_px_y(y_from_top_mm: float, *, page_h_mm: float, target_h: int) -> int:
    return int(y_from_top_mm / page_h_mm * target_h)


def _analyze_checkbox_fill(gray: np.ndarray, crop_rect: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = crop_rect
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return 0.0

    # Инвертируем, чтобы закрашенное становилось "белым" на бинарной маске.
    # Оцу адаптирует порог под конкретный кроп.
    _, th = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Убираем мелкий шум и оцениваем несколько признаков отметки.
    th = cv2.medianBlur(th, 3)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((2, 2), dtype=np.uint8))
    h, w = th.shape[:2]
    cx0 = int(w * 0.25)
    cx1 = int(w * 0.75)
    cy0 = int(h * 0.25)
    cy1 = int(h * 0.75)
    center = th[cy0:cy1, cx0:cx1]

    fill_total = float(np.mean(th) / 255.0)
    fill_center = float(np.mean(center) / 255.0) if center.size else fill_total

    # Доп. признак для X: плотность по диагональным полосам.
    yy, xx = np.indices((h, w))
    diag_band = max(1, int(min(h, w) * 0.17))
    d1_mask = np.abs(xx - yy) <= diag_band
    d2_mask = np.abs((xx + yy) - (w - 1)) <= diag_band
    d1 = float(np.mean(th[d1_mask]) / 255.0) if np.any(d1_mask) else 0.0
    d2 = float(np.mean(th[d2_mask]) / 255.0) if np.any(d2_mask) else 0.0
    diag_score = max(d1, d2)

    # Смешанный score: центр + диагонали + общая плотность.
    return 0.25 * fill_total + 0.35 * fill_center + 0.40 * diag_score


def _find_marker_center(
    gray: np.ndarray,
    *,
    expected_x: int,
    expected_y: int,
    marker_size_px: int,
    search_radius_px: int = 120,
) -> tuple[float, float] | None:
    h, w = gray.shape[:2]
    x0 = max(0, expected_x - search_radius_px)
    y0 = max(0, expected_y - search_radius_px)
    x1 = min(w, expected_x + search_radius_px)
    y1 = min(h, expected_y + search_radius_px)
    roi = gray[y0:y1, x0:x1]
    if roi.size == 0:
        return None

    _, th = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    best = None
    best_score = 1e9
    for c in contours:
        x, y, ww, hh = cv2.boundingRect(c)
        area = float(ww * hh)
        if area < 16:
            continue
        ratio = ww / max(1.0, float(hh))
        if ratio < 0.65 or ratio > 1.35:
            continue
        size_penalty = abs(ww - marker_size_px) + abs(hh - marker_size_px)
        cx = x0 + x + ww / 2.0
        cy = y0 + y + hh / 2.0
        dist_penalty = abs(cx - expected_x) * 0.25 + abs(cy - expected_y) * 0.25
        score = size_penalty + dist_penalty
        if score < best_score:
            best_score = score
            best = (cx, cy)
    return best


def _aruco_homography_from_layout(
    gray: np.ndarray,
    *,
    markers_layout: list[dict],
    page_w_mm: float,
    page_h_mm: float,
    target_w: int,
    target_h: int,
) -> np.ndarray | None:
    if not hasattr(cv2, "aruco"):
        return None
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    detector = cv2.aruco.ArucoDetector(aruco_dict, cv2.aruco.DetectorParameters())

    detected: dict[int, tuple[float, float]] = {}
    variants = [
        gray,
        cv2.equalizeHist(gray),
    ]
    for base in variants:
        for scale in (1.0, 1.4, 1.8, 2.2):
            if scale == 1.0:
                img_try = base
            else:
                img_try = cv2.resize(base, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            try:
                corners, ids, _rej = detector.detectMarkers(img_try)
            except Exception:
                continue
            if ids is None or len(ids) == 0:
                continue
            for idx, marker_id in enumerate(ids.flatten().tolist()):
                pts = np.asarray(corners[idx], dtype=np.float32).reshape(-1, 2)
                if pts.shape[0] != 4:
                    continue
                if scale != 1.0:
                    pts = pts / scale
                cx = float(np.mean(pts[:, 0]))
                cy = float(np.mean(pts[:, 1]))
                detected[int(marker_id)] = (cx, cy)

    src_pts = []
    dst_pts = []
    for m in markers_layout:
        mid = int(m.get("id", -1))
        if mid in detected:
            src_pts.append([detected[mid][0], detected[mid][1]])
            ex = _mm_to_px_x(float(m["x_mm"]), page_w_mm=page_w_mm, target_w=target_w)
            ey = _mm_to_px_y(float(m["y_mm"]), page_h_mm=page_h_mm, target_h=target_h)
            dst_pts.append([float(ex), float(ey)])

    if len(src_pts) < 4:
        return None
    hmat, _mask = cv2.findHomography(
        np.array(src_pts, dtype=np.float32),
        np.array(dst_pts, dtype=np.float32),
        method=0,
    )
    return hmat


def verify_blank_image(
    *,
    img_bgr: np.ndarray,
    blank: TestBlank,
    qr_payload_raw: str,
    qr_secret: str,
    qr_payload_version: str,
    qr_points_img: np.ndarray | None = None,
) -> dict:
    # QR payload -> blank_uuid already validated in route? Здесь перепроверяем сигнатуру.
    _ = verify_qr_payload(
        payload=qr_payload_raw,
        secret=qr_secret,
        expected_version=qr_payload_version,
    )

    page_w_mm = float(METRICS.page_w_mm)
    page_h_mm = float(METRICS.page_h_mm)
    options_left_mm = float(METRICS.options_left_mm)
    anchors: list[float] = []
    layout_version: int | None = None
    h_outer = float(METRICS.checkbox_outer_mm)
    h_gap_x = float(METRICS.checkbox_gap_x_mm)
    h_inset = float(METRICS.checkbox_inner_inset_mm)
    qr_left_mm = float(METRICS.qr_left_mm)
    qr_top_mm = float(METRICS.qr_top_mm)
    qr_size_mm = float(METRICS.qr_size_mm)
    data: dict | None = None
    if blank.layout_json:
        try:
            data = json.loads(blank.layout_json)
            layout_version = int(data.get("version")) if data.get("version") is not None else None
            if layout_version == 3 and data.get("rows"):
                page_w_mm = float(data["page_w_mm"])
                page_h_mm = float(data["page_h_mm"])
                options_left_mm = float(data["options_left_mm"])
                anchors = [float(r["checkbox_anchor_top_mm"]) for r in data["rows"]]
                h_outer = float(data["checkbox_outer_mm"])
                h_gap_x = float(data["checkbox_gap_x_mm"])
                h_inset = float(data["checkbox_inner_inset_mm"])
                qr_left_mm = float(data.get("qr_left_mm", page_w_mm - A6M.margin_mm - A6M.qr_size_mm))
                qr_top_mm = float(data.get("qr_top_mm", A6M.qr_top_mm))
                qr_size_mm = float(data.get("qr_size_mm", A6M.qr_size_mm))
            elif layout_version == 2 and data.get("rows"):
                options_left_mm = float(data["options_left_mm"])
                anchors = [float(r["checkbox_anchor_top_mm"]) for r in data["rows"]]
                page_w_mm = float(data.get("page_w_mm", page_w_mm))
                page_h_mm = float(data.get("page_h_mm", page_h_mm))
        except Exception:
            data = None
            layout_version = None

    target_h = _DEFAULT_RASTER_H
    target_w = max(1, int(target_h * (page_w_mm / page_h_mm)))

    # Для A6 сначала пытаемся выровнять прямо по ArUco в исходном кадре.
    pre_hmat_aruco = None
    if layout_version == 3 and data and data.get("markers"):
        src_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        pre_hmat_aruco = _aruco_homography_from_layout(
            src_gray,
            markers_layout=data.get("markers", []),
            page_w_mm=page_w_mm,
            page_h_mm=page_h_mm,
            target_w=target_w,
            target_h=target_h,
        )

    if pre_hmat_aruco is not None:
        warped_bgr = cv2.warpPerspective(img_bgr, pre_hmat_aruco, (target_w, target_h))
    elif layout_version == 3 and qr_points_img is not None:
        # Для A6-ответов выравнивание по QR значительно стабильнее, чем по контуру листа.
        warped_bgr, _h_qr = _warp_by_qr_anchor_with_matrix(
            img_bgr=img_bgr,
            qr_points_img=qr_points_img,
            target_w=target_w,
            target_h=target_h,
            page_w_mm=page_w_mm,
            page_h_mm=page_h_mm,
            qr_left_mm=qr_left_mm,
            qr_top_mm=qr_top_mm,
            qr_size_mm=qr_size_mm,
        )
    elif layout_version == 3:
        # Если payload QR пришел с телефона, но точки QR не передали — пытаемся найти углы QR в фото.
        qr_pts_only = _detect_qr_points_only(img_bgr)
        if qr_pts_only is not None:
            warped_bgr, _h_qr2 = _warp_by_qr_anchor_with_matrix(
                img_bgr=img_bgr,
                qr_points_img=qr_pts_only,
                target_w=target_w,
                target_h=target_h,
                page_w_mm=page_w_mm,
                page_h_mm=page_h_mm,
                qr_left_mm=qr_left_mm,
                qr_top_mm=qr_top_mm,
                qr_size_mm=qr_size_mm,
            )
        else:
            warped_bgr, _h_page = _warp_to_page_with_matrix(img_bgr, target_w, target_h)
    else:
        warped_bgr, _h_page = _warp_to_page_with_matrix(img_bgr, target_w, target_h)
    warped_gray = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
    warped_gray = cv2.equalizeHist(warped_gray)

    # Дополнительная калибровка после первичного warp (если из исходного кадра ArUco не нашли).
    if layout_version == 3 and data and data.get("markers") and pre_hmat_aruco is None:
        markers = data.get("markers", [])
        hmat = _aruco_homography_from_layout(
            warped_gray,
            markers_layout=markers,
            page_w_mm=page_w_mm,
            page_h_mm=page_h_mm,
            target_w=target_w,
            target_h=target_h,
        )

        # fallback на старые черные квадратные метки
        if hmat is None:
            src_pts = []
            dst_pts = []
            for m in markers:
                ex = _mm_to_px_x(float(m["x_mm"]), page_w_mm=page_w_mm, target_w=target_w)
                ey = _mm_to_px_y(float(m["y_mm"]), page_h_mm=page_h_mm, target_h=target_h)
                size_px = max(
                    2,
                    int(float(m.get("size_mm", 2.5)) / page_w_mm * target_w),
                )
                found = _find_marker_center(
                    warped_gray,
                    expected_x=ex,
                    expected_y=ey,
                    marker_size_px=size_px,
                    search_radius_px=140,
                )
                if found is not None:
                    src_pts.append([found[0], found[1]])
                    dst_pts.append([float(ex), float(ey)])
            if len(src_pts) >= 4:
                hmat, _mask = cv2.findHomography(
                    np.array(src_pts, dtype=np.float32),
                    np.array(dst_pts, dtype=np.float32),
                    method=0,
                )

        if hmat is not None:
            warped_bgr = cv2.warpPerspective(warped_bgr, hmat, (target_w, target_h))
            warped_gray = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
            warped_gray = cv2.equalizeHist(warped_gray)

    if not anchors:
        anchors = [
            METRICS.top_margin_mm + qi * METRICS.row_h_mm
            for qi in range(blank.question_count)
        ]

    # Анализ чекбоксов
    results: list[dict] = []
    fill_scores_by_question: list[list[float]] = []

    # Пороги по умолчанию (для старого шаблона).
    min_fill_ratio = 0.08
    ambiguous_delta = 0.03

    # Для A6 с горизонтальными вариантами (layout v3) используем более чувствительные пороги
    # и относительную проверку на фоне остальных вариантов в строке.
    if layout_version == 3:
        min_fill_ratio = 0.028
        ambiguous_delta = 0.010

    for qi in range(blank.question_count):
        if qi < len(anchors):
            anchor_mm = anchors[qi]
        else:
            anchor_mm = METRICS.top_margin_mm + qi * METRICS.row_h_mm


        scores: list[float] = []
        for opt_index in range(4):
            if layout_version == 3:
                inner_left_mm, inner_top_mm, inner_side = checkbox_inner_rect_horizontal(
                    row_anchor_top_mm=anchor_mm,
                    options_left_mm=options_left_mm,
                    opt_index=opt_index,
                    outer_mm=h_outer,
                    inset_mm=h_inset,
                    gap_x_mm=h_gap_x,
                )
            else:
                inner_left_mm, inner_top_mm, inner_side = checkbox_inner_rect_mm(
                    row_anchor_top_mm=anchor_mm,
                    options_left_mm=options_left_mm,
                    opt_index=opt_index,
                )

            x0 = max(0, _mm_to_px_x(inner_left_mm, page_w_mm=page_w_mm, target_w=target_w))
            y0 = max(0, _mm_to_px_y(inner_top_mm, page_h_mm=page_h_mm, target_h=target_h))
            x1 = min(
                target_w - 1,
                _mm_to_px_x(inner_left_mm + inner_side, page_w_mm=page_w_mm, target_w=target_w),
            )
            y1 = min(
                target_h - 1,
                _mm_to_px_y(inner_top_mm + inner_side, page_h_mm=page_h_mm, target_h=target_h),
            )

            scores.append(_analyze_checkbox_fill(warped_gray, (x0, y0, x1, y1)))

        fill_scores_by_question.append(scores)
        top_idx = int(np.argmax(scores))
        top_score = scores[top_idx]

        # second max
        sorted_scores = sorted([(s, i) for i, s in enumerate(scores)], reverse=True)
        second_score = sorted_scores[1][0] if len(sorted_scores) > 1 else 0.0

        selected_index = None
        ambiguous = False
        mean_other = (sum(scores) - top_score) / 3.0
        relative_margin = top_score - mean_other

        if top_score >= min_fill_ratio:
            if layout_version == 3:
                # На A6 считаем вариант выбранным, если он заметно "чернее" остальных.
                if relative_margin >= 0.008:
                    if (top_score - second_score) < ambiguous_delta:
                        ambiguous = True
                    selected_index = top_idx
            else:
                if (top_score - second_score) < ambiguous_delta:
                    ambiguous = True
                selected_index = top_idx

        correct_index = blank.questions[qi].correct_index  # вопросы в порядке qi
        is_correct = selected_index is not None and (selected_index == correct_index) and (not ambiguous)

        results.append(
            {
                "question_number": qi + 1,
                "selected": OPTION_LABELS[selected_index] if selected_index is not None else None,
                "correct": OPTION_LABELS[correct_index],
                "is_correct": bool(is_correct),
                "ambiguous": ambiguous,
                "scores": {
                    OPTION_LABELS[i]: float(scores[i]) for i in range(4)
                },
            }
        )

    correct_count = sum(1 for r in results if r["is_correct"])

    return {
        "blank_uuid": blank.uuid,
        "questions_total": blank.question_count,
        "correct_count": correct_count,
        "results": results,
    }

