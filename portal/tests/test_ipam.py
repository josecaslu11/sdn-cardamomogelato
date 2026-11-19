from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Barrier

import pytest

from db import transaction
from ipam import IPAM, direcciones
from seed import seed


@pytest.fixture
def ipam(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "birthday_day: 11\nbirthday_month_day: '1111'\nquarantine_days: 2\n",
        encoding="utf-8",
    )
    ipam = IPAM(tmp_path / "test.sqlite3", config)
    seed(ipam)
    return ipam


def consultar(ipam, sql, parametros=()):
    with transaction(ipam.database) as db:
        return [tuple(row) for row in db.execute(sql, parametros)]


@pytest.mark.parametrize(("n", "octeto"), [(7, 18), (8, 19), (9, 20), (10, 21)])
def test_direcciones(ipam, n, octeto):
    assert ipam.calcular_direcciones(n) == {
        "underlay": f"172.16.0.{n}",
        "loopback": f"10.254.0.{n}/32",
        "lan_v4": f"10.{octeto}.0.0/24",
        "gateway_v4": f"10.{octeto}.0.1",
        "lan_v6": f"2001:1111:{n}::/64",
        "gateway_v6": f"2001:1111:{n}::1",
        "tunnel0_v4": f"10.255.1.{n}",
        "tunnel1_v4": f"10.255.2.{n}",
        "tunnel0_v6": f"2001:1111:FF01::{n}",
        "tunnel1_v6": f"2001:1111:FF02::{n}",
        "link_local": f"FE80::{n}",
    }


@pytest.mark.parametrize("numero", [0, 243, "7", True])
def test_numero_invalido(numero):
    with pytest.raises(ValueError):
        direcciones(numero)


def test_octeto_fuera_de_rango():
    with pytest.raises(ValueError):
        direcciones(242, birthday_day=14)


def test_asignacion_consecutiva_y_agotamiento(ipam):
    routers = [ipam.reservar_sitio("Sucursal", f"C{i}")["router"] for i in range(4)]
    assert routers == ["R7", "R8", "R9", "R10"]
    with pytest.raises(ValueError, match="No hay routers"):
        ipam.reservar_sitio("Sucursal", "C4")


@pytest.mark.parametrize(("nombre", "codigo", "lan_if"), [
    ("A", "C1", None), ("A" * 21, "C1", None), ("Xx\n", "C1", None),
    ("Guate Sur", "C1", None), ("Guate", "; rm -rf", None),
    ("Guate", "", None), ("Guate", "C1", "Ethernet0/2"),
])
def test_validacion(ipam, nombre, codigo, lan_if):
    with pytest.raises(ValueError):
        ipam.reservar_sitio(nombre, codigo, lan_if)
    assert consultar(ipam, "SELECT router FROM nodes WHERE estado = 'disponible' ORDER BY site_id") == [
        ("R7",), ("R8",), ("R9",), ("R10",)
    ]


def test_codigo_duplicado(ipam):
    ipam.reservar_sitio("Guate", "GUA")
    with pytest.raises(ValueError, match="ya existe"):
        ipam.reservar_sitio("Nueva", "gua")
    assert ipam.reservar_sitio("Nueva", "NUE")["router"] == "R8"


@pytest.mark.parametrize("mismo_codigo", [False, True])
def test_reservas_concurrentes(ipam, mismo_codigo):
    inicio = Barrier(2)

    def reservar(i):
        inicio.wait(timeout=5)
        try:
            return ipam.reservar_sitio("Sucursal", "C1" if mismo_codigo else f"C{i}")["router"]
        except ValueError as error:
            assert "ya existe" in str(error)
            return "duplicado"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert set(executor.map(reservar, (1, 2))) == {
            "R7", "duplicado" if mismo_codigo else "R8"
        }


@pytest.mark.parametrize("numero", range(1, 7))
def test_routers_protegidos(ipam, numero):
    with pytest.raises(ValueError, match="pool administrable"):
        ipam.retirar_sitio(numero)
    assert consultar(ipam, "SELECT gestionado, estado FROM nodes WHERE site_id = ?", (numero,)) == [
        (0, "activo")
    ]


