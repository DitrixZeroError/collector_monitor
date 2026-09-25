"""
Раздел «Настройки» (доступен только администратору).

Содержит:
  * справочники «Объекты», «Журнал датчиков», «Пользователи», «Роли пользователей»;
  * «Модель прогнозирования» — загрузка файла .joblib и включение прогноза;
  * «Эмуляция» — проигрывание журнала событий датчиков.

Все формы добавления и редактирования рисуются одним шаблоном
settings/form.html. Какие поля в нём показать, описывается списком
словарей — «описаний полей» (см. функции *_form_fields).
"""

import csv
import io
import uuid
from datetime import datetime
from urllib.parse import quote

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

import config
from auth import settings_access_required
from database import (
    ENGINEERING_SYSTEM_TYPES,
    OBJECT_KINDS,
    SENSOR_TYPES,
    get_db,
    read_setting,
    write_setting,
)
from services.emulator import SPEED_OPTIONS, emulation_runner
from services.external_data import (
    DATASET_TITLES,
    PARSERS,
    TEMPLATE_CSV,
    ExternalDataError,
    clear_dataset,
    count_unknown_objects,
    describe_external_data,
    load_external_data,
    read_uploaded_csv,
    save_dataset,
)
from services.model_service import FireRiskModel, ModelLoadError, active_model_holder
from services.prediction_settings import load_model_if_prediction_enabled

settings_blueprint = Blueprint("settings", __name__, url_prefix="/settings")


# ===========================================================================
# Вспомогательные функции
# ===========================================================================

def field(name, label, kind="text", value="", options=None, required=True, hint=""):
    """Описание одного поля формы. kind: text, number, password, select, checkbox."""
    return {
        "name": name, "label": label, "kind": kind, "value": value,
        "options": options or [], "required": required, "hint": hint,
    }


