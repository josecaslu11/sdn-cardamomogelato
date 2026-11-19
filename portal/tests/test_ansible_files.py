
import pytest
import yaml

import ansible_files
from ansible_files import AnsibleFiles, InventoryError, dynamic_inventory, host_vars
from db import transaction
from ipam import IPAM
from seed import seed


@pytest.fixture
def entorno(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "birthday_day: 11\nbirthday_month_day: '1111'\nquarantine_days: 7\n",
        encoding="utf-8",
    )
    ipam = IPAM(tmp_path / "ipam.sqlite3", config)
    seed(ipam)
    settings = {
        "PORTAL_DRY_RUN": "1",
        "PORTAL_DRY_RUN_DIR": tmp_path / "dev",
        "PORTAL_ANSIBLE_DIR": tmp_path / "real",
    }
    return ipam, settings


def reservar(ipam, files, codigo):
    return files.reserve(ipam, {"nombre": "Escuintla", "codigo": codigo}, "prueba")


def hosts(text):
    return yaml.safe_load(text)["all"]["children"]["spokes"]["hosts"]


def test_host_vars_con_tres_variables():
    site = {"router": "R10", "site_id": 10, "site_name": "null", "lan_if": "Ethernet0/1"}
    assert yaml.safe_load(host_vars(site)) == {
        "site_id": 10, "site_name": "null", "lan_if": "Ethernet0/1"
    }


@pytest.mark.parametrize("cambios", [
    {"router": "../R7"}, {"router": "R8"}, {"site_id": "7"},
    {"router": "R1", "site_id": 1}, {"site_name": "Sitio: inválido"},
    {"lan_if": "Ethernet0/1\npassword"},
])
def test_rechaza_datos_invalidos(cambios):
    site = {"router": "R7", "site_id": 7, "site_name": "Escuintla", "lan_if": "Ethernet0/1"}
    with pytest.raises(ValueError):
        host_vars({**site, **cambios})


def test_inventario_vacio_y_sin_duplicados():
    assert "hosts: {}" in dynamic_inventory([])
    assert hosts(dynamic_inventory([])) == {}
    r7 = {"router": "R7", "site_id": 7}
    inventario = dynamic_inventory([r7, r7, {"router": "R8", "site_id": 8}])
    assert inventario.count("R7:") == 1
    assert hosts(inventario) == {"R7": None, "R8": None}


def test_dry_run_y_reconstruccion(entorno):
    ipam, settings = entorno
    files = AnsibleFiles(ipam.config_file, settings.get)
    real = files.real_inventory
    real.mkdir(parents=True)
    (real / "sitios.yml").write_text("original\n", encoding="utf-8")

    reservar(ipam, files, "ESC1")
    ipam.finalizar_alta(7, 0)
    ipam.retirar_sitio(7)
    ipam.finalizar_baja(7, 4)
    reservar(ipam, files, "ESC2")
    ipam.finalizar_alta(8, 4)
    reservar(ipam, files, "ESC3")
    assert hosts((files.directory / "sitios.yml").read_text()) == {
        "R7": None, "R8": None, "R9": None
    }
    assert set(path.name for path in (files.directory / "host_vars").iterdir()) == {
        "R7.yml", "R8.yml", "R9.yml"
    }
    assert yaml.safe_load((files.directory / "host_vars/R7.yml").read_text()) == {
        "site_id": 7, "site_name": "Escuintla", "lan_if": "Ethernet0/1"
    }
    assert list(real.iterdir()) == [real / "sitios.yml"]
    assert (real / "sitios.yml").read_text() == "original\n"


def test_dry_run_rechaza_ruta_real(entorno):
    ipam, settings = entorno
    settings["PORTAL_DRY_RUN_DIR"] = settings["PORTAL_ANSIBLE_DIR"]
    files = AnsibleFiles(ipam.config_file, settings.get)
    with pytest.raises(InventoryError, match="inventario real"):
        reservar(ipam, files, "ESC")
    assert not files.directory.exists()


def test_fallo_parcial_restaura_archivos_y_revierte_reserva(entorno, monkeypatch):
    ipam, settings = entorno
    files = AnsibleFiles(ipam.config_file, settings.get)
    reservar(ipam, files, "ESC1")
    previous = {
        path: path.read_bytes() for path in files.directory.rglob("*.yml")
    }
    original_replace = ansible_files.os.replace

    def fail_inventory(source, target):
        if target.name == "sitios.yml":
            raise OSError("Fallo simulado")
        original_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(ansible_files.os, "replace", fail_inventory)
        with pytest.raises(OSError):
            reservar(ipam, files, "ESC2")

    assert {path: path.read_bytes() for path in files.directory.rglob("*.yml")} == previous
    assert set(path for path in files.directory.rglob("*") if path.is_file()) == set(previous)
    with transaction(ipam.database) as db:
        assert not db.execute("SELECT 1 FROM sites WHERE codigo = 'ESC2'").fetchone()
        assert not db.execute("SELECT 1 FROM allocations WHERE site_id = 8").fetchone()
        assert not db.execute("SELECT 1 FROM audit_log WHERE router = 'R8'").fetchone()
    assert reservar(ipam, files, "ESC2")["router"] == "R8"
