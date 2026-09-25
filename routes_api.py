"""
Рабочее место диспетчера (главная страница) и JSON-API для него.

Страница диспетчера сама по себе почти пустая: карту, список датчиков
и прогноз заполняет JavaScript (static/js/dispatcher.js), который
раз в несколько секунд запрашивает свежие данные по адресам /api/....
"""

from collections import Counter
from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, render_template, request

import config
from auth import login_required
from database import OBJECT_KINDS, get_db, read_setting
from services.emulator import emulation_runner
from services.event_classifier import describe_value_status
from services.model_service import ModelLoadError
from services.prediction_settings import load_model_if_prediction_enabled

pages_blueprint = Blueprint("pages", __name__)
api_blueprint = Blueprint("api", __name__, url_prefix="/api")


# ===========================================================================
# Страница
# ===========================================================================

@pages_blueprint.route("/")
@login_required
def dispatcher():
    return render_template(
        "dispatcher.html",
        map_center=config.MOSCOW_CENTER,
        map_zoom=config.MAP_START_ZOOM,
    )


# ===========================================================================
# Вспомогательные функции
# ===========================================================================

def load_channels_by_id(db):
    """Словарь {ид_канала: строка справочника} — нужен, чтобы подписывать показания."""
    rows = db.execute(
        "SELECT channel_id, sensor_type, sensor_name, system_type, object_id FROM sensor_channels"
    ).fetchall()
    return {row["channel_id"]: row for row in rows}


def load_objects(db):
    return db.execute("SELECT * FROM objects ORDER BY hierarchy_level, object_id").fetchall()


def collect_descendants(object_rows):
    """
    Для каждого объекта находит его самого и всех потомков (детей, внуков...).
    Нужно, чтобы тревога или прогноз по части коллектора «поднимались» к коллектору.
    """
    children_by_parent = {}
    for row in object_rows:
        children_by_parent.setdefault(row["parent_id"], []).append(row["object_id"])

    def descendants_of(object_id):
        found = [object_id]
        for child_id in children_by_parent.get(object_id, []):
            if child_id != object_id:  # защита от зацикливания в данных
                found.extend(descendants_of(child_id))
        return found

    return {row["object_id"]: descendants_of(row["object_id"]) for row in object_rows}


def count_live_statuses_by_object(channels_by_id):
    """Считает для каждого объекта, сколько его датчиков сейчас в тревоге / требуют внимания."""
    alarms_by_object = Counter()
    warnings_by_object = Counter()
    for channel_id, reading in dict(emulation_runner.live_readings).items():
        channel = channels_by_id.get(channel_id)
        if channel is None or channel["object_id"] is None:
            continue
        status = describe_value_status(channel["sensor_type"], reading["value"], reading["is_alarm"])
        if status == "alarm":
            alarms_by_object[channel["object_id"]] += 1
        elif status == "warning":
            warnings_by_object[channel["object_id"]] += 1
    return alarms_by_object, warnings_by_object


def prediction_is_enabled(db):
    return read_setting(db, "prediction_enabled", "0") == "1"


# ===========================================================================
# API
# ===========================================================================

@api_blueprint.route("/objects")
@login_required
def api_objects():
    """Объекты для карты с их текущим состоянием."""
    db = get_db()
    object_rows = load_objects(db)
    channels_by_id = load_channels_by_id(db)
    descendants = collect_descendants(object_rows)

    channels_count_by_object = Counter(
        channel["object_id"] for channel in channels_by_id.values() if channel["object_id"] is not None
    )
    alarms_by_object, warnings_by_object = count_live_statuses_by_object(channels_by_id)
    predictions = emulation_runner.predictions if prediction_is_enabled(db) else {}

    result = []
    for row in object_rows:
        family = descendants[row["object_id"]]

        # Для коллектора берём самый высокий риск среди его частей
        family_predictions = [predictions[object_id] for object_id in family if object_id in predictions]
        highest_prediction = max(family_predictions, key=lambda item: item["probability"], default=None)

        result.append({
            "id": row["object_id"],
            "name": row["dispatch_name"],
            "kind": row["object_kind"],
            "kind_label": OBJECT_KINDS.get(row["object_kind"], row["object_kind"]),
            "level": row["hierarchy_level"],
            "parent_id": row["parent_id"],
            "lat": row["latitude"],
            "lon": row["longitude"],
            "channels_count": sum(channels_count_by_object[object_id] for object_id in family),
            "alarms": sum(alarms_by_object[object_id] for object_id in family),
            "warnings": sum(warnings_by_object[object_id] for object_id in family),
            "probability": highest_prediction["probability"] if highest_prediction else None,
            "is_high_risk": bool(highest_prediction and highest_prediction["is_high_risk"]),
        })
    return jsonify(result)


