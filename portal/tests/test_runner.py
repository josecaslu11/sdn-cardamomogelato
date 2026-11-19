from pathlib import Path
import sys
from threading import Event
from time import monotonic, sleep

import pytest

import runner
from ansible_files import AnsibleFiles
from db import transaction
from ipam import IPAM
from progress import consultar_progreso, crear_alta
from runner import AltaRunner, RunnerBusy, RunnerError, comando_alta, salida_proceso
from seed import seed


@pytest.fixture
def preparado(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "birthday_day: 11\nbirthday_month_day: '1111'\nquarantine_days: 7\n",
        encoding="utf-8",
    )
    ipam = IPAM(tmp_path / "ipam.sqlite3", config)
    seed(ipam)
    settings = {"PORTAL_DRY_RUN_DIR": str(tmp_path / "dev")}
    files = AnsibleFiles(config, settings.get)
    sites = [
        files.reserve(ipam, {"nombre": nombre, "codigo": codigo}, "prueba")
        for nombre, codigo in (("Guate", "GUA"), ("Jutiapa", "JUT"))
    ]
    return AltaRunner(ipam, files), sites


def esperar(database, run_id):
    limite = monotonic() + 5
    while monotonic() < limite:
        snapshot = consultar_progreso(database, run_id=run_id)
        if snapshot["estado"] != "en_curso":
            return snapshot
        sleep(0.01)
    pytest.fail("La ejecución de prueba no terminó.")


def test_comando_separado_y_router_restringido():
    assert comando_alta(Path("/tmp/bin local/ansible-playbook"), "R10", "/tmp/clave vault") == [
        "/tmp/bin local/ansible-playbook", "playbooks/site_add.yml", "--limit", "R10",
        "--vault-password-file", "/tmp/clave vault",
    ]
    for invalid in ("R1", "R6", "R11", "R7,R8", "R7 --check"):
        with pytest.raises(RunnerError):
            comando_alta("ansible-playbook", invalid, "/tmp/vault")


@pytest.mark.parametrize("valor", ["", "yes", "2"])
def test_rechaza_dry_run_invalido(preparado, valor):
    alta, _ = preparado
    with pytest.raises(ValueError, match="PORTAL_DRY_RUN"):
        AnsibleFiles(alta.ipam.config_file, {"PORTAL_DRY_RUN": valor}.get)


@pytest.mark.parametrize(("resultado", "codigo", "hechos", "sitio_estado"), [
    ("exito", 0, 11, "activo"),
    ("fallo", 2, 4, "fallido"),
    ("inalcanzable", 4, 3, "fallido"),
])
def test_resultados_y_persistencia(preparado, monkeypatch, resultado, codigo, hechos, sitio_estado):
    alta, sites = preparado
    monkeypatch.setattr(runner, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "salida_proceso", lambda *args: pytest.fail("Subprocess en dry run"))
    run_id = alta.iniciar(sites[0]["id"], "operador", resultado)
    snapshot = esperar(alta.ipam.database, run_id)
    assert snapshot["simulado"] and snapshot["automatico"]
    assert snapshot["codigo_salida"] == codigo
    assert snapshot["hechos"] == hechos
    assert snapshot["estado_sitio"] == sitio_estado
    assert snapshot["salida"][-1].startswith("Simulación")
    if codigo in (0, 2):
        assert snapshot["resumen"]["failed_count"] == (1 if codigo == 2 else 0)
    with transaction(alta.ipam.database) as db:
        estados = db.execute("SELECT DISTINCT estado FROM allocations WHERE sitio_id = ?", (sites[0]["id"],))
        assert [row[0] for row in estados] == ["en_uso" if codigo == 0 else "reservado"]
        evento = db.execute(
            "SELECT usuario, codigo_salida FROM audit_log WHERE accion = 'resultado_aprovisionamiento'"
        ).fetchone()
        assert tuple(evento) == ("operador", codigo)


