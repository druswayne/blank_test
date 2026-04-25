import uuid
from datetime import datetime

import bcrypt
from flask_login import LoginManager, UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.LargeBinary(60), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    blanks = db.relationship("TestBlank", back_populates="owner", cascade="all, delete-orphan")
    rated_blanks = db.relationship("TestBlankRating", back_populates="user", cascade="all, delete-orphan")

    @staticmethod
    def hash_password(password: str) -> bytes:
        password_bytes = password.encode("utf-8")
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password_bytes, salt)

    def check_password(self, password: str) -> bool:
        return bcrypt.checkpw(password.encode("utf-8"), self.password_hash)


class TestBlank(db.Model):
    __tablename__ = "test_blanks"

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=True)

    # Опционально: публичный поиск, класс (1–11), предмет (код: math, russian)
    is_public = db.Column(db.Boolean, nullable=False, default=False)
    grade = db.Column(db.Integer, nullable=True)
    subject = db.Column(db.String(50), nullable=True)

    question_count = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # JSON: координаты вёрстки для проверки по фото (см. pdf_service.build_layout_for_verify)
    layout_json = db.Column(db.Text, nullable=True)

    owner = db.relationship("User", back_populates="blanks")
    questions = db.relationship(
        "TestQuestion", back_populates="blank", order_by="TestQuestion.question_number", cascade="all, delete-orphan"
    )
    ratings = db.relationship("TestBlankRating", back_populates="blank", cascade="all, delete-orphan")


class TestQuestion(db.Model):
    __tablename__ = "test_questions"

    id = db.Column(db.Integer, primary_key=True)
    blank_id = db.Column(db.Integer, db.ForeignKey("test_blanks.id"), nullable=False, index=True)
    question_number = db.Column(db.Integer, nullable=False)  # 1..question_count

    question_text = db.Column(db.Text, nullable=False)

    option_a = db.Column(db.Text, nullable=False)
    option_b = db.Column(db.Text, nullable=False)
    option_c = db.Column(db.Text, nullable=False)
    option_d = db.Column(db.Text, nullable=False)

    correct_index = db.Column(db.Integer, nullable=False)  # 0=A,1=B,2=C,3=D

    blank = db.relationship("TestBlank", back_populates="questions")
    stats = db.relationship("TestQuestionStats", back_populates="question", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint("blank_id", "question_number", name="uq_question_number_per_blank"),)


class TestQuestionStats(db.Model):
    __tablename__ = "test_question_stats"

    id = db.Column(db.Integer, primary_key=True)
    blank_id = db.Column(db.Integer, db.ForeignKey("test_blanks.id"), nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey("test_questions.id"), nullable=False, unique=True, index=True)

    attempts_total = db.Column(db.Integer, nullable=False, default=0)
    correct_total = db.Column(db.Integer, nullable=False, default=0)
    option_a_total = db.Column(db.Integer, nullable=False, default=0)
    option_b_total = db.Column(db.Integer, nullable=False, default=0)
    option_c_total = db.Column(db.Integer, nullable=False, default=0)
    option_d_total = db.Column(db.Integer, nullable=False, default=0)

    question = db.relationship("TestQuestion", back_populates="stats")


class TestBlankRating(db.Model):
    __tablename__ = "test_blank_ratings"

    id = db.Column(db.Integer, primary_key=True)
    blank_id = db.Column(db.Integer, db.ForeignKey("test_blanks.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    score = db.Column(db.Integer, nullable=False)  # 1..5
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    blank = db.relationship("TestBlank", back_populates="ratings")
    user = db.relationship("User", back_populates="rated_blanks")

    __table_args__ = (db.UniqueConstraint("blank_id", "user_id", name="uq_blank_user_rating"),)


@login_manager.user_loader
def load_user(user_id: str):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None

