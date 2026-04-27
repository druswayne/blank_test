import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from sqlalchemy import inspect, text

from .models import db, login_manager
from .routes_web import web_bp
from .routes_api import api_bp


def create_app() -> Flask:
    load_dotenv()

    static_dir = Path(__file__).resolve().parent.parent / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL", f"sqlite:///{Path(__file__).resolve().parent.parent / 'data' / 'app.db'}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # QR: подпись, чтобы нельзя было подделать blank_id внутри QR
    app.config["QR_HMAC_SECRET"] = os.getenv("QR_HMAC_SECRET", "dev-qr-secret-change-me")
    app.config["QR_PAYLOAD_VERSION"] = os.getenv("QR_PAYLOAD_VERSION", "v1")

    # PDF storage
    app.config["PDF_DIR"] = os.getenv(
        "PDF_DIR", str(Path(__file__).resolve().parent.parent / "data" / "pdfs")
    )
    Path(app.config["PDF_DIR"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "web.login"

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    # MVP: без миграций — создаем таблицы при старте разработки.
    with app.app_context():
        db.create_all()
        try:
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
        except Exception:
            db.session.rollback()

    return app