def test_progreso_no_completa_un_rol_al_iniciar_tareas_y_bloqueo_global(preparado, monkeypatch):
    alta, sites = preparado
    started = Event()
    finish = Event()

    def salida(router, resultado, consume, deadline):
        consume("TASK [ipsec : primera tarea]")
        consume("ok: [R7]")
        consume("TASK [ipsec : segunda tarea]")
        consume("fatal: [R7]: detalle sensible")
        started.set()
        if not finish.wait(4):
            return 2
        consume("R7 : ok=1 changed=0 unreachable=0 failed=1")
        return 2

    monkeypatch.setattr(runner, "salida_simulada", salida)
    run_id = alta.iniciar(sites[0]["id"], "operador", "fallo")
    try:
        assert started.wait(3)
        progreso = consultar_progreso(alta.ipam.database, run_id=run_id)
        assert progreso["pasos"][4]["estado"] == "en_curso"
        assert progreso["hechos"] == 4
        otro_worker = AltaRunner(alta.ipam, alta.files)
        otro_worker.recuperar()
        with pytest.raises(RunnerBusy):
            otro_worker.iniciar(sites[1]["id"], "operador", "exito")
    finally:
        finish.set()
    progreso = esperar(alta.ipam.database, run_id)
    assert progreso["pasos"][4]["estado"] == "error"
    assert "detalle sensible" not in str(progreso)


def test_timeout_termina_proceso_y_se_registra(preparado, tmp_path, monkeypatch):
    alta, sites = preparado
    fd = alta.adquirir()
    try:
        with pytest.raises((TimeoutError, runner.subprocess.TimeoutExpired)):
            salida_proceso([sys.executable, "-c", "import time; time.sleep(60)"],
                           tmp_path, fd, lambda _: None, monotonic() + 0.1)
    finally:
        runner.os.close(fd)

    def timeout(*args):
        raise TimeoutError

    monkeypatch.setattr(runner, "salida_simulada", timeout)
    run_id = alta.iniciar(sites[0]["id"], "operador", "exito")
    assert esperar(alta.ipam.database, run_id)["codigo_salida"] == 124


def test_router_fijo_y_recuperacion(preparado):
    alta, sites = preparado
    with pytest.raises(RunnerError):
        alta.iniciar(1, "operador", "exito")
    with transaction(alta.ipam.database) as db:
        run_id = crear_alta(db, sites[0]["id"], simulado=True, fecha="2026-01-01T00:00:00+00:00")
        db.execute("INSERT INTO runner_jobs VALUES (?, ?)", (run_id, "operador"))
    AltaRunner(alta.ipam, alta.files).recuperar()
    progreso = consultar_progreso(alta.ipam.database, run_id=run_id)
    assert progreso["codigo_salida"] == 125
    assert progreso["estado_sitio"] == "fallido"


def test_preflight_real_usa_vault_y_omite_playbook_si_credencial_falla(
    preparado, tmp_path, monkeypatch,
):
    alta, sites = preparado
    project = tmp_path / "ansible"
    inventory = project / "inventory"
    (inventory / "group_vars/all").mkdir(parents=True)
    (inventory / "host_vars").mkdir()
    (project / "playbooks").mkdir()
    for path in (
        project / "ansible.cfg", project / "playbooks/site_add.yml",
        inventory / "hosts.yml", inventory / "zabbix.yml",
    ):
        path.touch()
    for name in ("sitios.yml", "host_vars/R7.yml"):
        (inventory / name).write_bytes((alta.files.directory / name).read_bytes())
    (inventory / "group_vars/all/vault.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n")
    for name in ("ansible-playbook", "ansible-vault"):
        path = project / name
        path.touch()
        path.chmod(0o700)
    vault = tmp_path / "vault-pass"
    vault.write_text("contenido de prueba\n")
    vault.chmod(0o600)

    real_ipam = IPAM(tmp_path / "produccion.sqlite3", alta.ipam.config_file)
    seed(real_ipam)
    sitio_real = real_ipam.reservar_sitio("Guate", "GUA")
    settings = {"PORTAL_DRY_RUN": "0", "PORTAL_ANSIBLE_DIR": str(project),
                "PORTAL_ANSIBLE_PLAYBOOK_BIN": "ansible-playbook",
                "PORTAL_VAULT_PASSWORD_FILE": str(vault)}
    with pytest.raises(RunnerError, match="Day-0"):
        AltaRunner(real_ipam, AnsibleFiles(real_ipam.config_file, settings.get)).iniciar(
            sitio_real["id"], "operador", "exito"
        )
    settings["PORTAL_DAY0_READY_ROUTERS"] = "R7"
    calls = []

    def transporte(command, cwd, fd, consume, deadline):
        calls.append((command, cwd, consume))
        return 1

    monkeypatch.setattr(runner, "salida_proceso", transporte)
    monkeypatch.setattr(runner.socket, "create_connection", lambda *args, **kwargs: pytest.fail("Conexión real"))
    execution = AltaRunner(real_ipam, AnsibleFiles(real_ipam.config_file, settings.get))
    run_id = execution.iniciar(sitio_real["id"], "operador", "exito")
    assert esperar(real_ipam.database, run_id)["codigo_salida"] == 126
    assert len(calls) == 1
    assert Path(calls[0][0][0]).name == "ansible-vault"
    assert calls[0][1:] == (project, None)


