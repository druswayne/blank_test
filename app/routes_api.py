import logging
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
import numpy as np
import cv2
from sqlalchemy import text

from .models import TestBlank, TestQuestion, TestQuestionStats, db
from .services.qr_service import verify_qr_payload
from .services.verify_service import _decode_qr_text_with_points, verify_blank_image

logger = logging.getLogger(__name__)

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

    for row in results:
        try:
            qn = int(row.get("question_number"))
        except Exception:
            continue
        q = q_by_num.get(qn)
        if not q:
            continue

        selected = (row.get("selected") or "").strip().upper()
        inc_correct = 1 if bool(row.get("is_correct")) else 0
        inc_a = 1 if selected == "A" else 0
        inc_b = 1 if selected == "B" else 0
        inc_c = 1 if selected == "C" else 0
        inc_d = 1 if selected == "D" else 0

        # Защита от гонок: сначала гарантируем наличие строки статистики, затем
        # увеличиваем счетчики атомарно на стороне БД (без read-modify-write в Python).
        db.session.execute(
            text(
                """
                INSERT INTO test_question_stats
                    (blank_id, question_id, attempts_total, correct_total, option_a_total, option_b_total, option_c_total, option_d_total)
                VALUES
                    (:blank_id, :question_id, 0, 0, 0, 0, 0, 0)
                ON CONFLICT(question_id) DO NOTHING
                """
            ),
            {"blank_id": blank.id, "question_id": q.id},
        )
        db.session.execute(
            text(
                """
                UPDATE test_question_stats
                SET
                    attempts_total = attempts_total + :inc_attempts,
                    correct_total = correct_total + :inc_correct,
                    option_a_total = option_a_total + :inc_a,
                    option_b_total = option_b_total + :inc_b,
                    option_c_total = option_c_total + :inc_c,
                    option_d_total = option_d_total + :inc_d
                WHERE question_id = :question_id
                """
            ),
            {
                "question_id": q.id,
                "inc_attempts": 1,
                "inc_correct": inc_correct,
                "inc_a": inc_a,
                "inc_b": inc_b,
                "inc_c": inc_c,
                "inc_d": inc_d,
            },
        )

    db.session.commit()


@api_bp.route("/verify", methods=["POST"])
def api_verify():
    """
    Ожидает multipart/form-data с файлом `photo`.

    Клиент может присылать уже обработанное изображение (режим «документ / ч/б» на телефоне).
    Рекомендуется поле `qr_payload`, если фото сильно бинаризовано — QR с картинки может не прочитаться.
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

    archive_run_id: str | None = None
    if current_app.config.get("VERIFY_ARCHIVE_PHOTOS"):
        try:
            archive_run_id = str(uuid.uuid4())
            archive_dir = Path(current_app.config["VERIFY_PHOTO_ARCHIVE_DIR"])
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{archive_run_id}.jpg"
            archive_path.write_bytes(photo_bytes)
        except OSError as exc:
            logger.warning("Архив фото verify (диск): %s", exc)
            archive_run_id = None

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

        payload, annotated_warped = verify_blank_image(
            img_bgr=img_bgr,
            blank=blank,
            qr_payload_raw=qr_raw,
            qr_secret=qr_secret,
            qr_payload_version=qr_payload_version,
            qr_points_img=qr_points,
            return_annotated_warped=archive_run_id is not None,
        )
        _accumulate_question_stats(blank, payload)
        body = {"status": "ok", **payload}
        if archive_run_id is not None:
            body["archive_photo_id"] = archive_run_id
            if annotated_warped is not None:
                try:
                    archive_dir = Path(current_app.config["VERIFY_PHOTO_ARCHIVE_DIR"])
                    enc_ok, enc_buf = cv2.imencode(
                        ".jpg", annotated_warped, [int(cv2.IMWRITE_JPEG_QUALITY), 90]
                    )
                    if enc_ok:
                        ann_path = archive_dir / f"{archive_run_id}_annotated.jpg"
                        ann_path.write_bytes(enc_buf.tobytes())
                        body["archive_photo_annotated"] = f"{archive_run_id}_annotated.jpg"
                except Exception as exc:
                    logger.warning("Сохранение разметки verify: %s", exc)
        return jsonify(body)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception:
        return jsonify({"status": "error", "message": "Ошибка обработки фото"}), 500

