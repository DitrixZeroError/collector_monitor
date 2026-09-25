"""
Вход в систему по логину и паролю и проверка прав доступа.

  * login_required  — страницу видит любой вошедший пользователь;
  * settings_access_required — только роль с правом «Настройки» (Администратор).
"""

from functools import wraps

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from database import get_db

auth_blueprint = Blueprint("auth", __name__)


def load_current_user():
    """
    Выполняется перед каждым запросом: находит вошедшего пользователя
    по номеру из сессии и кладёт его в g.current_user (или None).
    """
    user_id = session.get("user_id")
    g.current_user = None
    if user_id is None:
        return

    user_row = get_db().execute(
        "SELECT users.id, users.login, users.full_name, users.is_active, "
        "       roles.code AS role_code, roles.name AS role_name, roles.can_manage_settings "
        "FROM users JOIN roles ON roles.id = users.role_id "
        "WHERE users.id = ?",
        (user_id,),
    ).fetchone()

    # Если пользователя удалили или заблокировали — выходим из системы
    if user_row is None or not user_row["is_active"]:
        session.clear()
        return
    g.current_user = user_row


def login_required(view_function):
    """Декоратор: не пускает на страницу без входа в систему."""

    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if g.current_user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view_function(*args, **kwargs)

    return wrapped_view


def settings_access_required(view_function):
    """Декоратор: пускает только пользователей с правом управлять настройками."""

    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if g.current_user is None:
            return redirect(url_for("auth.login", next=request.path))
        if not g.current_user["can_manage_settings"]:
            flash("Раздел «Настройки» доступен только администратору.", "error")
            return redirect(url_for("pages.dispatcher"))
        return view_function(*args, **kwargs)

    return wrapped_view


@auth_blueprint.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_text = request.form.get("login", "").strip()
        password_text = request.form.get("password", "")

        user_row = get_db().execute(
            "SELECT id, password_hash, is_active FROM users WHERE login = ?", (login_text,)
        ).fetchone()

        password_is_correct = user_row is not None and check_password_hash(
            user_row["password_hash"], password_text
        )

        if not password_is_correct:
            flash("Неверный логин или пароль.", "error")
        elif not user_row["is_active"]:
            flash("Учётная запись заблокирована. Обратитесь к администратору.", "error")
        else:
            session.clear()
            session["user_id"] = user_row["id"]
            # После входа диспетчер сразу попадает на своё рабочее место
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/") and not next_page.startswith("//"):
                return redirect(next_page)
            return redirect(url_for("pages.dispatcher"))

    return render_template("login.html")


@auth_blueprint.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
