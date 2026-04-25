from flask import Blueprint, current_app, jsonify, request
import numpy as np
import cv2

from .models import TestBlank, TestQuestion, TestQuestionStats, db
from .services.qr_service import verify_qr_payload
from .services.verify_service import _decode_qr_text_with_points, verify_blank_image

api_bp = Blueprint("api", __name__)


def _accumulate_question_stats(blank: TestBlank, verify_payload: dict) -> None:
    """
    Накопительная статистика после каждой проверки бланка:
    - correct_total / attempts_total по вопросу
    - option_*_total по выбранному варианту.
    """
    results = verify_payload.get("results") or []
    if not results:
        return

    questions = (
        TestQuestion.query.filter_by(blank_id=blank.id).order_by(TestQuestion.question_number.asc()).all()
    )
    q_by_num = {int(q.question_number): q for q in questions}

    stats_rows = TestQuestionStats.query.filter_by(blank_id=blank.id).all()
    stats_by_qid = {int(s.question_id): s for s in stats_rows}

    for row in results:
        try:
            qn = int(row.get("question_number"))
        except Exception:
            continue
        q = q_by_num.get(qn)
        if not q:
            continue
        s = stats_by_qid.get(int(q.id))
        if s is None:
            s = TestQuestionStats(blank_id=blank.id, question_id=q.id)
            db.session.add(s)
            stats_by_qid[int(q.id)] = s

        s.attempts_total = int(s.attempts_total or 0) + 1
        if bool(row.get("is_correct")):
            s.correct_total = int(s.correct_total or 0) + 1

        selected = (row.get("selected") or "").strip().upper()
        if selected == "A":
            s.option_a_total = int(s.option_a_total or 0) + 1
        elif selected == "B":
            s.option_b_total = int(s.option_b_total or 0) + 1
        elif selected == "C":
            s.option_c_total = int(s.option_c_total or 0) + 1
        elif selected == "D":
            s.option_d_total = int(s.option_d_total or 0) + 1

    db.session.commit()


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
        )
        _accumulate_question_stats(blank, payload)
        return jsonify({"status": "ok", **payload})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception:
        return jsonify({"status": "error", "message": "Ошибка обработки фото"}), 500

