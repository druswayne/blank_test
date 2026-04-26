from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func

from .models import TestBlank, TestBlankRating, TestQuestion, TestQuestionStats, User, db, login_manager
from .services.pdf_service import generate_blank_pdfs
from .services.test_body_parser import (
    blank_to_editor_html,
    looks_like_quill_html,
    normalize_test_body_input,
    parse_structured_quill_html,
    parse_structured_test_body,
)

web_bp = Blueprint("web", __name__)

MAX_TEST_QUESTIONS = 10

SUBJECT_LABELS = {
    "math": "Математика",
    "russian": "Русский язык",
}


def _meta_from_form() -> tuple[str | None, bool, int | None, str | None]:
    title = (request.form.get("title") or "").strip() or None
    is_public = request.form.get("is_public") == "on"
    grade_raw = (request.form.get("grade") or "").strip()
    if not grade_raw:
        grade = None
    else:
        grade = int(grade_raw)
        if grade < 1 or grade > 11:
            raise ValueError("Класс должен быть от 1 до 11")
    sub = (request.form.get("subject") or "").strip()
    if sub and sub not in SUBJECT_LABELS:
        raise ValueError("Неверный предмет")
    subject = sub or None
    return title, is_public, grade, subject


def _apply_questions(blank: TestBlank, parsed: list[dict]) -> None:
    TestQuestion.query.filter_by(blank_id=blank.id).delete(synchronize_session=False)
    blank.question_count = len(parsed)
    for i, item in enumerate(parsed, start=1):
        o = item["options"]
        db.session.add(
            TestQuestion(
                blank_id=blank.id,
                question_number=i,
                question_text=item["text"],
                option_a=o[0],
                option_b=o[1],
                option_c=o[2],
                option_d=o[3],
                correct_index=int(item["correct"]),
            )
        )


def _regenerate_pdfs(blank: TestBlank) -> None:
    pdf_dir = current_app.config["PDF_DIR"]
    _pq, _pa, layout_json_str = generate_blank_pdfs(
        blank=blank,
        pdf_dir=pdf_dir,
        qr_secret=current_app.config["QR_HMAC_SECRET"],
        qr_payload_version=current_app.config["QR_PAYLOAD_VERSION"],
    )
    blank.layout_json = layout_json_str
    db.session.commit()


def _ratings_for_blank_ids(blank_ids: list[int], user_id: int) -> tuple[dict[int, tuple[float, int]], dict[int, int]]:
    if not blank_ids:
        return {}, {}

    agg_rows = (
        db.session.query(
            TestBlankRating.blank_id,
            func.avg(TestBlankRating.score),
            func.count(TestBlankRating.id),
        )
        .filter(TestBlankRating.blank_id.in_(blank_ids))
        .group_by(TestBlankRating.blank_id)
        .all()
    )
    stats: dict[int, tuple[float, int]] = {
        int(blank_id): (float(avg_score or 0.0), int(cnt or 0)) for blank_id, avg_score, cnt in agg_rows
    }

    user_rows = (
        db.session.query(TestBlankRating.blank_id, TestBlankRating.score)
        .filter(TestBlankRating.blank_id.in_(blank_ids), TestBlankRating.user_id == user_id)
        .all()
    )
    user_scores: dict[int, int] = {int(blank_id): int(score) for blank_id, score in user_rows}
    return stats, user_scores


@web_bp.route("/", methods=["GET"])
def index():
    return render_template("home.html")


@web_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))

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
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))

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
    return render_template("dashboard.html")


@web_bp.route("/tests", methods=["GET"])
@login_required
def tests_hub():
    return render_template("tests_hub.html")


