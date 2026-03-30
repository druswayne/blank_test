from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user

from .models import TestBlank, TestQuestion, User, db, login_manager
from .services.pdf_service import generate_blank_pdfs

web_bp = Blueprint("web", __name__)


@web_bp.route("/", methods=["GET"])
def index():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@web_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        login = request.form.get("login", "").strip()
        password = request.form.get("password", "").strip()
        password2 = request.form.get("password2", "").strip()

        if not login or not password:
            flash("Заполните логин и пароль", "error")
            return redirect(url_for("web.register"))
        if password != password2:
            flash("Пароли не совпадают", "error")
            return redirect(url_for("web.register"))

        if User.query.filter_by(login=login).first():
            flash("Логин уже занят", "error")
            return redirect(url_for("web.register"))

        user = User(login=login, password_hash=User.hash_password(password))
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for("web.dashboard"))

    return render_template("register.html")


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_val = request.form.get("login", "").strip()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(login=login_val).first()
        if not user or not user.check_password(password):
            flash("Неверный логин или пароль", "error")
            return redirect(url_for("web.login"))

        login_user(user)
        return redirect(url_for("web.dashboard"))

    return render_template("login.html")


@web_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("web.login"))


@web_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    blanks = TestBlank.query.filter_by(owner_id=current_user.id).order_by(TestBlank.created_at.desc()).all()
    return render_template("dashboard.html", blanks=blanks)


@web_bp.route("/blanks/new", methods=["GET", "POST"])
@login_required
def new_blank():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        question_count = int(request.form.get("question_count", "1"))
        question_count = max(1, min(10, question_count))

        blank = TestBlank(owner_id=current_user.id, title=title, question_count=question_count)
        db.session.add(blank)
        db.session.flush()  # чтобы blank.id появился до создания вопросов

        for i in range(1, question_count + 1):
            question_text = request.form.get(f"question_{i}", "").strip()
            option_a = request.form.get(f"option_a_{i}", "").strip()
            option_b = request.form.get(f"option_b_{i}", "").strip()
            option_c = request.form.get(f"option_c_{i}", "").strip()
            option_d = request.form.get(f"option_d_{i}", "").strip()
            correct_str = request.form.get(f"correct_{i}", "0")

            if not question_text:
                abort(400, description=f"Пустой текст вопроса {i}")

            correct_index = int(correct_str)
            correct_index = max(0, min(3, correct_index))

            q = TestQuestion(
                blank_id=blank.id,
                question_number=i,
                question_text=question_text,
                option_a=option_a or "",
                option_b=option_b or "",
                option_c=option_c or "",
                option_d=option_d or "",
                correct_index=correct_index,
            )
            db.session.add(q)

        db.session.commit()

        blank_db = TestBlank.query.filter_by(uuid=blank.uuid).first()
        _pq, _pa, layout_json_str = generate_blank_pdfs(
            blank=blank_db,
            pdf_dir=current_app.config["PDF_DIR"],
            qr_secret=current_app.config["QR_HMAC_SECRET"],
            qr_payload_version=current_app.config["QR_PAYLOAD_VERSION"],
        )
        blank_db.layout_json = layout_json_str
        db.session.commit()
        flash("Бланк создан: PDF с вопросами (A4) и бланк ответов (A6).", "success")
        return redirect(url_for("web.blank_detail", blank_uuid=blank.uuid))

    return render_template("new_blank.html", max_questions=10)


@web_bp.route("/blanks/<blank_uuid>", methods=["GET"])
@login_required
def blank_detail(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)
    return render_template("blank_detail.html", blank=blank)


def _ensure_both_pdfs(blank: TestBlank) -> None:
    pdf_dir = Path(current_app.config["PDF_DIR"])
    pq = pdf_dir / f"{blank.uuid}_questions.pdf"
    pa = pdf_dir / f"{blank.uuid}_answers.pdf"
    if pq.is_file() and pa.is_file():
        return
    _pq, _pa, layout_json_str = generate_blank_pdfs(
        blank=blank,
        pdf_dir=str(pdf_dir),
        qr_secret=current_app.config["QR_HMAC_SECRET"],
        qr_payload_version=current_app.config["QR_PAYLOAD_VERSION"],
    )
    blank.layout_json = layout_json_str
    db.session.commit()


@web_bp.route("/blanks/<blank_uuid>/pdf/questions", methods=["GET"])
@login_required
def blank_pdf_questions(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)
    _ensure_both_pdfs(blank)
    pdf_path = Path(current_app.config["PDF_DIR"]) / f"{blank.uuid}_questions.pdf"
    return send_file(
        str(pdf_path),
        as_attachment=True,
        download_name=f"blank_{blank.uuid}_voprosy_a4.pdf",
        mimetype="application/pdf",
    )


@web_bp.route("/blanks/<blank_uuid>/pdf/answers", methods=["GET"])
@login_required
def blank_pdf_answers(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)
    _ensure_both_pdfs(blank)
    pdf_path = Path(current_app.config["PDF_DIR"]) / f"{blank.uuid}_answers.pdf"
    return send_file(
        str(pdf_path),
        as_attachment=True,
        download_name=f"blank_{blank.uuid}_otvety_a6.pdf",
        mimetype="application/pdf",
    )


@web_bp.route("/blanks/<blank_uuid>/pdf", methods=["GET"])
@login_required
def blank_pdf_legacy(blank_uuid: str):
    """Раньше был один файл; перенаправляем на вопросы A4."""
    return redirect(url_for("web.blank_pdf_questions", blank_uuid=blank_uuid))