def iniciar_y_esperar(alta, site, resultado="exito", **kwargs):
    from fcntl import LOCK_EX, flock

    run_id = alta.iniciar(site["id"], "operador", resultado, **kwargs)
    progreso = esperar(alta.ipam.database, run_id)
    # Esperar a que el worker suelte el bloqueo que aún puede tener al confirmar SQLite.
    with alta.lock_path.open("rb") as lock:
        flock(lock, LOCK_EX)
    return progreso


@pytest.mark.parametrize("resultado,codigo,hechos", [("fallo", 2, 1), ("inalcanzable", 4, 1)])
def test_baja_fallida_conserva_inventario_y_permite_reintento(
    preparado, monkeypatch, resultado, codigo, hechos
):
    alta, sites = preparado
    monkeypatch.setattr(runner, "sleep", lambda _: None)
    site = sites[0]
    iniciar_y_esperar(alta, site)
    archivos = {p: p.read_bytes() for p in alta.files.directory.rglob("*.yml")}
    with pytest.raises(RunnerError, match="código exacto"):
        alta.iniciar(site["id"], "operador", "exito", tipo="baja", confirmacion="gua")
    progreso = iniciar_y_esperar(alta, site, resultado, tipo="baja", confirmacion="GUA")
    assert (progreso["codigo_salida"], progreso["hechos"], progreso["total"]) == (codigo, hechos, 10)
    assert progreso["estado_sitio"] == "fallido"
    assert {p: p.read_bytes() for p in alta.files.directory.rglob("*.yml")} == archivos
    with transaction(alta.ipam.database) as db:
        assert db.execute("SELECT DISTINCT estado FROM allocations WHERE sitio_id = ?", (site["id"],)).fetchone()[0] == "en_uso"
    with pytest.raises(RunnerError, match="operación original"):
        alta.iniciar(site["id"], "operador", "exito")
    reintento = iniciar_y_esperar(alta, site, tipo="baja", confirmacion="GUA")
    assert reintento["id"] != progreso["id"]
    assert reintento["estado_sitio"] == "retirado"


def test_baja_exitosa_cuarentena_e_inventario(preparado, monkeypatch):
    import yaml

    alta, sites = preparado
    monkeypatch.setattr(runner, "sleep", lambda _: None)
    site = sites[0]
    iniciar_y_esperar(alta, site)
    progreso = iniciar_y_esperar(alta, site, tipo="baja", confirmacion="GUA")
    assert (progreso["porcentaje"], progreso["tipo"], progreso["estado_sitio"]) == (100, "baja", "retirado")
    assert not (alta.files.directory / "host_vars/R7.yml").exists()
    assert (alta.files.directory / "host_vars/R8.yml").is_file()
    contenido = yaml.safe_load((alta.files.directory / "sitios.yml").read_text())
    assert contenido["all"]["children"]["spokes"]["hosts"] == {"R8": None}
    with transaction(alta.ipam.database) as db:
        fechas = db.execute("SELECT retirado_en, cuarentena_hasta FROM sites WHERE id = ?", (site["id"],)).fetchone()
        assert fechas[0] < fechas[1]
        assert db.execute("SELECT DISTINCT estado FROM allocations WHERE sitio_id = ?", (site["id"],)).fetchone()[0] == "cuarentena"
    nuevo = alta.files.reserve(alta.ipam, {"nombre": "Peten", "codigo": "PET"}, "operador")
    assert nuevo["router"] == "R9"


def test_baja_remota_exitosa_reintenta_solo_cierre_local(preparado, monkeypatch):
    alta, sites = preparado
    monkeypatch.setattr(runner, "sleep", lambda _: None)
    site = sites[0]
    iniciar_y_esperar(alta, site)
    archivos = {p: p.read_bytes() for p in alta.files.directory.rglob("*.yml")}
    unlink_original = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path.name == "R7.yml":
            raise OSError("Fallo de publicación")
        return unlink_original(path, *args, **kwargs)

    with monkeypatch.context() as parche:
        parche.setattr(Path, "unlink", fail_unlink)
        fallo = iniciar_y_esperar(alta, site, tipo="baja", confirmacion="GUA")
    assert fallo["codigo_salida"] == -127
    assert fallo["estado_sitio"] == "fallido"
    assert {p: p.read_bytes() for p in alta.files.directory.rglob("*.yml")} == archivos
    with transaction(alta.ipam.database) as db:
        assert db.execute("SELECT run_id FROM bajas_pendientes").fetchone()[0] == fallo["id"]
    monkeypatch.setattr(runner, "baja_simulada", lambda *args: pytest.fail("Se repitió la baja remota"))
    recuperada = iniciar_y_esperar(alta, site, tipo="baja", confirmacion="GUA")
    assert (recuperada["codigo_salida"], recuperada["estado_sitio"]) == (0, "retirado")
    with transaction(alta.ipam.database) as db:
        assert not db.execute("SELECT 1 FROM bajas_pendientes").fetchone()


