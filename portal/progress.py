"""Estado y transiciones del seguimiento de altas."""
from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from db import transaction


ETAPAS_ALTA = (
    ("Validar datos", "portal"),
    ("Reservar IDs y direcciones", "portal"),
    ("Generar archivos de variables", "portal"),
    ("Comprobar que el router responde", "portal"),
    ("IPsec", "ipsec"),
    ("Túneles DMVPN", "dmvpn"),
    ("LAN", "lan"),
    ("Enrutamiento OSPF", "routing"),
    ("SNMPv3", "snmp"),
    ("Alta en Zabbix", "zabbix"),
    ("Cerrar alta (marcar activo)", "portal"),
)


class ProgresoNoDisponible(ValueError):
    pass


def ahora_utc():
    return datetime.now(timezone.utc)


def pasos_pendientes():
    return [
        {"orden": orden, "nombre": nombre, "quien": quien, "estado": "pendiente",
         "segundos": None, "detalle": None}
        for orden, (nombre, quien) in enumerate(ETAPAS_ALTA, 1)
    ]


def crear_alta(db, sitio_id, *, simulado, fecha):
    run_id = db.execute(
        """INSERT INTO runs (site_id, tipo, simulado, estado, inicio)
           VALUES (?, 'alta', ?, 'en_curso', ?)""",
        (sitio_id, simulado, fecha),
    ).lastrowid
    db.executemany(
        """INSERT INTO run_steps (run_id, orden, nombre, quien, estado)
           VALUES (?, ?, ?, ?, 'pendiente')""",
        ((run_id, orden, nombre, quien) for orden, (nombre, quien) in enumerate(ETAPAS_ALTA, 1)),
    )
    return run_id


def aplicar_evento(db, run_id, orden, estado, *, fecha, detalle=None):
    """Aplica un evento de etapa en la transacción del llamador."""
    paso = db.execute(
        """SELECT p.estado, r.estado AS estado_run
           FROM run_steps p JOIN runs r ON r.id = p.run_id
           WHERE p.run_id = ? AND p.orden = ?""",
        (run_id, orden),
    ).fetchone()
    origen = {"en_curso": "pendiente", "ok": "en_curso", "error": "en_curso"}.get(estado)
    if paso is None or paso["estado_run"] != "en_curso" or paso["estado"] != origen:
        raise ProgresoNoDisponible("La etapa ya cambió o la ejecución terminó.")
    if db.execute(
        "SELECT 1 FROM run_steps WHERE run_id = ? AND orden < ? AND estado <> 'ok'",
        (run_id, orden),
    ).fetchone():
        raise ProgresoNoDisponible("Primero deben completarse las etapas anteriores.")
    db.execute(
        """UPDATE run_steps SET estado = ?, inicio = COALESCE(inicio, ?), fin = ?, detalle = ?
           WHERE run_id = ? AND orden = ?""",
        (estado, fecha, None if estado == "en_curso" else fecha, detalle, run_id, orden),
    )
    if estado == "error" or (
        estado == "ok" and not db.execute(
            "SELECT 1 FROM run_steps WHERE run_id = ? AND estado <> 'ok'", (run_id,)
        ).fetchone()
    ):
        db.execute("UPDATE runs SET estado = ?, fin = ? WHERE id = ?", (estado, fecha, run_id))


