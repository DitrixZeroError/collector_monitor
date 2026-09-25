"""
Точка входа веб-приложения.

Запуск:
    python app.py
После запуска откройте в браузере http://127.0.0.1:5000
"""

from flask import Flask

import config
from auth import auth_blueprint, load_current_user
from database import close_db, initialize_database
from routes_api import api_blueprint, pages_blueprint
from routes_settings import settings_blueprint


def create_app():
    """Создаёт и настраивает приложение Flask."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_SIZE_BYTES

    # Создаём таблицы и заполняем справочники при первом запуске
    initialize_database()

    # Перед каждым запросом узнаём, кто вошёл; после — закрываем базу
    app.before_request(load_current_user)
    app.teardown_appcontext(close_db)

    # Подключаем разделы приложения
    app.register_blueprint(auth_blueprint)
    app.register_blueprint(pages_blueprint)
    app.register_blueprint(api_blueprint)
    app.register_blueprint(settings_blueprint)

    return app


if __name__ == "__main__":
    application = create_app()
    # threaded=True — чтобы страницы открывались, пока идёт эмуляция.
    # use_reloader=False — иначе Flask запустит приложение дважды
    # и эмуляция будет жить в «чужом» процессе.
    # application.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
    application.run(host="127.0.0.1", port=5000, debug=False, threaded=True, use_reloader=False)
