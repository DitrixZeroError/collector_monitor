"""
Дополнительные данные, которые повышают качество прогноза (раздел 3.6 ноутбука):

  * горячие работы (АРМ-Контроль)   — ид_объект, начало, окончание, тип_работ;
  * погода по часам                 — дата и время, температура, влажность;
  * реестр пожарных инцидентов      — ид_объект, datetime.

Модуль умеет:
  1. прочитать загруженный CSV и проверить колонки (parse_*);
  2. сохранить данные в базу (save_*);
  3. отдать их построителю признаков в виде таблиц pandas (load_external_data);
  4. посчитать краткую сводку для страницы настроек (describe_external_data).
"""

import io
import json
import re

import pandas as pd

# Как данные называются для людей
DATASET_TITLES = {
    "hot_works": "Горячие работы (АРМ-Контроль)",
    "weather": "Погода по часам",
    "incidents": "Реестр пожарных инцидентов",
}

# Допустимые названия колонок. Сравнение без учёта регистра, пробелов,
# подчёркиваний и квадратных скобок: «[Дата и время]» = «дата_и_время».
COLUMN_ALIASES = {
    "object_id": ["ид_объект", "ид_объекта", "object_id"],
    "start": ["начало", "start"],
    "end": ["окончание", "конец", "end"],
    "work_type": ["тип_работ", "work_type"],
    "datetime": ["datetime", "дата_и_время", "дата_время", "дата"],
}

# Образцы файлов, которые можно скачать со страницы настроек
TEMPLATE_CSV = {
    "hot_works": "ид_объект,начало,окончание,тип_работ\n5122,2026-08-01 09:00,2026-08-01 17:00,Сварочные работы\n",
    "weather": "дата и время,температура,влажность\n2026-08-01 00:00,18.5,72\n2026-08-01 01:00,17.9,75\n",
    "incidents": "ид_объект,datetime\n5122,2026-05-14 03:20\n",
}


class ExternalDataError(Exception):
    """Файл не подходит: нет нужных колонок, не разбираются даты и т.п."""


# ===========================================================================
# Чтение и проверка CSV
# ===========================================================================

def _normalize_header(name):
    """«[Дата и время] » -> «дата_и_время»."""
    text = str(name).strip().strip("[]").strip().lower()
    return re.sub(r"[\s_]+", "_", text)


def read_uploaded_csv(file_bytes):
    """
    Читает CSV из загруженного файла.
    Кодировка — UTF-8 или Windows-1251, разделитель — запятая или точка с запятой.
    """
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ExternalDataError("Не удалось определить кодировку файла. Сохраните его в UTF-8.")

    first_line = text.split("\n", 1)[0]
    separator = ";" if first_line.count(";") > first_line.count(",") else ","
    table = pd.read_csv(io.StringIO(text), sep=separator, dtype=str, skipinitialspace=True)

    # Убираем квадратные скобки и пробелы вокруг названий колонок
    table.columns = [str(column).strip().strip("[]").strip() for column in table.columns]
    if table.empty:
        raise ExternalDataError("В файле нет ни одной строки с данными.")
    return table


def _find_column(table, logical_name, required=True):
    """Находит колонку по списку допустимых названий. Возвращает её настоящее имя."""
    wanted = {_normalize_header(alias) for alias in COLUMN_ALIASES[logical_name]}
    for column in table.columns:
        if _normalize_header(column) in wanted:
            return column
    if required:
        raise ExternalDataError(
            "Нет колонки «" + COLUMN_ALIASES[logical_name][0] + "». Подходящие названия: "
            + ", ".join(COLUMN_ALIASES[logical_name]) + "."
        )
    return None


def parse_datetime_column(values):
    """
    Разбирает даты. Сначала пробуем формат ГГГГ-ММ-ДД ЧЧ:ММ, потом ДД.ММ.ГГГГ ЧЧ:ММ
    (так же, как _parse_datetime в ноутбуке).
    """
    text = values.astype(str).str.strip()
    parsed = pd.to_datetime(text, format="ISO8601", errors="coerce")
    if parsed.isna().mean() > 0.5:
        parsed = pd.to_datetime(text, format="mixed", dayfirst=True, errors="coerce")
    return parsed


