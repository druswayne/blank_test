import os

from app import create_app


app = create_app()


if __name__ == "__main__":
    # Удобно для разработки: запускаем локально
    # Можно переопределить переменной окружения PORT.
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=True)

