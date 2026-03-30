from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
import numpy as np
import cv2

from .models import TestBlank
from .services.qr_service import verify_qr_payload
from .services.verify_service import _decode_qr_text_with_points, verify_blank_image

api_bp = Blueprint("api", __name__)

def _safe_imwrite(path: Path, img_bgr) -> bool:
    """
    Надежная запись изображения для Windows путей с не-ASCII символами.
    """
    try:
        ok, buf = cv2.imencode(".jpg", img_bgr)
        if not ok:
            return False
        buf.tofile(str(path))
        return True
    except Exception:
        return False


@api_bp.route("/verify", methods=["POST"])
def api_verify():
    """
    Ожидает multipart/form-data с файлом `photo`.
    """
    if "photo" not in request.files:
        return jsonify({"status": "error", "message": "Нет поля `photo`"}), 400

    f = request.files["photo"]
    photo_bytes = f.read()
    if not photo_bytes:
        return jsonify({"status": "error", "message": "Пустой файл"}), 400

    # Разбираем в OpenCV
    arr = np.frombuffer(photo_bytes, dtype=np.uint8)
    img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        return jsonify({"status": "error", "message": "Не удалось декодировать изображение"}), 400

    qr_secret = current_app.config["QR_HMAC_SECRET"]
    qr_payload_version = current_app.config["QR_PAYLOAD_VERSION"]
    debug_dir = current_app.config.get("VERIFY_DEBUG_DIR")

    # Всегда сохраняем входное фото для отладки, даже если дальше проверка упадет.
    raw_debug_path = None
    if debug_dir:
        try:
            Path(debug_dir).mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            raw_path = Path(debug_dir) / f"raw_{ts}.jpg"
            if _safe_imwrite(raw_path, img_bgr):
                raw_debug_path = str(raw_path)
            else:
                raw_debug_path = None
        except Exception:
            raw_debug_path = None

    try:
        qr_raw = request.form.get("qr_payload", "").strip()
        qr_points = None
        if not qr_raw:
            qr_raw, qr_points = _decode_qr_text_with_points(img_bgr)
        blank_uuid = verify_qr_payload(
            payload=qr_raw,
            secret=qr_secret,
            expected_version=qr_payload_version,
        )

        blank = TestBlank.query.filter_by(uuid=blank_uuid).first()
        if not blank:
            return jsonify({"status": "error", "message": "Бланк не найден"}), 404

        payload = verify_blank_image(
            img_bgr=img_bgr,
            blank=blank,
            qr_payload_raw=qr_raw,
            qr_secret=qr_secret,
            qr_payload_version=qr_payload_version,
            qr_points_img=qr_points,
            debug_dir=debug_dir,
        )
        payload["raw_debug_path"] = raw_debug_path
        return jsonify({"status": "ok", **payload})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e), "raw_debug_path": raw_debug_path}), 400
    except Exception:
        return jsonify({"status": "error", "message": "Ошибка обработки фото", "raw_debug_path": raw_debug_path}), 500

