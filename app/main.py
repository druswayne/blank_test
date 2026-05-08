import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from sqlalchemy import create_engine, inspect, text

from .models import db, login_manager
from .routes_web import web_bp
from .routes_api import api_bp


logger = logging.getLogger(__name__)


def _default_sqlite_uri() -> str:
    return f"sqlite:///{Path(__file__).resolve().parent.parent / 'data' / 'app.db'}"


def _normalize_database_uri(raw_uri: str) -> str:
    # Популярный формат postgres:// больше не поддерживается SQLAlchemy 2.x напрямую.
    if raw_uri.startswith("postgres://"):
        return raw_uri.replace("postgres://", "postgresql://", 1)
    return raw_uri


def _build_engine_options(database_uri: str) -> dict:
    # pool_pre_ping проверяет "живость" коннекта перед выдачей из пула.
    options = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_timeout": 15,
    }
    if database_uri.startswith("sqlite:///"):
        # Для SQLite используем timeout блокировок записи.
        options["connect_args"] = {"timeout": 15}
    else:
        # При ошибках соединения SQLAlchemy пересоздает невалидные коннекты в пуле.
        options["pool_reset_on_return"] = "rollback"
    return options


def _resolve_database_uri() -> str:
    default_uri = _default_sqlite_uri()
    raw_database_uri = os.getenv("DATABASE_URL", "").strip()
    if not raw_database_uri:
        return default_uri

    normalized_uri = _normalize_database_uri(raw_database_uri)
    try:
        test_engine = create_engine(normalized_uri, **_build_engine_options(normalized_uri))
        with test_engine.connect():
            pass
        test_engine.dispose()
        return normalized_uri
    except Exception as exc:
        logger.warning(
            "DATABASE_URL is invalid/unreachable, fallback to SQLite. Details: %s",
            exc,
        )
        return default_uri


def create_app() -> Flask:
    load_dotenv()

    static_dir = Path(__file__).resolve().parent.parent / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = _resolve_database_uri()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _build_engine_options(
        app.config["SQLALCHEMY_DATABASE_URI"]
    )

    # QR: подпись, чтобы нельзя было подделать blank_id внутри QR
    app.config["QR_HMAC_SECRET"] = os.getenv("QR_HMAC_SECRET", "dev-qr-secret-change-me")
    app.config["QR_PAYLOAD_VERSION"] = os.getenv("QR_PAYLOAD_VERSION", "v1")

    # PDF storage
    app.config["PDF_DIR"] = os.getenv(
        "PDF_DIR", str(Path(__file__).resolve().parent.parent / "data" / "pdfs")
    )
    Path(app.config["PDF_DIR"]).mkdir(parents=True, exist_ok=True)

    # Timeweb Cloud AI (генерация тестов): https://agent.timeweb.cloud/docs
    app.config["TIMEWEB_AI_BEARER_TOKEN"] = os.getenv("TIMEWEB_AI_BEARER_TOKEN", "").strip()
    app.config["TIMEWEB_AI_AGENT_ACCESS_ID"] = os.getenv(
        "TIMEWEB_AI_AGENT_ACCESS_ID", ""
    ).strip()
    app.config["TIMEWEB_AI_PROXY_SOURCE"] = os.getenv("TIMEWEB_AI_PROXY_SOURCE", "")

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "web.login"

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    # MVP: без миграций — создаем таблицы при старте разработки.
    with app.app_context():
        try:
            db.create_all()
            insp = inspect(db.engine)
            if insp.has_table("test_blanks"):
                cols = {c["name"] for c in insp.get_columns("test_blanks")}
                with db.engine.begin() as conn:
                    if "layout_json" not in cols:
                        conn.execute(text("ALTER TABLE test_blanks ADD COLUMN layout_json TEXT"))
                    if "is_public" not in cols:
                        conn.execute(text("ALTER TABLE test_blanks ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT 0"))
                    if "grade" not in cols:
                        conn.execute(text("ALTER TABLE test_blanks ADD COLUMN grade INTEGER"))
                    if "subject" not in cols:
                        conn.execute(text("ALTER TABLE test_blanks ADD COLUMN subject VARCHAR(50)"))
        except Exception as exc:
            db.session.rollback()
            logger.warning("Database init skipped due to connection error: %s", exc)

    return app

