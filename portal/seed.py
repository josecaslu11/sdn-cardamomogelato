"""Inicializa un inventario de ejemplo local."""
from datetime import timezone

from db import initialize, transaction
from ipam import IPAM, guardar_asignaciones, registrar


SUCURSALES = {
    3: ("Mazate", "Ethernet0/2"),
    4: ("Suchi", "Ethernet0/1"),
    5: ("Zacapa", "Ethernet0/1"),
    6: ("Reu", "Ethernet0/2"),
}


def seed(ipam):
    initialize(ipam.database)
    with transaction(ipam.database) as db:
        fecha = ipam.clock().astimezone(timezone.utc).isoformat()
        for numero in range(1, 11):
            router = f"R{numero}"
            gestionado = numero >= 7
            direcciones = ipam.calcular_direcciones(numero)
            nombre, interfaz = SUCURSALES.get(
                numero, (None, "Ethernet0/1" if gestionado else None)
            )
            estado = "disponible" if gestionado else "activo"
            nuevo = db.execute(
                """INSERT INTO nodes (router, site_id, underlay_ip, lan_if, gestionado, estado)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(router) DO NOTHING RETURNING router""",
                (router, numero, direcciones["underlay"], interfaz, gestionado, estado),
            ).fetchone()
            if nuevo is None or nombre is None:
                continue
            sitio = db.execute(
                """INSERT INTO sites (site_id, router, site_name, codigo, lan_if, estado, creado_en)
                   VALUES (?, ?, ?, ?, ?, 'activo', ?) RETURNING *""",
                (numero, router, nombre, router, interfaz, fecha),
            ).fetchone()
            guardar_asignaciones(db, sitio, direcciones, "en_uso", fecha)
            registrar(db, fecha, "seed", "registrar_existente", router, "Sucursal fija de ejemplo.")


if __name__ == "__main__":
    ipam = IPAM()
    seed(ipam)
    print(f"Base de ejemplo inicializada en {ipam.database}")