@web_bp.route("/tests/mine", methods=["GET"])
@login_required
def tests_mine():
    q = request.args.get("q", "").strip()
    grade_raw = (request.args.get("grade") or "").strip()
    subject = (request.args.get("subject") or "").strip()
    sort = (request.args.get("sort") or "created_desc").strip()
    page_raw = (request.args.get("page") or "1").strip()

    try:
        page = max(1, int(page_raw))
    except Exception:
        page = 1
    per_page = 12

    query = TestBlank.query.filter_by(owner_id=current_user.id)
    if q:
        query = query.filter(TestBlank.title.ilike(f"%{q}%"))

    grade_val: int | None = None
    if grade_raw:
        try:
            gv = int(grade_raw)
            if 1 <= gv <= 11:
                grade_val = gv
                query = query.filter(TestBlank.grade == gv)
        except Exception:
            grade_val = None

    subject_val: str | None = None
    if subject and subject in SUBJECT_LABELS:
        subject_val = subject
        query = query.filter(TestBlank.subject == subject)

    if sort == "grade_asc":
        query = query.order_by(TestBlank.grade.asc(), TestBlank.created_at.desc())
    elif sort == "grade_desc":
        query = query.order_by(TestBlank.grade.desc(), TestBlank.created_at.desc())
    elif sort == "subject_asc":
        query = query.order_by(TestBlank.subject.asc(), TestBlank.created_at.desc())
    elif sort == "subject_desc":
        query = query.order_by(TestBlank.subject.desc(), TestBlank.created_at.desc())
    elif sort == "title_asc":
        query = query.order_by(TestBlank.title.asc(), TestBlank.created_at.desc())
    elif sort == "title_desc":
        query = query.order_by(TestBlank.title.desc(), TestBlank.created_at.desc())
    else:
        sort = "created_desc"
        query = query.order_by(TestBlank.created_at.desc())

    total = query.count()
    pages = (total + per_page - 1) // per_page if total else 0
    if pages and page > pages:
        page = pages

    blanks = query.offset((page - 1) * per_page).limit(per_page).all() if total else []
    rating_stats, my_ratings = _ratings_for_blank_ids([b.id for b in blanks], current_user.id)

    query_args = {
        "q": q,
        "grade": str(grade_val) if grade_val is not None else "",
        "subject": subject_val or "",
        "sort": sort,
    }
    return render_template(
        "tests_mine.html",
        blanks=blanks,
        subject_labels=SUBJECT_LABELS,
        q=q,
        grade=grade_val,
        subject=subject_val,
        sort=sort,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
        query_args=query_args,
        rating_stats=rating_stats,
        my_ratings=my_ratings,
        return_to=request.full_path.rstrip("?"),
    )


@web_bp.route("/tests/search", methods=["GET"])
@login_required
def tests_search():
    q = request.args.get("q", "").strip()
    subject_keys = [s for s in request.args.getlist("subject") if s in SUBJECT_LABELS]
    page_raw = (request.args.get("page") or "1").strip()
    try:
        page = max(1, int(page_raw))
    except Exception:
        page = 1
    per_page = 12
    query = TestBlank.query.filter(TestBlank.is_public.is_(True))
    if q:
        query = query.filter(TestBlank.title.ilike(f"%{q}%"))
    if subject_keys:
        query = query.filter(TestBlank.subject.in_(subject_keys))
    query = query.order_by(TestBlank.created_at.desc())
    total = query.count()
    pages = (total + per_page - 1) // per_page if total else 0
    if pages and page > pages:
        page = pages
    items = query.offset((page - 1) * per_page).limit(per_page).all() if total else []
    rating_stats, my_ratings = _ratings_for_blank_ids([t.id for t in items], current_user.id)
    return render_template(
        "tests_search.html",
        items=items,
        q=q,
        subject_keys=subject_keys,
        subject_labels=SUBJECT_LABELS,
        page=page,
        pages=pages,
        total=total,
        query_args={"q": q, "subject": subject_keys},
        rating_stats=rating_stats,
        my_ratings=my_ratings,
        return_to=request.full_path.rstrip("?"),
    )


def _get_accessible_blank(blank_uuid: str) -> TestBlank | None:
    blank = TestBlank.query.filter_by(uuid=blank_uuid).first()
    if not blank:
        return None
    if blank.owner_id == current_user.id or bool(blank.is_public):
        return blank
    return None


@web_bp.route("/tests/<blank_uuid>/preview", methods=["GET"])
@login_required
def tests_preview(blank_uuid: str):
    blank = _get_accessible_blank(blank_uuid)
    if not blank:
        abort(404)
    return render_template("tests_preview.html", blank=blank, subject_labels=SUBJECT_LABELS)