def test_reintento_alta_e_inventario_aislado_bidireccional(preparado, monkeypatch, tmp_path):
    from ansible_files import InventoryError

    alta, sites = preparado
    monkeypatch.setattr(runner, "sleep", lambda _: None)
    site = sites[0]
    fallo = iniciar_y_esperar(alta, site, "fallo")
    with pytest.raises(RunnerError, match="operación original"):
        alta.iniciar(site["id"], "operador", "exito", tipo="baja", confirmacion="GUA")
    assert iniciar_y_esperar(alta, site)["id"] != fallo["id"]
    real_project = tmp_path / "real"
    real = AnsibleFiles(alta.ipam.config_file, {
        "PORTAL_DRY_RUN": "0", "PORTAL_ANSIBLE_DIR": str(real_project)
    }.get)
    with pytest.raises(InventoryError, match="bases independientes"):
        AltaRunner(alta.ipam, real).iniciar(site["id"], "operador", "exito", tipo="baja", confirmacion="GUA")
    assert not real_project.exists()
    base_real = IPAM(tmp_path / "real.sqlite3", alta.ipam.config_file)
    seed(base_real)
    with transaction(base_real.database) as db:
        real.ensure_mode(db)
    with pytest.raises(InventoryError, match="bases independientes"):
        alta.files.reserve(base_real, {"nombre": "Peten", "codigo": "PET"}, "operador")


def test_baja_protege_nodos_fijos_y_comparte_bloqueo(preparado, monkeypatch):
    alta, sites = preparado
    monkeypatch.setattr(runner, "sleep", lambda _: None)
    assert runner.comando_operacion("/bin/ansible", "R7", "/vault", "baja")[1:4] == [
        "playbooks/site_remove.yml", "--limit", "R7",
    ]
    for site_id in range(1, 5):
        with pytest.raises(RunnerError):
            alta.iniciar(site_id, "operador", "exito", tipo="baja", confirmacion="R3")
    iniciar_y_esperar(alta, sites[0])
    iniciado = Event()
    terminar = Event()

    def salida(*args):
        iniciado.set()
        terminar.wait(4)
        return 2

    monkeypatch.setattr(runner, "baja_simulada", salida)
    run_id = alta.iniciar(sites[0]["id"], "operador", "exito", tipo="baja", confirmacion="GUA")
    try:
        assert iniciado.wait(3)
        with pytest.raises(RunnerBusy):
            AltaRunner(alta.ipam, alta.files).iniciar(sites[1]["id"], "operador", "exito")
        with pytest.raises(RunnerBusy):
            alta.iniciar(sites[0]["id"], "operador", "exito", tipo="baja", confirmacion="GUA")
    finally:
        terminar.set()
    assert esperar(alta.ipam.database, run_id)["codigo_salida"] == 2


def test_baja_con_fallo_sqlite_conserva_recibo_y_archivos(preparado, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    alta, sites = preparado
    monkeypatch.setattr(runner, "sleep", lambda _: None)
    site = sites[0]
    iniciar_y_esperar(alta, site)
    archivos = {p: p.read_bytes() for p in alta.files.directory.rglob("*.yml")}
    publicar = alta.files.publish

    @contextmanager
    def fallo_al_publicar(files):
        with publicar(files):
            if any(content is None for content in files.values()):
                raise sqlite3.OperationalError("Fallo al confirmar SQLite")
            yield

    with monkeypatch.context() as patch:
        patch.setattr(alta.files, "publish", fallo_al_publicar)
        fallo = iniciar_y_esperar(alta, site, tipo="baja", confirmacion="GUA")
    assert fallo["codigo_salida"] == -127
    assert {p: p.read_bytes() for p in alta.files.directory.rglob("*.yml")} == archivos
    with transaction(alta.ipam.database) as db:
        row = db.execute("SELECT estado, cuarentena_hasta FROM sites WHERE id = ?", (site["id"],)).fetchone()
        assert tuple(row) == ("fallido", None)
        assert db.execute("SELECT 1 FROM bajas_pendientes WHERE sitio_id = ?", (site["id"],)).fetchone()