@api_blueprint.route("/objects/<int:object_id>/sensors")
@login_required
def api_object_sensors(object_id):
    """
    Датчики объекта (и его частей) с последними показаниями.

    Параметр ?only_active=1 оставляет только датчики, от которых уже были
    показания. На крупных объектах тысячи датчиков, и без этого фильтра
    каждое обновление передавало бы по сети сотни килобайт.
    """
    only_active = request.args.get("only_active") == "1"
    db = get_db()
    object_rows = load_objects(db)
    object_by_id = {row["object_id"]: row for row in object_rows}
    if object_id not in object_by_id:
        return jsonify({"error": "Объект не найден"}), 404

    family = collect_descendants(object_rows)[object_id]
    placeholders = ",".join("?" for _ in family)
    channel_rows = db.execute(
        f"SELECT * FROM sensor_channels WHERE object_id IN ({placeholders}) "
        f"ORDER BY system_type, sensor_type, sensor_name",
        family,
    ).fetchall()

    live_readings = dict(emulation_runner.live_readings)
    sensors = []
    readings_count = 0
    for channel in channel_rows:
        reading = live_readings.get(channel["channel_id"])
        if reading:
            readings_count += 1
        elif only_active:
            continue
        sensors.append({
            "channel_id": channel["channel_id"],
            "name": channel["sensor_name"],
            "sensor_type": channel["sensor_type"],
            "system_type": channel["system_type"],
            "object_name": object_by_id[channel["object_id"]]["dispatch_name"],
            "value": reading["value"] if reading else None,
            "time": reading["time"] if reading else None,
            "status": (
                describe_value_status(channel["sensor_type"], reading["value"], reading["is_alarm"])
                if reading else "silent"
            ),
        })

    # Датчики с показаниями и с тревогами — наверх списка
    status_order = {"alarm": 0, "warning": 1, "presence": 2, "ok": 3, "nodata": 4, "silent": 5}
    sensors.sort(key=lambda sensor: status_order[sensor["status"]])

    predictions = emulation_runner.predictions if prediction_is_enabled(db) else {}
    object_row = object_by_id[object_id]
    hot_works = find_current_hot_works(db, family, object_by_id)
    return jsonify({
        "object": {
            "id": object_id,
            "name": object_row["dispatch_name"],
            "kind_label": OBJECT_KINDS.get(object_row["object_kind"], object_row["object_kind"]),
            "level": object_row["hierarchy_level"],
        },
        "sensors": sensors,
        "total_sensors": len(channel_rows),
        "sensors_with_readings": readings_count,
        "prediction": predictions.get(object_id),
        "hot_works": hot_works,
    })


def find_current_hot_works(db, object_ids, object_by_id):
    """
    Горячие работы на объекте (и его частях): идущие сейчас и начинающиеся
    в ближайшие сутки. «Сейчас» — модельное время эмуляции, если она запускалась.
    """
    reference_time = emulation_runner.simulation_time or datetime.now()
    now_text = reference_time.strftime("%Y-%m-%d %H:%M:%S")
    tomorrow_text = (reference_time + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    placeholders = ",".join("?" for _ in object_ids)
    rows = db.execute(
        f"SELECT object_id, start_time, end_time, work_type FROM hot_works "
        f"WHERE object_id IN ({placeholders}) AND end_time >= ? AND start_time <= ? "
        f"ORDER BY start_time LIMIT 10",
        list(object_ids) + [now_text, tomorrow_text],
    ).fetchall()

    return [
        {
            "object_name": object_by_id[row["object_id"]]["dispatch_name"],
            "start": row["start_time"][:16],
            "end": row["end_time"][:16],
            "work_type": row["work_type"] or "Горячие работы",
            "is_active": row["start_time"] <= now_text,
        }
        for row in rows
    ]


@api_blueprint.route("/forecast")
@login_required
def api_forecast():
    """Прогноз пожарного риска по всем объектам (для блока «Прогноз»)."""
    db = get_db()
    if not prediction_is_enabled(db):
        return jsonify({"enabled": False})

    object_names = {row["object_id"]: row["dispatch_name"] for row in load_objects(db)}
    rows = [
        {
            "id": object_id,
            "name": object_names.get(object_id, f"Объект {object_id}"),
            "probability": prediction["probability"],
            "is_high_risk": prediction["is_high_risk"],
            "factors": prediction["factors"],
        }
        for object_id, prediction in dict(emulation_runner.predictions).items()
    ]
    rows.sort(key=lambda row: row["probability"], reverse=True)
    
    # Порог берём из модели, чтобы нарисовать его на шкале
    try:
        model = load_model_if_prediction_enabled()
        threshold = model.threshold if model else None
    except ModelLoadError:
        threshold = None

    return jsonify({
        "enabled": True,
        "threshold": threshold,
        "hour": emulation_runner.prediction_hour,
        "message": emulation_runner.prediction_message,
        "objects": rows,
    })


@api_blueprint.route("/status")
@login_required
def api_status():
    """Состояние эмуляции и лента последних тревог (для шапки страницы)."""
    db = get_db()
    channels_by_id = load_channels_by_id(db)
    object_names = {row["object_id"]: row["dispatch_name"] for row in load_objects(db)}

    alarms = []
    for alarm in emulation_runner.get_recent_alarms()[:10]:
        channel = channels_by_id.get(alarm["channel_id"])
        alarms.append({
            "time": alarm["time"],
            "value": alarm["value"],
            "sensor": channel["sensor_name"] if channel else f"Канал {alarm['channel_id']}",
            "object_id": channel["object_id"] if channel else None,
            "object": object_names.get(channel["object_id"], "—") if channel else "—",
        })

    return jsonify({
        "emulation": emulation_runner.snapshot(),
        "prediction_enabled": prediction_is_enabled(db),
        "alarms": alarms,
        "user": {"name": g.current_user["full_name"] or g.current_user["login"]},
    })