def test_transiciones_y_bitacora(ipam):
    ipam.reservar_sitio("Guate", "GUA")
    with pytest.raises(ValueError, match="Transición"):
        ipam.retirar_sitio(7)
    ipam.finalizar_alta(7, 4)
    assert consultar(ipam, "SELECT estado FROM sites WHERE site_id = 7") == [("fallido",)]
    ipam.reintentar_alta(7)
    ipam.finalizar_alta(7, 0)
    assert consultar(ipam, "SELECT DISTINCT estado FROM allocations WHERE site_id = 7") == [("en_uso",)]
    ipam.retirar_sitio(7)
    ipam.finalizar_baja(7, 0)
    assert consultar(ipam, "SELECT estado FROM nodes WHERE site_id = 7") == [("retirado",)]
    assert consultar(ipam, "SELECT accion, codigo_salida FROM audit_log WHERE router = 'R7' ORDER BY id") == [
        ("reservar", None), ("finalizar_alta", 4), ("reintentar_alta", None),
        ("finalizar_alta", 0), ("iniciar_baja", None), ("finalizar_baja", 0),
    ]


def test_cuarentena_y_reutilizacion(ipam):
    inicio = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ipam.clock = lambda: inicio
    primer_sitio = ipam.reservar_sitio("Guate", "GUA")
    ipam.finalizar_alta(7, 0)
    ipam.retirar_sitio(7)
    ipam.finalizar_baja(7, 0)

    ipam.clock = lambda: inicio + timedelta(days=2, microseconds=-1)
    assert ipam.liberar_vencidos() == 0
    assert consultar(ipam, "SELECT DISTINCT estado FROM allocations WHERE site_id = 7") == [("cuarentena",)]
    assert ipam.reservar_sitio("Jutiapa", "JUT")["router"] == "R8"

    ipam.clock = lambda: inicio + timedelta(days=2)
    nuevo_sitio = ipam.reservar_sitio("Nueva", "NUE")
    assert nuevo_sitio["router"] == "R7"
    assert nuevo_sitio["id"] != primer_sitio["id"]
    assert consultar(ipam, "SELECT DISTINCT estado FROM allocations WHERE site_id = 7 ORDER BY estado") == [
        ("liberado",), ("reservado",)
    ]


def test_baja_fallida_conserva_recursos(ipam):
    ipam.reservar_sitio("Guate", "GUA")
    ipam.finalizar_alta(7, 0)
    ipam.retirar_sitio(7)
    ipam.finalizar_baja(7, 2)
    assert consultar(ipam, "SELECT estado, cuarentena_hasta FROM sites WHERE site_id = 7") == [
        ("fallido", None)
    ]
    assert consultar(ipam, "SELECT DISTINCT estado FROM allocations WHERE site_id = 7") == [("en_uso",)]
    assert ipam.reservar_sitio("Jutiapa", "JUT")["router"] == "R8"
    ipam.retirar_sitio(7)
    ipam.finalizar_baja(7, 0)


def test_seed_idempotente(ipam):
    sitio = ipam.reservar_sitio("Guate", "GUA")
    ipam.finalizar_alta(7, 0)
    tablas = ("nodes", "sites", "allocations", "audit_log")
    antes = {tabla: consultar(ipam, f"SELECT * FROM {tabla}") for tabla in tablas}
    seed(ipam)
    assert antes == {tabla: consultar(ipam, f"SELECT * FROM {tabla}") for tabla in tablas}
    assert sitio["router"] == "R7"


def test_colision_de_direccion_revierte_la_reserva(ipam, monkeypatch):
    calcular = ipam.calcular_direcciones

    def direcciones_en_conflicto(site_id):
        direcciones_sitio = calcular(site_id)
        direcciones_sitio["lan_v4"] = calcular(3)["lan_v4"]
        return direcciones_sitio

    monkeypatch.setattr(ipam, "calcular_direcciones", direcciones_en_conflicto)
    with pytest.raises(sqlite3.IntegrityError):
        ipam.reservar_sitio("Guate", "GUA")
    assert consultar(ipam, "SELECT estado FROM nodes WHERE router = 'R7'") == [("disponible",)]
    assert consultar(ipam, "SELECT id FROM sites WHERE router = 'R7'") == []
    assert consultar(ipam, "SELECT id FROM allocations WHERE site_id = 7") == []
    assert consultar(ipam, "SELECT id FROM audit_log WHERE router = 'R7'") == []