def _snapshot(db, run):
    if run is None:
        return {"id": None, "estado": "pendiente", "simulado": False, "automatico": False, "porcentaje": 0,
                "hechos": 0, "total": len(ETAPAS_ALTA), "pasos": pasos_pendientes(), "salida": []}
    pasos = []
    ahora = ahora_utc()
    for row in db.execute("SELECT * FROM run_steps WHERE run_id = ? ORDER BY orden", (run["id"],)):
        paso = {key: row[key] for key in ("orden", "nombre", "quien", "estado", "detalle")}
        segundos = None
        if row["inicio"]:
            inicio = datetime.fromisoformat(row["inicio"])
            fin = datetime.fromisoformat(row["fin"]) if row["fin"] else ahora
            segundos = max(0, int((fin - inicio).total_seconds()))
        paso["segundos"] = segundos
        pasos.append(paso)
    hechos = sum(paso["estado"] == "ok" for paso in pasos)
    lineas = db.execute(
        "SELECT texto FROM run_log WHERE run_id = ? ORDER BY numero_linea DESC LIMIT 15",
        (run["id"],),
    ).fetchall()
    return {
        "id": run["id"], "estado": run["estado"], "simulado": bool(run["simulado"]),
        "automatico": db.execute("SELECT 1 FROM runner_jobs WHERE run_id = ?", (run["id"],)).fetchone() is not None,
        "codigo_salida": run["codigo_salida"],
        "estado_sitio": db.execute("SELECT estado FROM sites WHERE id = ?", (run["site_id"],)).fetchone()[0],
        "resumen": {key: run[key] for key in ("ok_count", "changed_count", "failed_count")},
        "porcentaje": round(100 * hechos / len(pasos)), "hechos": hechos, "total": len(pasos),
        "pasos": pasos, "salida": [row["texto"] for row in reversed(lineas)],
    }


def _ultima_alta(db, sitio_id):
    return db.execute(
        "SELECT * FROM runs WHERE site_id = ? AND tipo = 'alta' ORDER BY id DESC LIMIT 1",
        (sitio_id,),
    ).fetchone()


def consultar_progreso(database, *, sitio_id=None, run_id=None):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if run_id is not None:
            run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return _snapshot(db, run) if run is not None else None
        sitio = db.execute("SELECT id FROM sites WHERE id = ?", (sitio_id,)).fetchone()
        return _snapshot(db, _ultima_alta(db, sitio_id)) if sitio is not None else None


def simular(database, sitio_id, accion):
    if accion not in ("avanzar", "fallar"):
        raise ValueError("Acción de simulación inválida.")
    with transaction(database) as db:
        sitio = db.execute(
            """SELECT s.estado, n.gestionado FROM sites s JOIN nodes n ON n.router = s.router
               WHERE s.id = ?""",
            (sitio_id,),
        ).fetchone()
        if sitio is None:
            return None
        run = _ultima_alta(db, sitio_id)
        if (not sitio["gestionado"] or sitio["estado"] != "aprovisionando"
                or (run is not None and (
                    not run["simulado"] or run["estado"] != "en_curso"
                    or db.execute("SELECT 1 FROM runner_jobs WHERE run_id = ?", (run["id"],)).fetchone()
                ))):
            raise ProgresoNoDisponible("La simulación no está disponible para esta ficha.")
        fecha = ahora_utc().isoformat()
        if run is None:
            run_id = crear_alta(db, sitio_id, simulado=True, fecha=fecha)
            aplicar_evento(db, run_id, 1, "en_curso", fecha=fecha)
        else:
            run_id = run["id"]
        paso = db.execute(
            "SELECT orden, nombre FROM run_steps WHERE run_id = ? AND estado = 'en_curso'",
            (run_id,),
        ).fetchone()
        orden = paso["orden"]
        fallo = accion == "fallar"
        resultado = "Falló la etapa" if fallo else "Etapa completada"
        mensaje = f"Simulación · {resultado}: {paso['nombre']}."
        aplicar_evento(db, run_id, orden, "error" if fallo else "ok", fecha=fecha, detalle=mensaje)
        db.execute(
            "INSERT INTO run_log (run_id, numero_linea, texto) VALUES (?, ?, ?)",
            (run_id, orden, mensaje),
        )
        if not fallo and orden < len(ETAPAS_ALTA):
            aplicar_evento(db, run_id, orden + 1, "en_curso", fecha=fecha)
        return _snapshot(db, _ultima_alta(db, sitio_id))
