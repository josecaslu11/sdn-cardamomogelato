from hmac import compare_digest
from pathlib import Path
from secrets import token_hex

from dotenv import dotenv_values
from flask import Flask, redirect, render_template, request, session, url_for


def create_app(env_file=None):
    if env_file is None:
        env_file = Path(__file__).with_name(".env")
    settings = dotenv_values(env_file, interpolate=False)
    username = settings.get("PORTAL_USER")
    password = settings.get("PORTAL_PASSWORD")
    if not username or not password:
        raise RuntimeError("Define PORTAL_USER y PORTAL_PASSWORD en .env.")

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.get("PORTAL_SECRET_KEY") or token_hex(32),
        SESSION_COOKIE_SAMESITE="Lax",
    )

    @app.get("/")
    def sites():
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return render_template("sites.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("authenticated"):
            return redirect(url_for("sites"))
        if request.method == "GET":
            return render_template("login.html")

        user_matches = compare_digest(
            request.form.get("username", "").encode(), username.encode()
        )
        password_matches = compare_digest(
            request.form.get("password", "").encode(), password.encode()
        )
        if not (user_matches and password_matches):
            return render_template("login.html", error="Usuario o contraseña incorrectos."), 401

        session.clear()
        session["authenticated"] = True
        return redirect(url_for("sites"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    return app
