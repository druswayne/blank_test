from pathlib import Path
import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
    Response,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func

from .models import TestBlank, TestBlankRating, TestQuestion, TestQuestionStats, User, db, login_manager
from .services.pdf_service import generate_blank_pdfs
from .services.timeweb_ai_service import (
    TimewebAIError,
    chat_completion,
    extract_message_content,
    strip_markdown_fences,
)
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
    "belarusian_language": "Белорусский язык",
    "belarusian_literature": "Белорусская литература",
    "russian_language": "Русский язык",
    "russian_literature": "Русская литература",
    "mathematics": "Математика",
    "algebra": "Алгебра",
    "geometry": "Геометрия",
    "informatics": "Информатика",
    "human_and_world": "Человек и мир",
    "world_history": "Всемирная история",
    "history_of_belarus": "История Беларуси",
    "social_studies": "Обществоведение",
    "geography": "География",
    "biology": "Биология",
    "physics": "Физика",
    "chemistry": "Химия",
    "astronomy": "Астрономия",
    "foreign_language": "Иностранный язык",
    "fine_arts": "Изобразительное искусство",
    "music": "Музыка",
    "labor_training": "Трудовое обучение",
    "arts_omhk": "Искусство (Отечественная и мировая художественная культура)",
    "drafting": "Черчение",
    "physical_education_health": "Физическая культура и здоровье",
    "life_safety_basics_obzh": "Основы безопасности жизнедеятельности (ОБЖ)",
    "preconscription_training": "Допризывная подготовка",
    "medical_training": "Медицинская подготовка",
}


def _safe_download_title(title: str | None, fallback: str = "Тест") -> str:
    raw = (title or "").strip() or fallback
    # Для имени файла убираем запрещённые в Windows символы.
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


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


@web_bp.route("/robots.txt", methods=["GET"])
def robots_txt():
    # Явно разрешаем индексацию публичной части сайта и даем ссылку на карту.
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /dashboard",
        "Disallow: /tests",
        "Disallow: /tests/",
        "",
        f"Sitemap: {url_for('web.sitemap_xml', _external=True)}",
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain; charset=utf-8")


@web_bp.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    pages = [
        {
            "loc": url_for("web.index", _external=True),
            "changefreq": "weekly",
            "priority": "1.0",
        },
        {
            "loc": url_for("web.login", _external=True),
            "changefreq": "monthly",
            "priority": "0.4",
        },
        {
            "loc": url_for("web.register", _external=True),
            "changefreq": "monthly",
            "priority": "0.5",
        },
    ]
    url_items = []
    for page in pages:
        url_items.append(
            "\n".join(
                [
                    "  <url>",
                    f"    <loc>{page['loc']}</loc>",
                    f"    <changefreq>{page['changefreq']}</changefreq>",
                    f"    <priority>{page['priority']}</priority>",
                    "  </url>",
                ]
            )
        )
    xml = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
            *url_items,
            "</urlset>",
            "",
        ]
    )
    return Response(xml, mimetype="application/xml; charset=utf-8")


