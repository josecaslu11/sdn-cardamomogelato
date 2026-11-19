import os
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from db import transaction


ROOT = Path(__file__).resolve().parent
SITE_NAME = re.compile(r"[A-Za-z0-9-]{2,20}")
SITE_CODE = re.compile(r"[A-Za-z0-9-]+")


def direcciones(site_id, birthday_day=11, birthday_month_day="1111"):
    if type(site_id) is not int or not 1 <= site_id <= 242:
        raise ValueError("El número de sitio debe estar entre 1 y 242.")
    octeto = birthday_day + site_id
    if not 1 <= octeto <= 255:
        raise ValueError("El segundo octeto IPv4 queda fuera de rango.")
    return {
        "underlay": f"172.16.0.{site_id}",
        "loopback": f"10.254.0.{site_id}/32",
        "lan_v4": f"10.{octeto}.0.0/24",
        "gateway_v4": f"10.{octeto}.0.1",
        "lan_v6": f"2001:{birthday_month_day}:{site_id}::/64",
        "gateway_v6": f"2001:{birthday_month_day}:{site_id}::1",
        "tunnel0_v4": f"10.255.1.{site_id}",
        "tunnel1_v4": f"10.255.2.{site_id}",
        "tunnel0_v6": f"2001:{birthday_month_day}:FF01::{site_id}",
        "tunnel1_v6": f"2001:{birthday_month_day}:FF02::{site_id}",
        "link_local": f"FE80::{site_id}",
    }


def guardar_asignaciones(db, sitio, direcciones_sitio, estado, fecha):
    db.executemany(
        """INSERT INTO allocations (site_id, sitio_id, recurso, valor, estado, fecha_cambio)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ((sitio["site_id"], sitio["id"], recurso, valor, estado, fecha)
         for recurso, valor in direcciones_sitio.items()),
    )


def registrar(db, fecha, usuario, accion, router, resumen, codigo=None):
    db.execute(
        """INSERT INTO audit_log (fecha, usuario, accion, router, codigo_salida, resumen)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fecha, usuario, accion, router, codigo, resumen),
    )


