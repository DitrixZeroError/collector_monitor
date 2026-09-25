"""
Чтение настроек прогнозирования из базы: включён ли прогноз и какой файл модели выбран.

Вынесено в отдельный модуль, потому что эти настройки нужны и разделу
«Настройки», и API главной страницы, и потоку эмуляции.
"""

import config
from database import open_connection, read_setting
from services.model_service import active_model_holder


def load_model_if_prediction_enabled():
    """
    Вызывается из потока эмуляции в конце каждого модельного часа.
    Возвращает модель, если прогнозирование включено, иначе None.

    Поток эмуляции живёт вне веб-запроса, поэтому открываем собственное
    соединение с базой и сразу его закрываем.
    """
    connection = open_connection()
    try:
        if read_setting(connection, "prediction_enabled", "0") != "1":
            return None
        active_model_id = read_setting(connection, "active_model_id")
        model_row = connection.execute(
            "SELECT stored_name FROM ml_models WHERE id = ?", (active_model_id,)
        ).fetchone()
    finally:
        connection.close()

    if model_row is None:
        return None
    return active_model_holder.get(config.UPLOADED_MODELS_DIR / model_row["stored_name"])