def paginate(total_rows):
    """Считает номер страницы и смещение для SQL-запроса по параметру ?page=."""
    page_count = max(1, (total_rows + config.ROWS_PER_PAGE - 1) // config.ROWS_PER_PAGE)
    page_number = request.args.get("page", 1, type=int)
    page_number = min(max(page_number, 1), page_count)
    offset = (page_number - 1) * config.ROWS_PER_PAGE
    return page_number, page_count, offset


def form_number(name, allow_empty=False, is_float=False):
    """
    Читает число из формы.
    Возвращает (число, текст_ошибки). Текст ошибки пустой, если всё хорошо.
    """
    raw_text = request.form.get(name, "").strip().replace(",", ".")
    if raw_text == "":
        return (None, "") if allow_empty else (None, "обязательное поле")
    try:
        return (float(raw_text) if is_float else int(raw_text)), ""
    except ValueError:
        return None, "нужно число"


@settings_blueprint.route("/")
@settings_access_required
def index():
    return redirect(url_for("settings.objects_list"))


# ===========================================================================
# Справочник «Объекты»
# ===========================================================================

@settings_blueprint.route("/objects")
@settings_access_required
def objects_list():
    search_text = request.args.get("q", "").strip()
    where_sql, parameters = "", []
    if search_text:
        where_sql = "WHERE objects.dispatch_name LIKE ? OR CAST(objects.object_id AS TEXT) = ?"
        parameters = [f"%{search_text}%", search_text]

    db = get_db()
    total_rows = db.execute(f"SELECT COUNT(*) FROM objects {where_sql}", parameters).fetchone()[0]
    page_number, page_count, offset = paginate(total_rows)

    object_rows = db.execute(
        f"""
        SELECT objects.*,
               parent.dispatch_name AS parent_name,
               (SELECT COUNT(*) FROM sensor_channels WHERE sensor_channels.object_id = objects.object_id)
                   AS channels_count
        FROM objects
        LEFT JOIN objects AS parent ON parent.object_id = objects.parent_id
        {where_sql}
        ORDER BY objects.hierarchy_level, objects.parent_id, objects.object_id
        LIMIT ? OFFSET ?
        """,
        parameters + [config.ROWS_PER_PAGE, offset],
    ).fetchall()

    return render_template(
        "settings/objects.html",
        object_rows=object_rows, object_kinds=OBJECT_KINDS, search_text=search_text,
        total_rows=total_rows, page_number=page_number, page_count=page_count,
    )


def object_form_fields(object_row=None, is_new=True):
    values = dict(object_row) if object_row else {}
    kind_options = [(code, f"{code} — {label}") for code, label in OBJECT_KINDS.items()]
    fields = [
        field("object_id", "Ид объекта", "number", values.get("object_id", ""),
              hint="Уникальный номер. После создания изменить нельзя."),
        field("hierarchy_level", "Уровень иерархии", "number", values.get("hierarchy_level", 3),
              hint="1 — район, 2 — коллектор, 3 — часть коллектора"),
        field("parent_id", "Код родительского объекта", "number", values.get("parent_id") or "", required=False),
        field("object_kind", "Вид объекта", "select", values.get("object_kind", "controlHouse"), kind_options),
        field("dispatch_name", "Диспетчерское название", "text", values.get("dispatch_name", "")),
        field("latitude", "Широта", "text", values.get("latitude") or "", required=False,
              hint="Например, 55.7558. Нужна для отображения на карте."),
        field("longitude", "Долгота", "text", values.get("longitude") or "", required=False,
              hint="Например, 37.6173."),
    ]
    if not is_new:
        fields[0]["readonly"] = True
    return fields


def read_object_form():
    """Читает и проверяет форму объекта. Возвращает (данные, список ошибок)."""
    errors = []
    data = {}
    for name, allow_empty, is_float in [
        ("object_id", False, False), ("hierarchy_level", False, False), ("parent_id", True, False),
        ("latitude", True, True), ("longitude", True, True),
    ]:
        number, error_text = form_number(name, allow_empty, is_float)
        if error_text:
            errors.append(f"{name}: {error_text}")
        data[name] = number

    data["object_kind"] = request.form.get("object_kind", "")
    data["dispatch_name"] = request.form.get("dispatch_name", "").strip()
    if data["object_kind"] not in OBJECT_KINDS:
        errors.append("Выберите вид объекта из списка.")
    if not data["dispatch_name"]:
        errors.append("Укажите диспетчерское название.")
    return data, errors


@settings_blueprint.route("/objects/new", methods=["GET", "POST"])
@settings_access_required
def object_create():
    if request.method == "POST":
        data, errors = read_object_form()
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM objects WHERE object_id = ?", (data["object_id"],)).fetchone():
            errors.append(f"Объект с ид {data['object_id']} уже есть.")
        if not errors:
            db.execute(
                "INSERT INTO objects(object_id, hierarchy_level, parent_id, object_kind, dispatch_name, "
                "latitude, longitude) VALUES(:object_id, :hierarchy_level, :parent_id, :object_kind, "
                ":dispatch_name, :latitude, :longitude)",
                data,
            )
            db.commit()
            flash("Объект добавлен.", "success")
            return redirect(url_for("settings.objects_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="objects", title="Новый объект",
        fields=object_form_fields(), back_url=url_for("settings.objects_list"),
    )


@settings_blueprint.route("/objects/<int:object_id>/edit", methods=["GET", "POST"])
@settings_access_required
def object_edit(object_id):
    db = get_db()
    object_row = db.execute("SELECT * FROM objects WHERE object_id = ?", (object_id,)).fetchone()
    if object_row is None:
        abort(404)

    if request.method == "POST":
        data, errors = read_object_form()
        data["object_id"] = object_id  # ид менять нельзя
        if not errors:
            db.execute(
                "UPDATE objects SET hierarchy_level = :hierarchy_level, parent_id = :parent_id, "
                "object_kind = :object_kind, dispatch_name = :dispatch_name, "
                "latitude = :latitude, longitude = :longitude WHERE object_id = :object_id",
                data,
            )
            db.commit()
            flash("Изменения сохранены.", "success")
            return redirect(url_for("settings.objects_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="objects", title=f"Объект {object_id}",
        fields=object_form_fields(object_row, is_new=False), back_url=url_for("settings.objects_list"),
    )


@settings_blueprint.route("/objects/<int:object_id>/delete", methods=["POST"])
@settings_access_required
def object_delete(object_id):
    db = get_db()
    channels_count = db.execute(
        "SELECT COUNT(*) FROM sensor_channels WHERE object_id = ?", (object_id,)
    ).fetchone()[0]
    children_count = db.execute("SELECT COUNT(*) FROM objects WHERE parent_id = ?", (object_id,)).fetchone()[0]

    if channels_count or children_count:
        flash(
            f"Нельзя удалить: к объекту привязано датчиков — {channels_count}, "
            f"дочерних объектов — {children_count}. Сначала перенесите их.",
            "error",
        )
    else:
        db.execute("DELETE FROM objects WHERE object_id = ?", (object_id,))
        db.commit()
        flash("Объект удалён.", "success")
    return redirect(url_for("settings.objects_list"))


# ===========================================================================
# Справочник «Журнал датчиков»
# ===========================================================================

@settings_blueprint.route("/channels")
@settings_access_required
def channels_list():
    search_text = request.args.get("q", "").strip()
    system_filter = request.args.get("system", "")
    sensor_filter = request.args.get("sensor", "")
    object_filter = request.args.get("object", "", type=str)

    # Собираем условие WHERE из заполненных фильтров
    conditions, parameters = [], []
    if search_text:
        conditions.append("(sensor_name LIKE ? OR system_tag LIKE ? OR CAST(channel_id AS TEXT) = ?)")
        parameters += [f"%{search_text}%", f"%{search_text}%", search_text]
    if system_filter:
        conditions.append("system_type = ?")
        parameters.append(system_filter)
    if sensor_filter:
        conditions.append("sensor_type = ?")
        parameters.append(sensor_filter)
    if object_filter.isdigit():
        conditions.append("sensor_channels.object_id = ?")
        parameters.append(int(object_filter))
    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    db = get_db()
    total_rows = db.execute(f"SELECT COUNT(*) FROM sensor_channels {where_sql}", parameters).fetchone()[0]
    page_number, page_count, offset = paginate(total_rows)

    channel_rows = db.execute(
        f"""
        SELECT sensor_channels.*, objects.dispatch_name AS object_name
        FROM sensor_channels
        LEFT JOIN objects ON objects.object_id = sensor_channels.object_id
        {where_sql}
        ORDER BY channel_id
        LIMIT ? OFFSET ?
        """,
        parameters + [config.ROWS_PER_PAGE, offset],
    ).fetchall()

    object_options = db.execute(
        "SELECT object_id, dispatch_name FROM objects ORDER BY dispatch_name"
    ).fetchall()

    return render_template(
        "settings/channels.html",
        channel_rows=channel_rows, search_text=search_text,
        system_filter=system_filter, sensor_filter=sensor_filter, object_filter=object_filter,
        system_types=ENGINEERING_SYSTEM_TYPES, sensor_types=SENSOR_TYPES, object_options=object_options,
        total_rows=total_rows, page_number=page_number, page_count=page_count,
    )


def channel_form_fields(channel_row=None, is_new=True):
    values = dict(channel_row) if channel_row else {}
    object_options = [("", "— не привязан —")] + [
        (row["object_id"], f"{row['dispatch_name']} ({row['object_id']})")
        for row in get_db().execute("SELECT object_id, dispatch_name FROM objects ORDER BY dispatch_name")
    ]
    fields = [
        field("channel_id", "Ид канала данных", "number", values.get("channel_id", "")),
        field("system_type", "Тип инженерной системы", "select", values.get("system_type", ""),
              [(name, name) for name in ENGINEERING_SYSTEM_TYPES]),
        field("sensor_type", "Тип датчика", "select", values.get("sensor_type", ""),
              [(name, name) for name in SENSOR_TYPES]),
        field("sensor_name", "Название датчика", "text", values.get("sensor_name", "")),
        field("system_tag", "Тег инженерной системы", "text", values.get("system_tag", ""), required=False),
        field("object_id", "Объект, где установлен", "select", values.get("object_id") or "",
              object_options, required=False),
    ]
    if not is_new:
        fields[0]["readonly"] = True
    return fields


def read_channel_form():
    errors = []
    channel_id, error_text = form_number("channel_id")
    if error_text:
        errors.append(f"Ид канала: {error_text}")
    object_id, error_text = form_number("object_id", allow_empty=True)
    if error_text:
        errors.append(f"Объект: {error_text}")

    data = {
        "channel_id": channel_id,
        "system_type": request.form.get("system_type", ""),
        "sensor_type": request.form.get("sensor_type", ""),
        "sensor_name": request.form.get("sensor_name", "").strip(),
        "system_tag": request.form.get("system_tag", "").strip(),
        "object_id": object_id,
    }
    if data["system_type"] not in ENGINEERING_SYSTEM_TYPES:
        errors.append("Выберите тип инженерной системы из списка.")
    if data["sensor_type"] not in SENSOR_TYPES:
        errors.append("Выберите тип датчика из списка.")
    if not data["sensor_name"]:
        errors.append("Укажите название датчика.")
    return data, errors


@settings_blueprint.route("/channels/new", methods=["GET", "POST"])
@settings_access_required
def channel_create():
    if request.method == "POST":
        data, errors = read_channel_form()
        db = get_db()
        if not errors and db.execute(
            "SELECT 1 FROM sensor_channels WHERE channel_id = ?", (data["channel_id"],)
        ).fetchone():
            errors.append(f"Канал {data['channel_id']} уже есть.")
        if not errors:
            db.execute(
                "INSERT INTO sensor_channels(channel_id, system_type, sensor_type, sensor_name, system_tag, "
                "object_id) VALUES(:channel_id, :system_type, :sensor_type, :sensor_name, :system_tag, :object_id)",
                data,
            )
            db.commit()
            flash("Датчик добавлен.", "success")
            return redirect(url_for("settings.channels_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="channels", title="Новый датчик",
        fields=channel_form_fields(), back_url=url_for("settings.channels_list"),
    )


@settings_blueprint.route("/channels/<int:channel_id>/edit", methods=["GET", "POST"])
@settings_access_required
def channel_edit(channel_id):
    db = get_db()
    channel_row = db.execute("SELECT * FROM sensor_channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if channel_row is None:
        abort(404)

    if request.method == "POST":
        data, errors = read_channel_form()
        data["channel_id"] = channel_id
        if not errors:
            db.execute(
                "UPDATE sensor_channels SET system_type = :system_type, sensor_type = :sensor_type, "
                "sensor_name = :sensor_name, system_tag = :system_tag, object_id = :object_id "
                "WHERE channel_id = :channel_id",
                data,
            )
            db.commit()
            flash("Изменения сохранены.", "success")
            return redirect(url_for("settings.channels_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="channels", title=f"Датчик {channel_id}",
        fields=channel_form_fields(channel_row, is_new=False), back_url=url_for("settings.channels_list"),
    )


@settings_blueprint.route("/channels/<int:channel_id>/delete", methods=["POST"])
@settings_access_required
def channel_delete(channel_id):
    db = get_db()
    db.execute("DELETE FROM sensor_channels WHERE channel_id = ?", (channel_id,))
    db.commit()
    flash("Датчик удалён.", "success")
    return redirect(url_for("settings.channels_list"))


@settings_blueprint.route("/channels/import", methods=["POST"])
@settings_access_required
def channels_import():
    """
    Импорт датчиков из CSV (кодировка UTF-8).

    Обязательные колонки: ид_канала_данных, тип_инж_системы, тип_датчика, название_датчика.
    Необязательные: тег_инженерной_системы, ид_объекта (или ид_объект).
    Существующие каналы обновляются, новые — добавляются.
    Если колонки ид_объекта нет, привязка к объекту у существующих каналов не меняется.
    """
    uploaded_file = request.files.get("csv_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Выберите CSV-файл.", "error")
        return redirect(url_for("settings.channels_list"))

    text = uploaded_file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    required_columns = {"ид_канала_данных", "тип_инж_системы", "тип_датчика", "название_датчика"}
    missing_columns = required_columns - set(reader.fieldnames or [])
    if missing_columns:
        flash("В файле нет колонок: " + ", ".join(sorted(missing_columns)), "error")
        return redirect(url_for("settings.channels_list"))

    # Колонка объекта может называться по-разному: ид_объекта или ид_объект (как в ноутбуке)
    object_column = next((name for name in ("ид_объекта", "ид_объект") if name in (reader.fieldnames or [])), None)
    has_object_column = object_column is not None
    db = get_db()
    imported_count, skipped_count = 0, 0

    for row in reader:
        try:
            channel_id = int(row["ид_канала_данных"])
        except (TypeError, ValueError):
            skipped_count += 1
            continue

        object_id = None
        if has_object_column and (row.get(object_column) or "").strip().isdigit():
            object_id = int(row[object_column])

        db.execute(
            """
            INSERT INTO sensor_channels(channel_id, system_type, sensor_type, system_tag, sensor_name, object_id)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                system_type = excluded.system_type,
                sensor_type = excluded.sensor_type,
                system_tag  = excluded.system_tag,
                sensor_name = excluded.sensor_name,
                object_id   = CASE WHEN ? THEN excluded.object_id ELSE sensor_channels.object_id END
            """,
            (channel_id, row["тип_инж_системы"], row["тип_датчика"], row.get("тег_инженерной_системы", ""),
             row["название_датчика"], object_id, has_object_column),
        )
        imported_count += 1

    db.commit()
    flash(f"Импортировано датчиков: {imported_count}. Пропущено строк с ошибкой: {skipped_count}.", "success")
    return redirect(url_for("settings.channels_list"))


# ===========================================================================
# Справочник «Роли пользователей»
# ===========================================================================

PROTECTED_ROLE_CODES = {"admin", "dispatcher"}  # обязательные роли нельзя удалить


@settings_blueprint.route("/roles")
@settings_access_required
def roles_list():
    role_rows = get_db().execute(
        "SELECT roles.*, (SELECT COUNT(*) FROM users WHERE users.role_id = roles.id) AS users_count "
        "FROM roles ORDER BY id"
    ).fetchall()
    return render_template("settings/roles.html", role_rows=role_rows, protected_codes=PROTECTED_ROLE_CODES)


def role_form_fields(role_row=None):
    values = dict(role_row) if role_row else {}
    fields = [
        field("code", "Код роли", "text", values.get("code", ""),
              hint="Латиницей, без пробелов, например: engineer"),
        field("name", "Название", "text", values.get("name", "")),
        field("description", "Описание", "text", values.get("description", ""), required=False),
        field("can_manage_settings", "Доступ к разделу «Настройки»", "checkbox",
              values.get("can_manage_settings", 0), required=False),
    ]
    if role_row and role_row["code"] in PROTECTED_ROLE_CODES:
        fields[0]["readonly"] = True
    return fields


def read_role_form():
    data = {
        "code": request.form.get("code", "").strip(),
        "name": request.form.get("name", "").strip(),
        "description": request.form.get("description", "").strip(),
        "can_manage_settings": 1 if request.form.get("can_manage_settings") else 0,
    }
    errors = []
    if not data["code"] or not data["code"].replace("_", "").isalnum() or not data["code"].isascii():
        errors.append("Код роли: только латинские буквы, цифры и «_».")
    if not data["name"]:
        errors.append("Укажите название роли.")
    return data, errors


@settings_blueprint.route("/roles/new", methods=["GET", "POST"])
@settings_access_required
def role_create():
    if request.method == "POST":
        data, errors = read_role_form()
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM roles WHERE code = ?", (data["code"],)).fetchone():
            errors.append("Роль с таким кодом уже есть.")
        if not errors:
            db.execute(
                "INSERT INTO roles(code, name, description, can_manage_settings) "
                "VALUES(:code, :name, :description, :can_manage_settings)",
                data,
            )
            db.commit()
            flash("Роль добавлена.", "success")
            return redirect(url_for("settings.roles_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="roles", title="Новая роль",
        fields=role_form_fields(), back_url=url_for("settings.roles_list"),
    )


@settings_blueprint.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@settings_access_required
def role_edit(role_id):
    db = get_db()
    role_row = db.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if role_row is None:
        abort(404)

    if request.method == "POST":
        data, errors = read_role_form()
        if role_row["code"] in PROTECTED_ROLE_CODES:
            data["code"] = role_row["code"]
        # Защита от ошибки: роль «Администратор» всегда имеет доступ к настройкам
        if role_row["code"] == "admin":
            data["can_manage_settings"] = 1
        if not errors:
            data["id"] = role_id
            db.execute(
                "UPDATE roles SET code = :code, name = :name, description = :description, "
                "can_manage_settings = :can_manage_settings WHERE id = :id",
                data,
            )
            db.commit()
            flash("Изменения сохранены.", "success")
            return redirect(url_for("settings.roles_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="roles", title=f"Роль «{role_row['name']}»",
        fields=role_form_fields(role_row), back_url=url_for("settings.roles_list"),
    )


@settings_blueprint.route("/roles/<int:role_id>/delete", methods=["POST"])
@settings_access_required
def role_delete(role_id):
    db = get_db()
    role_row = db.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    users_count = db.execute("SELECT COUNT(*) FROM users WHERE role_id = ?", (role_id,)).fetchone()[0]

    if role_row is None:
        abort(404)
    if role_row["code"] in PROTECTED_ROLE_CODES:
        flash("Роли «Администратор» и «Диспетчер» обязательны — их нельзя удалить.", "error")
    elif users_count:
        flash(f"Роль назначена пользователям ({users_count}). Сначала смените им роль.", "error")
    else:
        db.execute("DELETE FROM roles WHERE id = ?", (role_id,))
        db.commit()
        flash("Роль удалена.", "success")
    return redirect(url_for("settings.roles_list"))


# ===========================================================================
# Справочник «Пользователи»
# ===========================================================================

@settings_blueprint.route("/users")
@settings_access_required
def users_list():
    user_rows = get_db().execute(
        "SELECT users.*, roles.name AS role_name FROM users JOIN roles ON roles.id = users.role_id "
        "ORDER BY users.id"
    ).fetchall()
    return render_template("settings/users.html", user_rows=user_rows)


def user_form_fields(user_row=None):
    values = dict(user_row) if user_row else {}
    role_options = [(row["id"], row["name"]) for row in get_db().execute("SELECT id, name FROM roles ORDER BY id")]
    is_new = user_row is None
    return [
        field("login", "Логин", "text", values.get("login", "")),
        field("full_name", "ФИО", "text", values.get("full_name", ""), required=False),
        field("role_id", "Роль", "select", values.get("role_id", ""), role_options),
        field("password", "Пароль", "password", "", required=is_new,
              hint="Не короче 6 символов." if is_new else "Оставьте пустым, чтобы не менять."),
        field("is_active", "Учётная запись активна", "checkbox", values.get("is_active", 1), required=False),
    ]


def read_user_form(is_new):
    role_id, role_error = form_number("role_id")
    data = {
        "login": request.form.get("login", "").strip(),
        "full_name": request.form.get("full_name", "").strip(),
        "role_id": role_id,
        "password": request.form.get("password", ""),
        "is_active": 1 if request.form.get("is_active") else 0,
    }
    errors = []
    if not data["login"]:
        errors.append("Укажите логин.")
    if role_error:
        errors.append("Выберите роль.")
    password_is_needed = is_new or data["password"]
    if password_is_needed and len(data["password"]) < 6:
        errors.append("Пароль должен быть не короче 6 символов.")
    return data, errors


@settings_blueprint.route("/users/new", methods=["GET", "POST"])
@settings_access_required
def user_create():
    if request.method == "POST":
        data, errors = read_user_form(is_new=True)
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM users WHERE login = ?", (data["login"],)).fetchone():
            errors.append("Пользователь с таким логином уже есть.")
        if not errors:
            db.execute(
                "INSERT INTO users(login, password_hash, full_name, role_id, is_active, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (data["login"], generate_password_hash(data["password"]), data["full_name"],
                 data["role_id"], data["is_active"], datetime.now().isoformat(timespec="seconds")),
            )
            db.commit()
            flash("Пользователь добавлен.", "success")
            return redirect(url_for("settings.users_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="users", title="Новый пользователь",
        fields=user_form_fields(), back_url=url_for("settings.users_list"),
    )


@settings_blueprint.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@settings_access_required
def user_edit(user_id):
    db = get_db()
    user_row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user_row is None:
        abort(404)

    if request.method == "POST":
        data, errors = read_user_form(is_new=False)
        editing_myself = user_id == g.current_user["id"]
        if editing_myself and not data["is_active"]:
            errors.append("Нельзя заблокировать собственную учётную запись.")
        if not errors and db.execute(
            "SELECT 1 FROM users WHERE login = ? AND id <> ?", (data["login"], user_id)
        ).fetchone():
            errors.append("Пользователь с таким логином уже есть.")
        if not errors:
            db.execute(
                "UPDATE users SET login = ?, full_name = ?, role_id = ?, is_active = ? WHERE id = ?",
                (data["login"], data["full_name"], data["role_id"], data["is_active"], user_id),
            )
            if data["password"]:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(data["password"]), user_id),
                )
            db.commit()
            flash("Изменения сохранены.", "success")
            return redirect(url_for("settings.users_list"))
        for error_text in errors:
            flash(error_text, "error")

    return render_template(
        "settings/form.html", section="users", title=f"Пользователь {user_row['login']}",
        fields=user_form_fields(user_row), back_url=url_for("settings.users_list"),
    )


@settings_blueprint.route("/users/<int:user_id>/delete", methods=["POST"])
@settings_access_required
def user_delete(user_id):
    if user_id == g.current_user["id"]:
        flash("Нельзя удалить собственную учётную запись.", "error")
    else:
        db = get_db()
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        flash("Пользователь удалён.", "success")
    return redirect(url_for("settings.users_list"))


# ===========================================================================
# Модель прогнозирования
# ===========================================================================

def get_active_model_path(db):
    """Путь к файлу выбранной модели или None."""
    active_model_id = read_setting(db, "active_model_id")
    if not active_model_id:
        return None
    model_row = db.execute("SELECT stored_name FROM ml_models WHERE id = ?", (active_model_id,)).fetchone()
    if model_row is None:
        return None
    return config.UPLOADED_MODELS_DIR / model_row["stored_name"]


@settings_blueprint.route("/model")
@settings_access_required
def model_page():
    db = get_db()
    model_rows = db.execute("SELECT * FROM ml_models ORDER BY id DESC").fetchall()
    active_model_id = read_setting(db, "active_model_id")
    prediction_enabled = read_setting(db, "prediction_enabled", "0") == "1"

    # Пытаемся прочитать выбранную модель, чтобы показать её описание
    model_description, model_error = None, None
    model_path = get_active_model_path(db)
    if model_path is not None:
        try:
            model_description = active_model_holder.get(model_path).describe()
        except ModelLoadError as error:
            model_error = str(error)

    return render_template(
        "settings/model.html",
        model_rows=model_rows,
        active_model_id=int(active_model_id) if active_model_id else None,
        prediction_enabled=prediction_enabled,
        model_description=model_description,
        model_error=model_error,
    )


@settings_blueprint.route("/model/upload", methods=["POST"])
@settings_access_required
def model_upload():
    uploaded_file = request.files.get("model_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Выберите файл модели.", "error")
        return redirect(url_for("settings.model_page"))
    if not uploaded_file.filename.lower().endswith(".joblib"):
        flash("Нужен файл в формате .joblib.", "error")
        return redirect(url_for("settings.model_page"))

    # Сохраняем под уникальным именем, чтобы файлы с одинаковыми именами не затирали друг друга
    stored_name = f"{uuid.uuid4().hex}.joblib"
    stored_path = config.UPLOADED_MODELS_DIR / stored_name
    uploaded_file.save(stored_path)

    # Сразу проверяем, что файл читается как модель
    try:
        FireRiskModel(stored_path)
    except ModelLoadError as error:
        stored_path.unlink(missing_ok=True)
        flash(f"Файл не принят. {error}", "error")
        return redirect(url_for("settings.model_page"))

    db = get_db()
    cursor = db.execute(
        "INSERT INTO ml_models(stored_name, original_name, uploaded_at, uploaded_by) VALUES(?, ?, ?, ?)",
        (stored_name, uploaded_file.filename, datetime.now().isoformat(sep=" ", timespec="seconds"),
         g.current_user["login"]),
    )
    db.commit()

    # Если модель ещё не выбрана — выбираем только что загруженную
    if not read_setting(db, "active_model_id"):
        write_setting(db, "active_model_id", cursor.lastrowid)

    flash(f"Модель «{uploaded_file.filename}» загружена и проверена.", "success")
    return redirect(url_for("settings.model_page"))


@settings_blueprint.route("/model/<int:model_id>/activate", methods=["POST"])
@settings_access_required
def model_activate(model_id):
    db = get_db()
    if db.execute("SELECT 1 FROM ml_models WHERE id = ?", (model_id,)).fetchone() is None:
        abort(404)
    write_setting(db, "active_model_id", model_id)
    if read_setting(db, "prediction_enabled", "0") == "1":
        try:
            emulation_runner.recalculate_predictions()
        except ModelLoadError as error:
            flash(str(error), "error")
    flash("Модель выбрана для прогнозирования.", "success")
    return redirect(url_for("settings.model_page"))


@settings_blueprint.route("/model/<int:model_id>/delete", methods=["POST"])
@settings_access_required
def model_delete(model_id):
    db = get_db()
    model_row = db.execute("SELECT * FROM ml_models WHERE id = ?", (model_id,)).fetchone()
    if model_row is None:
        abort(404)

    if read_setting(db, "active_model_id") == str(model_id):
        write_setting(db, "active_model_id", "")
        write_setting(db, "prediction_enabled", "0")
        active_model_holder.forget()

    (config.UPLOADED_MODELS_DIR / model_row["stored_name"]).unlink(missing_ok=True)
    db.execute("DELETE FROM ml_models WHERE id = ?", (model_id,))
    db.commit()
    flash("Модель удалена.", "success")
    return redirect(url_for("settings.model_page"))


@settings_blueprint.route("/model/toggle", methods=["POST"])
@settings_access_required
def model_toggle():
    db = get_db()
    turn_on = request.form.get("enabled") == "1"
    if turn_on and get_active_model_path(db) is None:
        flash("Сначала загрузите и выберите модель.", "error")
    else:
        write_setting(db, "prediction_enabled", "1" if turn_on else "0")
        if turn_on:
            # Если эмуляция уже шла — сразу считаем прогноз по накопленным данным
            try:
                emulation_runner.recalculate_predictions()
            except ModelLoadError as error:
                flash(str(error), "error")
        flash("Прогнозирование включено." if turn_on else "Прогнозирование выключено.", "success")
    return redirect(url_for("settings.model_page"))


# ===========================================================================
# Эмуляция
# ===========================================================================

def list_uploaded_logs():
    """Файлы журналов, которые уже лежат в папке загрузок (новые сверху)."""
    log_files = sorted(
        config.UPLOADED_LOGS_DIR.glob("*.csv"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    return [
        {"name": path.name, "size_mb": round(path.stat().st_size / 1024 / 1024, 1)}
        for path in log_files
    ]


@settings_blueprint.route("/emulation")
@settings_access_required
def emulation_page():
    db = get_db()
    return render_template(
        "settings/emulation.html",
        log_files=list_uploaded_logs(),
        speed_options=SPEED_OPTIONS,
        state=emulation_runner.snapshot(),
        prediction_enabled=read_setting(db, "prediction_enabled", "0") == "1",
        external_counts={name: info["rows_count"] for name, info in describe_external_data(db).items()},
        external_titles=DATASET_TITLES,
    )


@settings_blueprint.route("/emulation/upload", methods=["POST"])
@settings_access_required
def emulation_upload():
    uploaded_file = request.files.get("log_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Выберите CSV-файл журнала.", "error")
        return redirect(url_for("settings.emulation_page"))
    if not uploaded_file.filename.lower().endswith(".csv"):
        flash("Журнал должен быть в формате .csv.", "error")
        return redirect(url_for("settings.emulation_page"))

    # secure_filename удаляет кириллицу, поэтому добавляем метку времени,
    # чтобы имя точно было непустым и уникальным
    safe_name = secure_filename(uploaded_file.filename) or "journal.csv"
    stored_name = f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name}"
    if not stored_name.endswith(".csv"):
        stored_name += ".csv"
    uploaded_file.save(config.UPLOADED_LOGS_DIR / stored_name)
    flash(f"Журнал загружен как {stored_name}.", "success")
    return redirect(url_for("settings.emulation_page"))


@settings_blueprint.route("/emulation/start", methods=["POST"])
@settings_access_required
def emulation_start():
    log_name = request.form.get("log_name", "")
    speed = request.form.get("speed", 600, type=int)

    # Разрешаем только файлы из папки загрузок: так нельзя подсунуть
    # путь к произвольному файлу на сервере
    uploaded_log_names = {item["name"] for item in list_uploaded_logs()}
    if log_name not in uploaded_log_names:
        flash("Выберите журнал из списка загруженных.", "error")
        return redirect(url_for("settings.emulation_page"))
    log_path = config.UPLOADED_LOGS_DIR / log_name

    db = get_db()
    channel_rows = [dict(row) for row in db.execute("SELECT channel_id, sensor_type, object_id FROM sensor_channels")]
    object_rows = [dict(row) for row in db.execute("SELECT * FROM objects")]

    try:
        emulation_runner.start(
            log_file_path=log_path,
            log_display_name=log_name,
            speed=speed if speed in SPEED_OPTIONS else 600,
            channel_rows=channel_rows,
            object_rows=object_rows,
            get_prediction_model=load_model_if_prediction_enabled,
            external_data=load_external_data(db),
        )
        flash("Эмуляция запущена. Откройте рабочее место диспетчера, чтобы наблюдать.", "success")
    except (ValueError, OSError) as error:
        flash(f"Не удалось запустить эмуляцию: {error}", "error")
    return redirect(url_for("settings.emulation_page"))


@settings_blueprint.route("/emulation/control", methods=["POST"])
@settings_access_required
def emulation_control():
    action = request.form.get("action")
    if action == "pause":
        emulation_runner.pause()
    elif action == "resume":
        emulation_runner.resume()
    elif action == "stop":
        emulation_runner.stop()
    elif action == "speed":
        emulation_runner.set_speed(request.form.get("speed", 600, type=int))
    return redirect(url_for("settings.emulation_page"))


# ===========================================================================
# Дополнительные данные: горячие работы, погода, реестр инцидентов
# ===========================================================================

@settings_blueprint.route("/external")
@settings_access_required
def external_data_page():
    return render_template(
        "settings/external.html",
        summary=describe_external_data(get_db()),
        titles=DATASET_TITLES,
    )


@settings_blueprint.route("/external/<dataset_name>/import", methods=["POST"])
@settings_access_required
def external_data_import(dataset_name):
    if dataset_name not in PARSERS:
        abort(404)

    uploaded_file = request.files.get("csv_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Выберите CSV-файл.", "error")
        return redirect(url_for("settings.external_data_page"))

    # Шаг 1. Читаем и проверяем файл
    try:
        table = read_uploaded_csv(uploaded_file.read())
        data, skipped_count = PARSERS[dataset_name](table)
    except ExternalDataError as error:
        flash(f"{DATASET_TITLES[dataset_name]}: файл не принят. {error}", "error")
        return redirect(url_for("settings.external_data_page"))

    if data.empty:
        flash(f"{DATASET_TITLES[dataset_name]}: в файле нет ни одной корректной строки.", "error")
        return redirect(url_for("settings.external_data_page"))

    # Шаг 2. Сохраняем: заменяем прежние данные или добавляем к ним
    db = get_db()
    replace_existing = request.form.get("mode", "replace") == "replace"
    saved_count = save_dataset(db, dataset_name, data, replace_existing)

    message = f"{DATASET_TITLES[dataset_name]}: загружено строк — {saved_count}."
    if skipped_count:
        message += f" Пропущено строк с ошибками — {skipped_count}."
    unknown_count = count_unknown_objects(db, data)
    if unknown_count:
        message += f" Строк с объектами, которых нет в справочнике, — {unknown_count}: они не повлияют на прогноз."
    flash(message, "success")

    # Шаг 3. Если эмуляция идёт — сразу передаём ей новые данные
    refresh_running_emulation(db)
    return redirect(url_for("settings.external_data_page"))


@settings_blueprint.route("/external/<dataset_name>/clear", methods=["POST"])
@settings_access_required
def external_data_clear(dataset_name):
    if dataset_name not in PARSERS:
        abort(404)
    db = get_db()
    clear_dataset(db, dataset_name)
    refresh_running_emulation(db)
    flash(f"{DATASET_TITLES[dataset_name]}: данные удалены.", "success")
    return redirect(url_for("settings.external_data_page"))


@settings_blueprint.route("/external/<dataset_name>/template.csv")
@settings_access_required
def external_data_template(dataset_name):
    """Образец файла для заполнения."""
    if dataset_name not in TEMPLATE_CSV:
        abort(404)
    file_names = {
        "hot_works": "арм_контроль_горячие_работы.csv",
        "weather": "погода_часовая.csv",
        "incidents": "реестр_пожарных_инцидентов.csv",
    }
    return Response(
        "\ufeff" + TEMPLATE_CSV[dataset_name],  # метка BOM, чтобы Excel открыл кириллицу правильно
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_names[dataset_name])}"},
    )


def refresh_running_emulation(db):
    """Передаёт идущей эмуляции свежие дополнительные данные."""
    try:
        hot_works, weather, incidents = load_external_data(db)
        emulation_runner.update_external_data(hot_works, weather, incidents)
    except ModelLoadError as error:
        flash(str(error), "error")

