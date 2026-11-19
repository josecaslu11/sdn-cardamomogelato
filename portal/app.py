from functools import wraps
from hmac import compare_digest
from os import getenv
from pathlib import Path
from secrets import token_hex
import sqlite3

from dotenv import dotenv_values
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from ansible_files import AnsibleFiles, InventoryError, dynamic_inventory, host_vars
from db import bitacora, detalle_sitio, initialize, listado_ipam, resumen_sitios, transaction, vista_inventario
from ipam import IPAM, SITE_CODE, SITE_NAME
from progress import ProgresoNoDisponible, consultar_progreso, simular
from runner import AltaRunner, ROUTERS, RunnerBusy, RunnerError


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
    files = AnsibleFiles(ipam.config_file, lambda name, default=None: getenv(
        name, settings.get(name, default)
    ))

    runner = AltaRunner(ipam, files)
    runner.recuperar()

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
        registrados = vista_inventario(ipam.database)
        publicado = next((row for row in registrados if row["id"] == sitio_id), None)
        progreso = consultar_progreso(ipam.database, sitio_id=sitio_id)
        permitir_simular = (
            files.dry_run and publicado is not None and sitio["estado"] == "aprovisionando"
            and (progreso["id"] is None or (progreso["simulado"] and not progreso["automatico"]))
        )
        permitir_aprovisionar = (
            publicado is not None and sitio["router"] in ROUTERS
            and sitio["estado"] in ("aprovisionando", "fallido")
        )
        permitir_aprovisionar = permitir_aprovisionar and not (
            sitio["estado"] == "fallido" and progreso["tipo"] == "baja"
        )
        permitir_baja = publicado is not None and sitio["router"] in ROUTERS and (
            sitio["estado"] == "activo" or (sitio["estado"] == "fallido" and progreso["tipo"] == "baja")
        )
        permitir_baja = permitir_baja and progreso.get("codigo_salida") != -128
        permitir_liberar = (
            sitio["router"] in ROUTERS and sitio["estado"] == "retirado"
            and sitio["cuarentena_hasta"] is not None and sitio["liberado_en"] is None
        )
        if "csrf_token" not in session:
            session["csrf_token"] = token_hex(32)
        return render_template(
            "site_detail.html",
            progreso=progreso,
            permitir_simular=permitir_simular,
            permitir_aprovisionar=permitir_aprovisionar,
            permitir_baja=permitir_baja,
            permitir_liberar=permitir_liberar,
            sitio=sitio,
            asignaciones=asignaciones,
            inventario_yaml=dynamic_inventory(registrados),
            host_vars_yaml=host_vars(publicado) if publicado is not None else None,
            dry_run=files.dry_run,
        )

    @app.get("/sitios/<int:sitio_id>/progreso")
    @login_required
    def site_progress(sitio_id):
        progreso = consultar_progreso(ipam.database, sitio_id=sitio_id) if 0 < sitio_id < 2**63 else None
        if progreso is None:
            return jsonify(error="El sitio solicitado no existe."), 404
        return jsonify(progreso)

    @app.get("/runs/<int:run_id>/progress")
    @login_required
    def run_progress(run_id):
        progreso = consultar_progreso(ipam.database, run_id=run_id) if 0 < run_id < 2**63 else None
        if progreso is None:
            return jsonify(error="La ejecución solicitada no existe."), 404
        return jsonify(progreso)

    @app.post("/sitios/<int:sitio_id>/progreso/simular")
    @login_required
    def simulate_progress(sitio_id):
        if not files.dry_run:
            return jsonify(error="La simulación solo está disponible en modo de desarrollo."), 403
        csrf = session.get("csrf_token")
        if csrf is None or not compare_digest(request.form.get("csrf_token", "").encode(), csrf.encode()):
            return jsonify(error="La solicitud no es válida. Recarga la página."), 403
        if not 0 < sitio_id < 2**63:
            return jsonify(error="El sitio solicitado no existe."), 404
        try:
            progreso = simular(
                ipam.database, sitio_id, request.form.get("accion"),
                validar_modalidad=files.ensure_mode,
            )
        except (ProgresoNoDisponible, InventoryError) as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if progreso is None:
            return jsonify(error="El sitio solicitado no existe."), 404
        return jsonify(progreso)

    @app.post("/sitios/<int:sitio_id>/aprovisionar")
    @login_required
    def provision_site(sitio_id):
        return iniciar_operacion(sitio_id, "alta")

    @app.post("/sitios/<int:sitio_id>/baja")
    @login_required
    def retire_site(sitio_id):
        return iniciar_operacion(sitio_id, "baja")

    def iniciar_operacion(sitio_id, tipo):
        csrf = session.get("csrf_token")
        if csrf is None or not compare_digest(request.form.get("csrf_token", "").encode(), csrf.encode()):
            return jsonify(error="La solicitud no es válida. Recarga la página."), 403
        if not 0 < sitio_id < 2**63:
            return jsonify(error="El sitio solicitado no existe."), 404
        try:
            run_id = runner.iniciar(
                sitio_id, username, request.form.get("resultado", "exito"),
                tipo=tipo, confirmacion=request.form.get("confirmacion"),
            )
        except RunnerBusy as exc:
            return jsonify(error=str(exc)), 409
        except (RunnerError, InventoryError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        except (OSError, sqlite3.Error):
            return jsonify(error="No se pudo preparar la ejecución."), 500
        if run_id is None:
            return jsonify(error="El sitio solicitado no existe."), 404
        return jsonify(consultar_progreso(ipam.database, run_id=run_id)), 202

    @app.post("/sitios/<int:sitio_id>/liberar-cuarentena")
    @login_required
    def release_quarantine(sitio_id):
        csrf = session.get("csrf_token")
        if csrf is None or not compare_digest(request.form.get("csrf_token", "").encode(), csrf.encode()):
            return jsonify(error="La solicitud no es válida. Recarga la página."), 403
        if not 0 < sitio_id < 2**63 or detalle_sitio(ipam.database, sitio_id) is None:
            return jsonify(error="El sitio solicitado no existe."), 404
        try:
            with transaction(ipam.database) as db:
                files.ensure_mode(db)
                ipam.liberar_cuarentena(
                    sitio_id, request.form.get("confirmacion"), usuario=username, conexion=db
                )
        except (InventoryError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Cuarentena liberada.")
        return redirect(url_for("site_detail", sitio_id=sitio_id), code=303)

    @app.get("/bitacora")
    @login_required
    def audit():
        filtros = {campo: request.args.get(campo, "") for campo in ("router", "operacion", "resultado")}
        return render_template("audit.html", eventos=bitacora(ipam.database, **filtros),
                               filtros=filtros, routers=sorted(ROUTERS, key=lambda r: int(r[1:])))

    @app.get("/sitios/<int:sitio_id>/historial")
    @login_required
    def site_history(sitio_id):
        detalle = detalle_sitio(ipam.database, sitio_id) if 0 < sitio_id < 2**63 else None
        if detalle is None:
            return render_template("site_not_found.html"), 404
        return render_template("audit.html", sitio=detalle[0],
                               eventos=bitacora(ipam.database, sitio_id=sitio_id))

    @app.get("/runs/<int:run_id>")
    @login_required
    def run_history(run_id):
        progreso = consultar_progreso(ipam.database, run_id=run_id) if 0 < run_id < 2**63 else None
        if progreso is None:
            return render_template("site_not_found.html"), 404
        return render_template("run_history.html", progreso=progreso)

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
        status = 400
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
                    sitio = files.reserve(ipam, valores, username)
                except (OSError, sqlite3.Error, InventoryError) as exc:
                    app.logger.error("Error al guardar los archivos de alta (%s).", type(exc).__name__)
                    error = "No se pudo guardar el inventario. La reserva no se completó."
                    if isinstance(exc, InventoryError):
                        error += f" {exc}"
                    status = 500
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
        ), status if error else 200

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
