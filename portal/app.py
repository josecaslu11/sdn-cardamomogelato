from functools import wraps
from hmac import compare_digest
from os import getenv
from pathlib import Path
from secrets import token_hex

from dotenv import dotenv_values
from flask import Flask, flash, redirect, render_template, request, session, url_for

from db import detalle_sitio, initialize, listado_ipam, resumen_sitios
from ipam import IPAM, SITE_CODE, SITE_NAME


LAN_INTERFACES = ("Ethernet0/1", "Ethernet0/2")


def login_required(view):
    @wraps(view)
    def protected(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return protected


def create_app(env_file=None, *, database=None, config_file=None):
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

    ipam = IPAM(
        database if database is not None else getenv("PORTAL_DB_PATH", settings.get("PORTAL_DB_PATH")),
        config_file if config_file is not None else getenv("PORTAL_CONFIG_PATH", settings.get("PORTAL_CONFIG_PATH")),
    )
    initialize(ipam.database)

    @app.get("/")
    @login_required
    def sites():
        sitios, indicadores = resumen_sitios(ipam.database)
        return render_template("sites.html", sitios=sitios, indicadores=indicadores)

    @app.get("/sitios/<int:sitio_id>")
    @login_required
    def site_detail(sitio_id):
        # Los identificadores de SQLite son enteros de 64 bits con signo.
        detalle = detalle_sitio(ipam.database, sitio_id) if 0 < sitio_id < 2**63 else None
        if detalle is None:
            return render_template("site_not_found.html"), 404
        sitio, asignaciones = detalle
        return render_template("site_detail.html", sitio=sitio, asignaciones=asignaciones)

    @app.get("/ipam")
    @login_required
    def ipam_list():
        filtros = {campo: request.args.get(campo, "") for campo in ("router", "recurso", "estado")}
        asignaciones, opciones = listado_ipam(ipam.database, **filtros)
        return render_template(
            "ipam.html", asignaciones=asignaciones, filtros=filtros, opciones=opciones
        )

    @app.route("/sitios/nuevo", methods=["GET", "POST"])
    @login_required
    def new_site():
        valores = {"nombre": "", "codigo": "", "lan_if": LAN_INTERFACES[0]}
        error = None
        if "csrf_token" not in session:
            session["csrf_token"] = token_hex(32)
        if request.method == "POST":
            valores = {campo: request.form.get(campo, "") for campo in valores}
            if not compare_digest(
                request.form.get("csrf_token", "").encode(), session["csrf_token"].encode()
            ):
                error = "La solicitud no es válida. Vuelve a enviar el formulario."
            elif valores["lan_if"] not in LAN_INTERFACES:
                error = "Selecciona una interfaz LAN de la lista."
            else:
                try:
                    sitio = ipam.reservar_sitio(**valores, usuario=username)
                except ValueError as exc:
                    error = str(exc)
                else:
                    flash(f"Sitio reservado en {sitio['router']}. Estado: aprovisionando.")
                    return redirect(url_for("sites"), code=303)
        return render_template(
            "new_site.html",
            valores=valores,
            error=error,
            interfaces=LAN_INTERFACES,
            patron_nombre=SITE_NAME.pattern.replace("-]", r"\-]"),
            patron_codigo=SITE_CODE.pattern.replace("-]", r"\-]"),
        ), 400 if error else 200

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
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    return app
