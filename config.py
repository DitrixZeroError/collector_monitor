"""
Настройки приложения.
"""

import os
from pathlib import Path

# Корневая папка проекта (там, где лежит этот файл)
PROJECT_DIR = Path(__file__).resolve().parent

# Папка с данными: база, загруженные модели и журналы
DATA_DIR = PROJECT_DIR / "data"
DATABASE_PATH = DATA_DIR / "collector_monitor.sqlite3"

# Исходные справочники, которыми база заполняется при первом запуске
SEED_DIR = DATA_DIR / "seed"
SEED_OBJECTS_CSV = SEED_DIR / "objects.csv"
SEED_CHANNELS_CSV = SEED_DIR / "sensor_channels.csv"

# Куда складываются файлы, загруженные через раздел «Настройки»
UPLOADED_MODELS_DIR = DATA_DIR / "uploads" / "models"
UPLOADED_LOGS_DIR = DATA_DIR / "uploads" / "logs"

# Секретный ключ для подписи сессии (cookie). В реальной эксплуатации
# задайте свой через переменную окружения COLLECTOR_SECRET_KEY.
SECRET_KEY = os.environ.get("COLLECTOR_SECRET_KEY", "change-me-in-production")

# Максимальный размер загружаемого файла: 4 ГБ
MAX_UPLOAD_SIZE_BYTES = 200*1024*1024*20

# Сколько строк показывать на одной странице справочника
ROWS_PER_PAGE = 50

# Центр карты Москвы (широта, долгота) и начальный масштаб
MOSCOW_CENTER = (55.7558, 37.6173)
MAP_START_ZOOM = 11

# Логины и пароли, которые создаются при первом запуске.
# После входа обязательно смените пароли в справочнике «Пользователи».
DEFAULT_USERS = [
    {"login": "admin", "password": "admin123", "full_name": "Администратор системы", "role_code": "admin"},
    {"login": "dispatcher", "password": "disp123", "full_name": "Дежурный диспетчер", "role_code": "dispatcher"},
]