class IPAM:
    def __init__(self, database=None, config_file=None, *, clock=None):
        self.database = database if database is not None else os.getenv(
            "PORTAL_DB_PATH", ROOT / "portal.sqlite3"
        )
        self.config_file = Path(config_file or os.getenv("PORTAL_CONFIG_PATH", ROOT / "config.yml"))
        with self.config_file.open(encoding="utf-8") as config:
            settings = yaml.safe_load(config)
        self.day = settings["birthday_day"]
        self.month_day = str(settings["birthday_month_day"])
        self.quarantine_days = settings["quarantine_days"]
        if type(self.day) is not int or not 1 <= self.day <= 31:
            raise ValueError("birthday_day debe ser un entero entre 1 y 31.")
        if not re.fullmatch(r"[0-9]{4}", self.month_day):
            raise ValueError("birthday_month_day debe contener cuatro dígitos.")
        if type(self.quarantine_days) is not int or self.quarantine_days < 0:
            raise ValueError("quarantine_days debe ser un entero no negativo.")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def calcular_direcciones(self, site_id):
        return direcciones(site_id, self.day, self.month_day)

    def _ahora(self):
        return self.clock().astimezone(timezone.utc)

    def _liberar_vencidos(self, db, fecha, usuario):
        sitios = db.execute(
            """UPDATE sites SET liberado_en = ?
               WHERE estado = 'retirado' AND liberado_en IS NULL AND cuarentena_hasta <= ?
               RETURNING id, router""",
            (fecha, fecha),
        ).fetchall()
        for sitio in sitios:
            self._estado_asignaciones(db, sitio["id"], "liberado", fecha)
            db.execute("UPDATE nodes SET estado = 'disponible' WHERE router = ?", (sitio["router"],))
            registrar(db, fecha, usuario, "liberar_cuarentena", sitio["router"], "Cuarentena vencida.")
        return len(sitios)

    def liberar_vencidos(self, *, usuario="sistema"):
        with transaction(self.database) as db:
            return self._liberar_vencidos(db, self._ahora().isoformat(), usuario)

    def reservar_sitio(self, nombre, codigo, lan_if=None, *, usuario="sistema", conexion=None):
        if not isinstance(nombre, str) or not SITE_NAME.fullmatch(nombre):
            raise ValueError("El nombre debe tener entre 2 y 20 letras, números o guiones.")
        if not isinstance(codigo, str) or not SITE_CODE.fullmatch(codigo):
            raise ValueError("El código solo admite letras, números y guiones.")
        reserva = transaction(self.database) if conexion is None else nullcontext(conexion)
        with reserva as db:
            fecha = self._ahora().isoformat()
            if db.execute("SELECT 1 FROM sites WHERE codigo = ?", (codigo,)).fetchone():
                raise ValueError("El código ya existe.")
            self._liberar_vencidos(db, fecha, usuario)
            nodo = db.execute(
                """SELECT * FROM nodes WHERE gestionado = 1 AND estado = 'disponible'
                   ORDER BY site_id LIMIT 1"""
            ).fetchone()
            if nodo is None:
                raise ValueError("No hay routers disponibles.")
            if lan_if is not None and lan_if != nodo["lan_if"]:
                raise ValueError("La interfaz LAN debe coincidir con la del router.")
            sitio = db.execute(
                """INSERT INTO sites (site_id, router, site_name, codigo, lan_if, estado, creado_en)
                   VALUES (?, ?, ?, ?, ?, 'aprovisionando', ?) RETURNING *""",
                (nodo["site_id"], nodo["router"], nombre, codigo, nodo["lan_if"], fecha),
            ).fetchone()
            guardar_asignaciones(
                db, sitio, self.calcular_direcciones(sitio["site_id"]), "reservado", fecha
            )
            db.execute("UPDATE nodes SET estado = 'aprovisionando' WHERE router = ?", (sitio["router"],))
            registrar(db, fecha, usuario, "reservar", sitio["router"], "Recursos reservados.")
            return dict(sitio)

    @staticmethod
    def _estado_asignaciones(db, sitio_id, estado, fecha):
        db.execute(
            "UPDATE allocations SET estado = ?, fecha_cambio = ? WHERE sitio_id = ?",
            (estado, fecha, sitio_id),
        )

    def reintentar_alta(self, site_id, *, usuario="sistema", conexion=None):
        self._transicion(site_id, "reintentar_alta", usuario, conexion=conexion)

    def finalizar_alta(self, site_id, codigo_salida, *, usuario="sistema", conexion=None):
        self._transicion(site_id, "finalizar_alta", usuario, codigo_salida, conexion=conexion)

    def retirar_sitio(self, site_id, *, usuario="sistema"):
        self._transicion(site_id, "iniciar_baja", usuario)

    def finalizar_baja(self, site_id, codigo_salida, *, usuario="sistema"):
        self._transicion(site_id, "finalizar_baja", usuario, codigo_salida)

    def _transicion(self, site_id, accion, usuario, codigo=None, *, conexion=None):
        if type(site_id) is not int or not 1 <= site_id <= 242:
            raise ValueError("Número de sitio inválido.")
        if accion.startswith("finalizar") and type(codigo) is not int:
            raise ValueError("Se requiere un código de salida entero.")
        operacion = transaction(self.database) if conexion is None else nullcontext(conexion)
        with operacion as db:
            nodo = db.execute("SELECT * FROM nodes WHERE site_id = ?", (site_id,)).fetchone()
            if nodo is None or not nodo["gestionado"]:
                raise ValueError("El router no pertenece al pool administrable.")
            sitio = db.execute(
                "SELECT * FROM sites WHERE site_id = ? AND liberado_en IS NULL", (site_id,)
            ).fetchone()
            transiciones = {
                "reintentar_alta": (("fallido",), "aprovisionando"),
                "finalizar_alta": (("aprovisionando",), "activo" if codigo == 0 else "fallido"),
                "iniciar_baja": (("activo", "fallido"), "retirando"),
                "finalizar_baja": (("retirando",), "retirado" if codigo == 0 else "fallido"),
            }
            origenes, destino = transiciones[accion]
            if sitio is None or sitio["estado"] not in origenes:
                raise ValueError("Transición de estado inválida.")
            ahora = self._ahora()
            fecha = ahora.isoformat()
            db.execute("UPDATE sites SET estado = ? WHERE id = ?", (destino, sitio["id"]))
            db.execute("UPDATE nodes SET estado = ? WHERE router = ?", (destino, sitio["router"]))
            if destino == "activo":
                self._estado_asignaciones(db, sitio["id"], "en_uso", fecha)
            if destino == "retirado":
                hasta = (ahora + timedelta(days=self.quarantine_days)).isoformat()
                db.execute(
                    "UPDATE sites SET retirado_en = ?, cuarentena_hasta = ? WHERE id = ?",
                    (fecha, hasta, sitio["id"]),
                )
                self._estado_asignaciones(db, sitio["id"], "cuarentena", fecha)
            registrar(db, fecha, usuario, accion, sitio["router"], f"Estado: {destino}.", codigo)