def parse_hot_works(table):
    """Горячие работы: ид_объект, начало, окончание[, тип_работ]."""
    object_column = _find_column(table, "object_id")
    start_column = _find_column(table, "start")
    end_column = _find_column(table, "end")
    type_column = _find_column(table, "work_type", required=False)

    works = pd.DataFrame({
        "object_id": pd.to_numeric(table[object_column], errors="coerce"),
        "start": parse_datetime_column(table[start_column]),
        "end": parse_datetime_column(table[end_column]),
        "work_type": table[type_column].fillna("").str.strip() if type_column else "",
    })

    is_valid = works["object_id"].notna() & works["start"].notna() & works["end"].notna()
    is_valid &= works["end"] >= works["start"]
    skipped_count = int((~is_valid).sum())
    works = works[is_valid].copy()
    works["object_id"] = works["object_id"].astype(int)
    return works, skipped_count


def parse_weather(table):
    """
    Погода: колонка даты и времени плюс любые числовые колонки.

    Названия числовых колонок сохраняются как в файле: именно под этими
    именами (например, «температура», «влажность») они попадают в модель.
    Несколько записей за один час усредняются.
    """
    datetime_column = _find_column(table, "datetime")
    value_columns = [column for column in table.columns if column != datetime_column]
    if not value_columns:
        raise ExternalDataError("Кроме даты нужна хотя бы одна колонка с показателем (температура, влажность).")

    weather = pd.DataFrame({"hour_start": parse_datetime_column(table[datetime_column]).dt.floor("h")})
    for column in value_columns:
        numbers = pd.to_numeric(table[column].str.replace(",", ".", regex=False), errors="coerce")
        if numbers.notna().sum() == 0:
            raise ExternalDataError(f"В колонке «{column}» нет чисел. В файле погоды должны быть только числа.")
        weather[column] = numbers

    is_valid = weather["hour_start"].notna()
    skipped_count = int((~is_valid).sum())
    weather = weather[is_valid].groupby("hour_start", as_index=False)[value_columns].mean()
    return weather, skipped_count


def parse_incidents(table):
    """Реестр пожарных инцидентов: ид_объект, datetime."""
    object_column = _find_column(table, "object_id")
    datetime_column = _find_column(table, "datetime")

    incidents = pd.DataFrame({
        "object_id": pd.to_numeric(table[object_column], errors="coerce"),
        "incident_time": parse_datetime_column(table[datetime_column]),
    })
    is_valid = incidents["object_id"].notna() & incidents["incident_time"].notna()
    skipped_count = int((~is_valid).sum())
    incidents = incidents[is_valid].copy()
    incidents["object_id"] = incidents["object_id"].astype(int)
    return incidents, skipped_count


PARSERS = {"hot_works": parse_hot_works, "weather": parse_weather, "incidents": parse_incidents}


# ===========================================================================
# Сохранение в базу
# ===========================================================================

def _format_time(moment):
    return pd.Timestamp(moment).strftime("%Y-%m-%d %H:%M:%S")


def save_dataset(connection, dataset_name, data, replace_existing):
    """
    Сохраняет разобранные данные в базу.
    replace_existing=True — сначала удалить всё, что было загружено раньше.
    Возвращает число сохранённых строк.
    """
    if replace_existing:
        clear_dataset(connection, dataset_name, commit=False)

    if dataset_name == "hot_works":
        connection.executemany(
            "INSERT INTO hot_works(object_id, start_time, end_time, work_type) VALUES(?, ?, ?, ?)",
            [(int(row.object_id), _format_time(row.start), _format_time(row.end), row.work_type)
             for row in data.itertuples()],
        )
    elif dataset_name == "weather":
        value_columns = [column for column in data.columns if column != "hour_start"]
        rows = []
        for record in data.to_dict(orient="records"):
            values = {column: record[column] for column in value_columns if pd.notna(record[column])}
            rows.append((_format_time(record["hour_start"]), json.dumps(values, ensure_ascii=False)))
        # Если час уже есть — заменяем его значения
        connection.executemany(
            "INSERT INTO weather_hourly(hour_start, values_json) VALUES(?, ?) "
            "ON CONFLICT(hour_start) DO UPDATE SET values_json = excluded.values_json",
            rows,
        )
    elif dataset_name == "incidents":
        connection.executemany(
            "INSERT INTO fire_incidents(object_id, incident_time) VALUES(?, ?)",
            [(int(row.object_id), _format_time(row.incident_time)) for row in data.itertuples()],
        )
    else:
        raise ValueError(f"Неизвестный набор данных: {dataset_name}")

    connection.commit()
    return len(data)