@web_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))

    if request.method == "POST":
        login = request.form.get("login", "").strip().lower()
        password = request.form.get("password", "").strip()
        password2 = request.form.get("password2", "").strip()

        if not login or not password:
            flash("Заполните логин и пароль", "error")
            return redirect(url_for("web.register"))
        if password != password2:
            flash("Пароли не совпадают", "error")
            return redirect(url_for("web.register"))

        if User.query.filter(func.lower(User.login) == login).first():
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
        login_val = request.form.get("login", "").strip().lower()
        password = request.form.get("password", "").strip()

        user = User.query.filter(func.lower(User.login) == login_val).first()
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
    grade_raw = (request.args.get("grade") or "").strip()
    qc_min_raw = (request.args.get("qc_min") or "").strip()
    qc_max_raw = (request.args.get("qc_max") or "").strip()
    rating_min_raw = (request.args.get("rating_min") or "").strip()
    page_raw = (request.args.get("page") or "1").strip()

    grade_val: int | None = None
    if grade_raw:
        try:
            g = int(grade_raw)
            if 1 <= g <= 11:
                grade_val = g
        except Exception:
            grade_val = None

    qc_min: int | None = None
    if qc_min_raw:
        try:
            qmn = int(qc_min_raw)
            if 1 <= qmn <= MAX_TEST_QUESTIONS:
                qc_min = qmn
        except Exception:
            qc_min = None

    qc_max: int | None = None
    if qc_max_raw:
        try:
            qmx = int(qc_max_raw)
            if 1 <= qmx <= MAX_TEST_QUESTIONS:
                qc_max = qmx
        except Exception:
            qc_max = None

    if qc_min is not None and qc_max is not None and qc_min > qc_max:
        qc_min, qc_max = qc_max, qc_min

    rating_min: int | None = None
    if rating_min_raw:
        try:
            rmn = int(rating_min_raw)
            if 1 <= rmn <= 5:
                rating_min = rmn
        except Exception:
            rating_min = None

    try:
        page = max(1, int(page_raw))
    except Exception:
        page = 1
    per_page = 12

    rating_subq = (
        db.session.query(
            TestBlankRating.blank_id.label("blank_id"),
            func.avg(TestBlankRating.score).label("avg_score"),
        )
        .group_by(TestBlankRating.blank_id)
        .subquery()
    )

    query = (
        TestBlank.query.outerjoin(rating_subq, TestBlank.id == rating_subq.c.blank_id)
        .filter(TestBlank.is_public.is_(True))
    )
    if q:
        query = query.filter(TestBlank.title.ilike(f"%{q}%"))
    if subject_keys:
        query = query.filter(TestBlank.subject.in_(subject_keys))
    if grade_val is not None:
        query = query.filter(TestBlank.grade == grade_val)
    if qc_min is not None:
        query = query.filter(TestBlank.question_count >= qc_min)
    if qc_max is not None:
        query = query.filter(TestBlank.question_count <= qc_max)
    if rating_min is not None:
        query = query.filter(func.coalesce(rating_subq.c.avg_score, 0.0) >= rating_min)
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
        grade=grade_val,
        qc_min=qc_min,
        qc_max=qc_max,
        rating_min=rating_min,
        subject_labels=SUBJECT_LABELS,
        page=page,
        pages=pages,
        total=total,
        query_args={
            "q": q,
            "subject": subject_keys,
            "grade": str(grade_val) if grade_val is not None else "",
            "qc_min": str(qc_min) if qc_min is not None else "",
            "qc_max": str(qc_max) if qc_max is not None else "",
            "rating_min": str(rating_min) if rating_min is not None else "",
        },
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
        form_title = (request.form.get("title") or "").strip()
        form_is_public = (request.form.get("is_public") == "on")
        form_grade_raw = (request.form.get("grade") or "").strip()
        form_subject = (request.form.get("subject") or "").strip()
        raw_body = request.form.get("test_body", "") or ""
        form_grade = None
        if form_grade_raw:
            try:
                form_grade = int(form_grade_raw)
            except Exception:
                form_grade = None
        try:
            title, is_public, grade, subject = _meta_from_form()
        except ValueError as e:
            flash(str(e), "error")
            return render_template(
                "test_editor.html",
                mode="create",
                subject_labels=SUBJECT_LABELS,
                body_text=raw_body,
                form_title=form_title,
                form_is_public=form_is_public,
                form_grade=form_grade,
                form_subject=form_subject,
            )
        try:
            if looks_like_quill_html(raw_body):
                parsed = parse_structured_quill_html(raw_body, max_questions=MAX_TEST_QUESTIONS)
            else:
                body = normalize_test_body_input(raw_body)
                parsed = parse_structured_test_body(body, max_questions=MAX_TEST_QUESTIONS)
        except ValueError as e:
            flash(str(e), "error")
            return render_template(
                "test_editor.html",
                mode="create",
                subject_labels=SUBJECT_LABELS,
                body_text=raw_body,
                form_title=form_title,
                form_is_public=form_is_public,
                form_grade=form_grade,
                form_subject=form_subject,
            )

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
        form_title="",
        form_is_public=False,
        form_grade=None,
        form_subject="",
    )


@web_bp.route("/tests/ai-generate", methods=["POST"])
@login_required
def tests_ai_generate():
    if not request.is_json:
        return jsonify(ok=False, error="Ожидался JSON (Content-Type: application/json)"), 400
    payload = request.get_json(silent=True) or {}
    user_prompt = (payload.get("prompt") or "").strip()
    if not user_prompt:
        return jsonify(ok=False, error="Введите тему или материал для генерации теста"), 400
    if len(user_prompt) > 12000:
        return jsonify(ok=False, error="Текст запроса слишком длинный (максимум 12000 символов)"), 400

    token = (current_app.config.get("TIMEWEB_AI_BEARER_TOKEN") or "").strip()
    agent_id = (current_app.config.get("TIMEWEB_AI_AGENT_ACCESS_ID") or "").strip()
    if not token or not agent_id:
        return (
            jsonify(
                ok=False,
                error=(
                    "Генерация через ИИ не настроена. Задайте переменные окружения "
                    "TIMEWEB_AI_BEARER_TOKEN и TIMEWEB_AI_AGENT_ACCESS_ID "
                    "(см. документацию Timeweb Cloud AI)."
                ),
            ),
            503,
        )

    proxy_source = current_app.config.get("TIMEWEB_AI_PROXY_SOURCE")
    if proxy_source is None:
        proxy_source = ""

    try:
        raw = chat_completion(
            agent_access_id=agent_id,
            bearer_token=token,
            x_proxy_source=str(proxy_source),
            messages=[{"role": "user", "content": user_prompt}],
        )
    except TimewebAIError as e:
        return jsonify(ok=False, error=str(e)), 502

    content = extract_message_content(raw)
    if not content:
        return jsonify(ok=False, error="Пустой ответ агента"), 502

    text = strip_markdown_fences(content)
    return jsonify(ok=True, text=text)