@web_bp.route("/tests/<blank_uuid>/copy", methods=["POST"])
@login_required
def tests_copy(blank_uuid: str):
    src = _get_accessible_blank(blank_uuid)
    if not src:
        abort(404)
    if src.owner_id == current_user.id:
        flash("Свои тесты копировать нельзя.", "error")
        nxt = request.form.get("next") or url_for("web.tests_search")
        return redirect(nxt)

    new_title = (src.title or f"Тест {src.uuid[:8]}").strip()
    clone = TestBlank(
        owner_id=current_user.id,
        title=f"{new_title} (копия)",
        is_public=False,
        grade=src.grade,
        subject=src.subject,
        question_count=src.question_count,
    )
    db.session.add(clone)
    db.session.flush()

    src_questions = (
        TestQuestion.query.filter_by(blank_id=src.id).order_by(TestQuestion.question_number.asc()).all()
    )
    for q in src_questions:
        db.session.add(
            TestQuestion(
                blank_id=clone.id,
                question_number=q.question_number,
                question_text=q.question_text,
                option_a=q.option_a,
                option_b=q.option_b,
                option_c=q.option_c,
                option_d=q.option_d,
                correct_index=q.correct_index,
            )
        )
    db.session.commit()
    db.session.refresh(clone)
    try:
        _regenerate_pdfs(clone)
    except Exception:
        flash("Тест скопирован, но PDF не удалось сгенерировать. Попробуйте сократить текст.", "error")
    else:
        flash("Тест успешно скопирован в «Мои тесты».", "success")

    nxt = request.form.get("next") or url_for("web.tests_mine")
    return redirect(nxt)


@web_bp.route("/tests/<blank_uuid>/rate", methods=["POST"])
@login_required
def rate_test(blank_uuid: str):
    blank = _get_accessible_blank(blank_uuid)
    if not blank:
        abort(404)

    try:
        score = int((request.form.get("score") or "").strip())
    except Exception:
        score = 0
    if score < 1 or score > 5:
        flash("Оценка должна быть от 1 до 5.", "error")
        nxt = request.form.get("next") or url_for("web.tests_search")
        return redirect(nxt)

    existing = TestBlankRating.query.filter_by(blank_id=blank.id, user_id=current_user.id).first()
    if existing:
        flash("Вы уже оценили этот тест.", "error")
    else:
        db.session.add(TestBlankRating(blank_id=blank.id, user_id=current_user.id, score=score))
        db.session.commit()
        flash("Спасибо за оценку!", "success")

    nxt = request.form.get("next") or url_for("web.tests_search")
    return redirect(nxt)


@web_bp.route("/tests/new", methods=["GET", "POST"])
@login_required
def tests_new():
    if request.method == "POST":
        try:
            title, is_public, grade, subject = _meta_from_form()
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("web.tests_new"))
        raw_body = request.form.get("test_body", "") or ""
        try:
            if looks_like_quill_html(raw_body):
                parsed = parse_structured_quill_html(raw_body, max_questions=MAX_TEST_QUESTIONS)
            else:
                body = normalize_test_body_input(raw_body)
                parsed = parse_structured_test_body(body, max_questions=MAX_TEST_QUESTIONS)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("web.tests_new"))

        blank = TestBlank(
            owner_id=current_user.id,
            title=title,
            is_public=is_public,
            grade=grade,
            subject=subject,
            question_count=0,
        )
        db.session.add(blank)
        db.session.flush()
        _apply_questions(blank, parsed)
        db.session.commit()
        db.session.refresh(blank)
        try:
            _regenerate_pdfs(blank)
        except Exception:
            flash("Тест сохранён, но не удалось сгенерировать PDF. Попробуйте сократить текст.", "error")
            return redirect(url_for("web.tests_edit", blank_uuid=blank.uuid))
        flash("Тест создан, PDF сгенерированы.", "success")
        return redirect(url_for("web.tests_mine"))

    return render_template(
        "test_editor.html",
        mode="create",
        subject_labels=SUBJECT_LABELS,
        body_text="",
    )