_TABLE_BY_DATASET = {"hot_works": "hot_works", "weather": "weather_hourly", "incidents": "fire_incidents"}


def clear_dataset(connection, dataset_name, commit=True):
    connection.execute(f"DELETE FROM {_TABLE_BY_DATASET[dataset_name]}")
    if commit:
        connection.commit()


# ===========================================================================
# Чтение из базы
# ===========================================================================

def load_external_data(connection):
    """
    Возвращает (горячие_работы, погода, инциденты) — таблицы pandas или None,
    если соответствующие данные не загружены. Формат — как ждёт HourlyFeatureBuilder.
    """
    hot_works = pd.read_sql_query(
        "SELECT object_id, start_time AS start, end_time AS end FROM hot_works", connection
    )
    if hot_works.empty:
        hot_works = None
    else:
        hot_works["start"] = pd.to_datetime(hot_works["start"])
        hot_works["end"] = pd.to_datetime(hot_works["end"])

    weather_rows = connection.execute("SELECT hour_start, values_json FROM weather_hourly").fetchall()
    if weather_rows:
        weather = pd.DataFrame([
            {"hour_start": pd.Timestamp(row["hour_start"]), **json.loads(row["values_json"])}
            for row in weather_rows
        ])
    else:
        weather = None

    incidents = pd.read_sql_query("SELECT object_id, incident_time FROM fire_incidents", connection)
    if incidents.empty:
        incidents = None
    else:
        incidents["incident_time"] = pd.to_datetime(incidents["incident_time"])

    return hot_works, weather, incidents


def describe_external_data(connection):
    """Сводка для страницы настроек: сколько строк, за какой период, первые строки."""
    summary = {}

    row = connection.execute(
        "SELECT COUNT(*) AS rows_count, MIN(start_time) AS first, MAX(end_time) AS last, "
        "COUNT(DISTINCT object_id) AS objects_count FROM hot_works"
    ).fetchone()
    summary["hot_works"] = {
        **dict(row),
        "preview_columns": ["Объект", "Начало", "Окончание", "Тип работ"],
        "preview": [tuple(item) for item in connection.execute(
            "SELECT hot_works.object_id || ' ' || COALESCE(objects.dispatch_name, '(нет в справочнике)'), "
            "start_time, end_time, work_type FROM hot_works "
            "LEFT JOIN objects ON objects.object_id = hot_works.object_id "
            "ORDER BY start_time DESC LIMIT 5"
        )],
    }

    row = connection.execute(
        "SELECT COUNT(*) AS rows_count, MIN(hour_start) AS first, MAX(hour_start) AS last FROM weather_hourly"
    ).fetchone()
    weather_preview = connection.execute(
        "SELECT hour_start, values_json FROM weather_hourly ORDER BY hour_start DESC LIMIT 5"
    ).fetchall()
    weather_columns = []
    for item in weather_preview:
        for column in json.loads(item["values_json"]):
            if column not in weather_columns:
                weather_columns.append(column)
    summary["weather"] = {
        **dict(row),
        "objects_count": None,
        "preview_columns": ["Час"] + weather_columns,
        "preview": [
            tuple([item["hour_start"]] + [json.loads(item["values_json"]).get(column, "") for column in weather_columns])
            for item in weather_preview
        ],
    }

    row = connection.execute(
        "SELECT COUNT(*) AS rows_count, MIN(incident_time) AS first, MAX(incident_time) AS last, "
        "COUNT(DISTINCT object_id) AS objects_count FROM fire_incidents"
    ).fetchone()
    summary["incidents"] = {
        **dict(row),
        "preview_columns": ["Объект", "Дата и время"],
        "preview": [tuple(item) for item in connection.execute(
            "SELECT fire_incidents.object_id || ' ' || COALESCE(objects.dispatch_name, '(нет в справочнике)'), "
            "incident_time FROM fire_incidents "
            "LEFT JOIN objects ON objects.object_id = fire_incidents.object_id "
            "ORDER BY incident_time DESC LIMIT 5"
        )],
    }
    return summary


def count_unknown_objects(connection, data):
    """Сколько строк ссылаются на объекты, которых нет в справочнике «Объекты»."""
    if "object_id" not in data:
        return 0
    known_ids = {row[0] for row in connection.execute("SELECT object_id FROM objects")}
    return int((~data["object_id"].isin(known_ids)).sum())
