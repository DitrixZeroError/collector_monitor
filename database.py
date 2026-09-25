"""
Работа с базой данных SQLite.

Здесь описаны:
  * структура таблиц (схема);
  * функция получения соединения с базой;
  * первоначальное заполнение базы (роли, пользователи, справочники из CSV).

SQLite выбрана потому, что это обычный файл и ничего дополнительно
устанавливать не нужно.
"""

import csv
import math
import sqlite3
from datetime import datetime

from flask import g
from werkzeug.security import generate_password_hash

import config

# ---------------------------------------------------------------------------
# Схема базы данных
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS roles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT    NOT NULL UNIQUE,   -- машинное имя: admin, dispatcher
    name                TEXT    NOT NULL,          -- название для людей
    description         TEXT    NOT NULL DEFAULT '',
    can_manage_settings INTEGER NOT NULL DEFAULT 0 -- 1 = доступен раздел «Настройки»
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    login         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,                -- храним только хэш, не сам пароль
    full_name     TEXT    NOT NULL DEFAULT '',
    role_id       INTEGER NOT NULL REFERENCES roles(id),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);

-- Справочник «Объекты»: места, где установлены датчики
CREATE TABLE IF NOT EXISTS objects (
    object_id       INTEGER PRIMARY KEY,           -- ид_объект
    hierarchy_level INTEGER NOT NULL,              -- иерархия_уровень
    parent_id       INTEGER,                       -- родитель
    object_kind     TEXT    NOT NULL,              -- вид_объекта: controlHouse / guardObject / district
    dispatch_name   TEXT    NOT NULL,              -- диспетчерское_название_объекта
    longitude       REAL,                          -- долгота
    latitude        REAL                           -- широта
);

-- Справочник «Журнал датчиков»: какой датчик на каком объекте установлен
CREATE TABLE IF NOT EXISTS sensor_channels (
    channel_id  INTEGER PRIMARY KEY,               -- ид_канала_данных
    system_type TEXT    NOT NULL,                  -- тип_инж_системы
    sensor_type TEXT    NOT NULL,                  -- тип_датчика
    system_tag  TEXT    NOT NULL DEFAULT '',       -- тег_инженерной_системы
    sensor_name TEXT    NOT NULL DEFAULT '',       -- название_датчика
    object_id   INTEGER REFERENCES objects(object_id)
);
CREATE INDEX IF NOT EXISTS idx_channels_object ON sensor_channels(object_id);

-- Загруженные файлы моделей прогнозирования (.joblib)
CREATE TABLE IF NOT EXISTS ml_models (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stored_name   TEXT NOT NULL,                   -- имя файла на диске
    original_name TEXT NOT NULL,                   -- имя файла, как его загрузили
    uploaded_at   TEXT NOT NULL,
    uploaded_by   TEXT NOT NULL DEFAULT ''
);

-- Горячие работы (АРМ-Контроль): сварка, резка и т.п. на объекте
CREATE TABLE IF NOT EXISTS hot_works (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id  INTEGER NOT NULL,                   -- ид_объект
    start_time TEXT    NOT NULL,                   -- начало
    end_time   TEXT    NOT NULL,                   -- окончание
    work_type  TEXT    NOT NULL DEFAULT ''         -- тип_работ
);
CREATE INDEX IF NOT EXISTS idx_hot_works_object ON hot_works(object_id);

-- Погода по часам. Показатели хранятся как JSON {"температура": 18.5, "влажность": 72},
-- потому что набор колонок берётся из загруженного файла
CREATE TABLE IF NOT EXISTS weather_hourly (
    hour_start  TEXT PRIMARY KEY,                  -- начало часа
    values_json TEXT NOT NULL
);

-- Реестр подтверждённых пожарных инцидентов
CREATE TABLE IF NOT EXISTS fire_incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id     INTEGER NOT NULL,                -- ид_объект
    incident_time TEXT    NOT NULL                 -- datetime
);

-- Простые настройки вида «ключ = значение»
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Допустимые значения для выпадающих списков в справочниках
OBJECT_KINDS = {
    "controlHouse": "Дом под охраной",
    "guardObject": "Объект под охраной",
    "district": "Район / округ",
}

ENGINEERING_SYSTEM_TYPES = [
    "Охранная подсистема",
    "Пожарная охрана",
    "Температурная подсистема",
    "Диагностическая подсистема",
    "Диспетчерский контроль",
    "Газовая охрана",
]