@web_bp.route("/tests/<blank_uuid>/edit", methods=["GET", "POST"])
@login_required
def tests_edit(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)

    if request.method == "POST":
        form_title = (request.form.get("title") or "").strip()
        form_is_public = (request.form.get("is_public") == "on")
        form_grade_raw = (request.form.get("grade") or "").strip()
        form_subject = (request.form.get("subject") or "").strip()
        raw_body = request.form.get("test_body", "") or ""
        form_grade = None
        if form_grade_raw:
            try:
                form_grade = int(form_grade_raw)
            except Exception:
                form_grade = None
        try:
            title, is_public, grade, subject = _meta_from_form()
        except ValueError as e:
            flash(str(e), "error")
            return render_template(
                "test_editor.html",
                mode="edit",
                blank=blank,
                subject_labels=SUBJECT_LABELS,
                body_text=raw_body,
                form_title=form_title,
                form_is_public=form_is_public,
                form_grade=form_grade,
                form_subject=form_subject,
            )
        try:
            if looks_like_quill_html(raw_body):
                parsed = parse_structured_quill_html(raw_body, max_questions=MAX_TEST_QUESTIONS)
            else:
                body = normalize_test_body_input(raw_body)
                parsed = parse_structured_test_body(body, max_questions=MAX_TEST_QUESTIONS)
        except ValueError as e:
            flash(str(e), "error")
            return render_template(
                "test_editor.html",
                mode="edit",
                blank=blank,
                subject_labels=SUBJECT_LABELS,
                body_text=raw_body,
                form_title=form_title,
                form_is_public=form_is_public,
                form_grade=form_grade,
                form_subject=form_subject,
            )

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
        form_title=blank.title or "",
        form_is_public=bool(blank.is_public),
        form_grade=blank.grade,
        form_subject=blank.subject or "",
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
    sort = (request.args.get("sort") or "question_asc").strip()

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

    if sort == "question_desc":
        rows.sort(key=lambda r: int(r["question_number"]), reverse=True)
    elif sort == "correct_pct_asc":
        rows.sort(key=lambda r: (float(r["correct_pct"]), int(r["question_number"])))
    elif sort == "correct_pct_desc":
        rows.sort(key=lambda r: (float(r["correct_pct"]), int(r["question_number"])), reverse=True)
    else:
        sort = "question_asc"
        rows.sort(key=lambda r: int(r["question_number"]))

    return render_template(
        "tests_stats.html",
        blank=blank,
        rows=rows,
        total_attempts=total_attempts,
        total_correct=total_correct,
        overall_pct=overall_pct,
        sort=sort,
    )


@web_bp.route("/tests/<blank_uuid>/stats/reset", methods=["POST"])
@login_required
def tests_stats_reset(blank_uuid: str):
    blank = TestBlank.query.filter_by(uuid=blank_uuid, owner_id=current_user.id).first()
    if not blank:
        abort(404)

    TestQuestionStats.query.filter_by(blank_id=blank.id).delete(synchronize_session=False)
    db.session.commit()
    flash("Статистика теста обнулена.", "success")
    return redirect(url_for("web.tests_stats", blank_uuid=blank.uuid))


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
def blank_pdf_questions(blank_uuid: str):
    blank = _get_accessible_blank(blank_uuid)
    if not blank:
        abort(404)
    _ensure_both_pdfs(blank)
    pdf_path = Path(current_app.config["PDF_DIR"]) / f"{blank.uuid}_questions.pdf"
    title = _safe_download_title(blank.title)
    return send_file(
        str(pdf_path),
        as_attachment=True,
        download_name=f"{title}.pdf",
        mimetype="application/pdf",
    )


@web_bp.route("/blanks/<blank_uuid>/pdf/answers", methods=["GET"])
def blank_pdf_answers(blank_uuid: str):
    blank = _get_accessible_blank(blank_uuid)
    if not blank:
        abort(404)
    _ensure_both_pdfs(blank)
    pdf_path = Path(current_app.config["PDF_DIR"]) / f"{blank.uuid}_answers.pdf"
    title = _safe_download_title(blank.title)
    return send_file(
        str(pdf_path),
        as_attachment=True,
        download_name=f"бланк ответа {title}.pdf",
        mimetype="application/pdf",
    )


@web_bp.route("/blanks/<blank_uuid>/pdf", methods=["GET"])
def blank_pdf_legacy(blank_uuid: str):
    """Раньше был один файл; перенаправляем на вопросы A4."""
    return redirect(url_for("web.blank_pdf_questions", blank_uuid=blank_uuid))