@web_bp.route("/tests/<blank_uuid>/edit", methods=["GET", "POST"])
@login_required
def tests_edit(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)

    if request.method == "POST":
        try:
            title, is_public, grade, subject = _meta_from_form()
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("web.tests_edit", blank_uuid=blank_uuid))
        raw_body = request.form.get("test_body", "") or ""
        try:
            if looks_like_quill_html(raw_body):
                parsed = parse_structured_quill_html(raw_body, max_questions=MAX_TEST_QUESTIONS)
            else:
                body = normalize_test_body_input(raw_body)
                parsed = parse_structured_test_body(body, max_questions=MAX_TEST_QUESTIONS)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("web.tests_edit", blank_uuid=blank_uuid))

        blank.title = title
        blank.is_public = is_public
        blank.grade = grade
        blank.subject = subject
        _apply_questions(blank, parsed)
        db.session.commit()
        db.session.refresh(blank)
        try:
            _regenerate_pdfs(blank)
        except Exception:
            flash("Изменения сохранены, но PDF не обновлены. Попробуйте сократить текст.", "error")
            return redirect(url_for("web.tests_edit", blank_uuid=blank.uuid))
        flash("Тест сохранён, PDF пересозданы.", "success")
        return redirect(url_for("web.tests_mine"))

    return render_template(
        "test_editor.html",
        mode="edit",
        blank=blank,
        subject_labels=SUBJECT_LABELS,
        body_text=blank_to_editor_html(blank),
    )


@web_bp.route("/tests/<blank_uuid>/delete", methods=["POST"])
@login_required
def tests_delete(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)

    pdf_dir = Path(current_app.config["PDF_DIR"])
    files_to_remove = [
        pdf_dir / f"{blank.uuid}_questions.pdf",
        pdf_dir / f"{blank.uuid}_answers.pdf",
    ]

    db.session.delete(blank)
    db.session.commit()

    for fp in files_to_remove:
        try:
            fp.unlink(missing_ok=True)
        except Exception:
            # Не прерываем запрос, если файл уже удалён или занят.
            pass

    flash("Тест удалён.", "success")
    nxt = request.form.get("next") or url_for("web.tests_mine")
    return redirect(nxt)


@web_bp.route("/tests/<blank_uuid>/stats", methods=["GET"])
@login_required
def tests_stats(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)

    qs = sorted(blank.questions, key=lambda x: x.question_number)
    stats_rows = TestQuestionStats.query.filter_by(blank_id=blank.id).all()
    stats_by_qid = {int(s.question_id): s for s in stats_rows}

    rows: list[dict] = []
    for q in qs:
        s = stats_by_qid.get(int(q.id))
        attempts = int(s.attempts_total) if s else 0
        correct = int(s.correct_total) if s else 0
        a_cnt = int(s.option_a_total) if s else 0
        b_cnt = int(s.option_b_total) if s else 0
        c_cnt = int(s.option_c_total) if s else 0
        d_cnt = int(s.option_d_total) if s else 0

        if attempts > 0:
            correct_pct = (correct * 100.0) / attempts
            a_pct = (a_cnt * 100.0) / attempts
            b_pct = (b_cnt * 100.0) / attempts
            c_pct = (c_cnt * 100.0) / attempts
            d_pct = (d_cnt * 100.0) / attempts
        else:
            correct_pct = a_pct = b_pct = c_pct = d_pct = 0.0

        rows.append(
            {
                "question_number": q.question_number,
                "attempts": attempts,
                "correct": correct,
                "correct_pct": correct_pct,
                "options": {
                    "A": {"count": a_cnt, "pct": a_pct},
                    "B": {"count": b_cnt, "pct": b_pct},
                    "C": {"count": c_cnt, "pct": c_pct},
                    "D": {"count": d_cnt, "pct": d_pct},
                },
            }
        )

    total_attempts = sum(r["attempts"] for r in rows)
    total_correct = sum(r["correct"] for r in rows)
    overall_pct = (total_correct * 100.0 / total_attempts) if total_attempts > 0 else 0.0

    return render_template(
        "tests_stats.html",
        blank=blank,
        rows=rows,
        total_attempts=total_attempts,
        total_correct=total_correct,
        overall_pct=overall_pct,
    )


@web_bp.route("/blanks/new", methods=["GET", "POST"])
@login_required
def new_blank():
    if request.method == "GET":
        return redirect(url_for("web.tests_new"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        question_count = int(request.form.get("question_count", "1"))
        question_count = max(1, min(10, question_count))

        blank = TestBlank(
            owner_id=current_user.id,
            title=title,
            question_count=question_count,
            is_public=False,
            grade=None,
            subject=None,
        )
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

    abort(405)


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
    blank = _get_accessible_blank(blank_uuid)
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
    blank = _get_accessible_blank(blank_uuid)
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