SENSOR_TYPES = [
    "КД АВ", "Тепловой датчик", "Датчик температуры", "Датчик движения",
    "КД Дверь", "Состояние охраны", "Состояние УИР-Р", "Датчик дыма",
    "КД Люк", "Датчик затопления", "Ручной извещатель", "Стекло",
    "Состояние фазы", "Состояние вентилятора", "Переключатель",
    "Газовый датчик", "Состояние насоса", "ИБП", "9-секционный люк",
]


# ---------------------------------------------------------------------------
# Соединение с базой
# ---------------------------------------------------------------------------

def open_connection():
    """Открывает новое соединение с базой. Строки будут доступны по имени колонки."""
    connection = sqlite3.connect(config.DATABASE_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_db():
    """
    Возвращает соединение для текущего веб-запроса.

    Flask хранит его в объекте g, поэтому в пределах одного запроса
    соединение открывается только один раз.
    """
    if "db_connection" not in g:
        g.db_connection = open_connection()
    return g.db_connection


def close_db(_error=None):
    """Закрывает соединение в конце веб-запроса."""
    connection = g.pop("db_connection", None)
    if connection is not None:
        connection.close()


# ---------------------------------------------------------------------------
# Настройки «ключ = значение»
# ---------------------------------------------------------------------------

def read_setting(connection, key, default_value=None):
    row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default_value


def write_setting(connection, key, value):
    connection.execute(
        "INSERT INTO app_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    connection.commit()


# ---------------------------------------------------------------------------
# Первоначальное заполнение
# ---------------------------------------------------------------------------

def initialize_database():
    """Создаёт таблицы и, если база пустая, заполняет её начальными данными."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOADED_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOADED_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    connection = open_connection()
    connection.executescript(SCHEMA_SQL)

    roles_count = connection.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    if roles_count == 0:
        _seed_roles_and_users(connection)

    objects_count = connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    if objects_count == 0 and config.SEED_OBJECTS_CSV.exists():
        _seed_objects(connection)

    channels_count = connection.execute("SELECT COUNT(*) FROM sensor_channels").fetchone()[0]
    if channels_count == 0 and config.SEED_CHANNELS_CSV.exists():
        _seed_sensor_channels(connection)

    connection.commit()
    connection.close()


def _seed_roles_and_users(connection):
    """Создаёт две обязательные роли и учётные записи по умолчанию."""
    connection.execute(
        "INSERT INTO roles(code, name, description, can_manage_settings) VALUES(?, ?, ?, ?)",
        ("admin", "Администратор", "Полный доступ, включая раздел «Настройки»", 1),
    )
    connection.execute(
        "INSERT INTO roles(code, name, description, can_manage_settings) VALUES(?, ?, ?, ?)",
        ("dispatcher", "Диспетчер", "Рабочее место диспетчера: карта, датчики, прогноз", 0),
    )

    created_at = datetime.now().isoformat(timespec="seconds")
    for user in config.DEFAULT_USERS:
        role_id = connection.execute(
            "SELECT id FROM roles WHERE code = ?", (user["role_code"],)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO users(login, password_hash, full_name, role_id, is_active, created_at) "
            "VALUES(?, ?, ?, ?, 1, ?)",
            (user["login"], generate_password_hash(user["password"]), user["full_name"], role_id, created_at),
        )


def _seed_objects(connection):
    """
    Загружает справочник объектов из CSV.

    В исходном файле нет координат, поэтому мы расставляем объекты на карте
    Москвы автоматически (см. calculate_demo_coordinates). Настоящие координаты
    администратор может потом ввести в справочнике «Объекты».
    """
    with open(config.SEED_OBJECTS_CSV, encoding="utf-8-sig", newline="") as csv_file:
        object_rows = list(csv.DictReader(csv_file))

    coordinates_by_id = calculate_demo_coordinates(object_rows)

    for row in object_rows:
        object_id = int(row["ид_объект"])
        latitude, longitude = coordinates_by_id[object_id]
        connection.execute(
            "INSERT INTO objects(object_id, hierarchy_level, parent_id, object_kind, "
            "dispatch_name, longitude, latitude) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                object_id,
                int(row["иерархия_уровень"]),
                int(row["родитель"]) if row["родитель"] else None,
                row["вид_объекта"],
                row["диспетчерское_название_объекта"],
                longitude,
                latitude,
            ),
        )


def calculate_demo_coordinates(object_rows):
    """
    Придумывает координаты для объектов, чтобы их можно было показать на карте.

    Правила:
      * уровень 1 (район) ставим в центр Москвы;
      * уровень 2 (коллекторы) раскладываем по спирали вокруг центра,
        чтобы они не наезжали друг на друга;
      * уровень 3 (части коллектора) ставим по небольшому кругу
        вокруг своего родителя.

    Возвращает словарь {ид_объекта: (широта, долгота)}.
    """
    center_latitude, center_longitude = config.MOSCOW_CENTER

    # 1 градус широты ≈ 111 км; 1 градус долготы на широте Москвы ≈ 63 км
    km_per_degree_latitude = 111.0
    km_per_degree_longitude = 111.0 * math.cos(math.radians(center_latitude))

    def shift_point(latitude, longitude, distance_km, angle_radians):
        """Сдвигает точку на заданное расстояние в заданном направлении."""
        new_latitude = latitude + (distance_km * math.sin(angle_radians)) / km_per_degree_latitude
        new_longitude = longitude + (distance_km * math.cos(angle_radians)) / km_per_degree_longitude
        return round(new_latitude, 6), round(new_longitude, 6)

    coordinates = {}
    golden_angle = math.pi * (3 - math.sqrt(5))  # «золотой угол» даёт ровную спираль

    # Уровни 1 и 2
    level_two_ids = []
    for row in object_rows:
        object_id = int(row["ид_объект"])
        level = int(row["иерархия_уровень"])
        if level == 1:
            coordinates[object_id] = (center_latitude, center_longitude)
        elif level == 2:
            level_two_ids.append(object_id)

    for position, object_id in enumerate(sorted(level_two_ids)):
        distance_km = 3.0 + 1.6 * math.sqrt(position + 1) * 2.2
        coordinates[object_id] = shift_point(
            center_latitude, center_longitude, distance_km, position * golden_angle
        )

    # Уровень 3 — вокруг родителя
    children_by_parent = {}
    for row in object_rows:
        if int(row["иерархия_уровень"]) >= 3:
            parent_id = int(row["родитель"])
            children_by_parent.setdefault(parent_id, []).append(int(row["ид_объект"]))

    for parent_id, child_ids in children_by_parent.items():
        parent_latitude, parent_longitude = coordinates.get(parent_id, config.MOSCOW_CENTER)
        for index, child_id in enumerate(sorted(child_ids)):
            angle = 2 * math.pi * index / len(child_ids)
            coordinates[child_id] = shift_point(parent_latitude, parent_longitude, 0.7, angle)

    # На всякий случай: всё, что не попало в правила, — в центр
    for row in object_rows:
        coordinates.setdefault(int(row["ид_объект"]), (center_latitude, center_longitude))

    return coordinates


def _seed_sensor_channels(connection):
    """Загружает справочник каналов датчиков и привязывает их к объектам."""
    with open(config.SEED_CHANNELS_CSV, encoding="utf-8-sig", newline="") as csv_file:
        channel_rows = list(csv.DictReader(csv_file))

    object_rows = connection.execute(
        "SELECT object_id, hierarchy_level, parent_id, object_kind, dispatch_name FROM objects"
    ).fetchall()

    # Если в файле есть колонка ид_объект (как в справочнике из ноутбука) — берём привязку из неё.
    # Иначе строим привязку по правилу.
    object_column = next(
        (name for name in ("ид_объект", "ид_объекта") if channel_rows and name in channel_rows[0]), None
    )
    if object_column:
        object_id_by_channel = {
            int(row["ид_канала_данных"]): int(row[object_column])
            for row in channel_rows
            if (row[object_column] or "").strip().isdigit()
        }
    else:
        object_id_by_channel = assign_channels_to_objects(channel_rows, object_rows)

    for row in channel_rows:
        channel_id = int(row["ид_канала_данных"])
        connection.execute(
            "INSERT INTO sensor_channels(channel_id, system_type, sensor_type, system_tag, "
            "sensor_name, object_id) VALUES(?, ?, ?, ?, ?, ?)",
            (
                channel_id,
                row["тип_инж_системы"],
                row["тип_датчика"],
                row.get("тег_инженерной_системы", "") or "",
                row.get("название_датчика", "") or "",
                object_id_by_channel.get(channel_id),
            ),
        )


def assign_channels_to_objects(channel_rows, object_rows):
    """
    Привязывает каждый канал датчика к объекту уровня 3.

    ВАЖНО: в исходном CSV нет колонки «ид объекта», поэтому привязка
    строится по правилу, а не берётся из данных:

      1. Первая часть тега (например, «847» в «847-1.1.21.4.») — это номер
         контроллера. Все каналы одного контроллера отправляем в один
         коллектор (объект уровня 2). Контроллеры распределяем так, чтобы
         число датчиков в коллекторах было примерно одинаковым.
      2. Внутри коллектора канал попадает в дочерний объект по смыслу
         подсистемы: пожарные датчики — в объект «ПС», охранные — в «ОС»,
         диспетчерский контроль — в «ДУ»/«ДП», диагностика — в «шкафы».

    Если у вас есть точная привязка, загрузите её в справочнике
    «Журнал датчиков» (кнопка «Импорт CSV» с колонкой ид_объекта).

    Возвращает словарь {ид_канала: ид_объекта}.
    """
    # --- Шаг 1. Группируем каналы по номеру контроллера -------------------
    channel_ids_by_controller = {}
    for row in channel_rows:
        tag = (row.get("тег_инженерной_системы") or "").strip()
        controller_number = tag.split("-")[0] if "-" in tag else "unknown"
        channel_ids_by_controller.setdefault(controller_number, []).append(row)

    level_two_ids = sorted(row["object_id"] for row in object_rows if row["hierarchy_level"] == 2)
    children_by_parent = {}
    for row in object_rows:
        if row["hierarchy_level"] >= 3:
            children_by_parent.setdefault(row["parent_id"], []).append(row)

    if not level_two_ids:
        return {}

    # --- Шаг 2. Раздаём контроллеры коллекторам «по очереди самому лёгкому» ---
    channels_count_by_collector = {object_id: 0 for object_id in level_two_ids}
    collector_by_controller = {}
    controllers_biggest_first = sorted(
        channel_ids_by_controller.items(), key=lambda item: (-len(item[1]), item[0])
    )
    for controller_number, rows in controllers_biggest_first:
        least_loaded_collector = min(
            level_two_ids, key=lambda object_id: (channels_count_by_collector[object_id], object_id)
        )
        collector_by_controller[controller_number] = least_loaded_collector
        channels_count_by_collector[least_loaded_collector] += len(rows)

    # --- Шаг 3. Внутри коллектора выбираем дочерний объект по подсистеме ---
    object_id_by_channel = {}
    for controller_number, rows in channel_ids_by_controller.items():
        collector_id = collector_by_controller[controller_number]
        children = children_by_parent.get(collector_id, [])
        for row in rows:
            child_id = _pick_child_for_system(children, row["тип_инж_системы"])
            object_id_by_channel[int(row["ид_канала_данных"])] = child_id or collector_id

    return object_id_by_channel


# Какие слова в названии дочернего объекта подходят для каждой подсистемы.
# Порядок важен: сначала самые подходящие варианты.
_NAME_HINTS_BY_SYSTEM = {
    "Пожарная охрана": ["ПС"],
    "Температурная подсистема": ["ПС"],
    "Охранная подсистема": ["ОС"],
    "Газовая охрана": ["ДУ", "ДП"],
    "Диспетчерский контроль": ["ДУ", "ДП"],
    "Диагностическая подсистема": ["шкаф", "Шкаф"],
}


def _pick_child_for_system(children, system_type):
    """Выбирает дочерний объект, название которого лучше всего подходит подсистеме."""
    if not children:
        return None

    for hint in _NAME_HINTS_BY_SYSTEM.get(system_type, []):
        for child in children:
            name_words = child["dispatch_name"].replace(",", " ").split()
            if any(word.startswith(hint) for word in name_words):
                return child["object_id"]

    # Ничего не подошло: охрана и пожарка — в «объект под охраной», остальное — в первый объект
    if system_type in ("Пожарная охрана", "Температурная подсистема", "Охранная подсистема"):
        for child in children:
            if child["object_kind"] == "guardObject":
                return child["object_id"]
    return sorted(children, key=lambda child: child["object_id"])[0]["object_id"]
