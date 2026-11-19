"""Ejecución de altas y seguimiento de eventos públicos de Ansible."""
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
from ipaddress import IPv4Address
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import sqlite3
from stat import S_IMODE
import subprocess
from threading import Thread
from time import monotonic, sleep

import yaml

from ansible_files import InventoryError, host_vars, valid_router
from db import transaction
from ipam import direcciones, registrar
from progress import ETAPAS, ETAPAS_ALTA, ahora_utc, aplicar_evento, crear_run


ROUTERS = frozenset(("R7", "R8", "R9", "R10"))
ROLES = {quien: orden for orden, (_, quien) in enumerate(ETAPAS_ALTA, 1) if quien != "portal"}
TAREAS_BAJA = {
    "Proteccion (solo spokes y siempre con --limit)": 2,
    "Estado actual del router": 2,
    "Eliminar tuneles": 3,
    "Eliminar OSPFv2 y OSPFv3": 3,
    "Limpiar la interfaz LAN": 4,
    "Eliminar objetos IKEv2 e IPsec": 5,
    "Limpiar NHRP del sitio en el hub 1": 6,
    "Limpiar NHRP del sitio en el hub 2": 6,
    "Apagar la interfaz LAN del sitio retirado": 6,
    "Quitar usuario SNMPv3 del sitio retirado": 7,
    "Quitar grupo, vista y ACL de SNMP": 7,
    "Eliminar el host de Zabbix": 8,
}
PLAYBOOKS = {"alta": "playbooks/site_add.yml", "baja": "playbooks/site_remove.yml"}
RESULTADOS = {
    0: "Alta completada.",
    2: "Falló una tarea de Ansible.",
    4: "El router no está accesible.",
    124: "Se agotó el tiempo límite de aprovisionamiento.",
    125: "La ejecución se interrumpió.",
    126: "No se pudo abrir el Vault con la credencial configurada.",
    -127: "Ansible terminó la baja. Reintenta el cierre local del inventario y la cuarentena.",
    -128: "Ansible terminó la baja, pero no se pudo confirmar su registro local. Verifica el resultado antes de reintentar.",
}


class RunnerError(RuntimeError):
    pass


class RunnerBusy(RunnerError):
    pass


def router_de_alta(site):
    router = valid_router(site)
    if router not in ROUTERS:
        raise RunnerError("Solo se permite aprovisionar routers R7–R10.")
    return router


def comando_alta(binario, router, vault):
    return comando_operacion(binario, router, vault, "alta")


def comando_operacion(binario, router, vault, tipo):
    if router not in ROUTERS or tipo not in PLAYBOOKS:
        raise RunnerError("Operación inválida. Solo se permiten routers R7–R10.")
    return [str(binario), PLAYBOOKS[tipo], "--limit", router,
            "--vault-password-file", str(vault)]


def ruta_local(project, value):
    if not value:
        raise RunnerError("Configura el ejecutable de Ansible y el archivo de contraseña del Vault.")
    return (project / value).resolve()


def validar_configuracion(files, site, tipo="alta"):
    router = router_de_alta(site)
    if router not in files.day0_ready:
        raise RunnerError("El operador debe confirmar la preparación Day-0 de este router en la VM.")
    project = files.project
    binario = ruta_local(project, files.playbook_bin)
    vault = ruta_local(project, files.vault_file)
    inventory = files.real_inventory
    required = (
        project / "ansible.cfg", project / PLAYBOOKS[tipo],
        inventory / "hosts.yml", inventory / "zabbix.yml", inventory / "sitios.yml",
        inventory / "host_vars" / f"{router}.yml", inventory / "group_vars/all/vault.yml",
    )
    vault_bin = binario.with_name("ansible-vault")
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (binario, vault_bin)):
        raise RunnerError("El ejecutable de Ansible no está disponible.")
    if (not vault.is_file() or vault.is_relative_to(project) or not os.access(vault, os.R_OK)
            or S_IMODE(vault.stat().st_mode) != 0o600 or not vault.stat().st_size):
        raise RunnerError("El archivo de contraseña del Vault debe ser legible, no vacío, externo al proyecto y tener permisos 0600.")
    if not all(path.is_file() and os.access(path, os.R_OK) for path in required):
        raise RunnerError("Faltan archivos del playbook, del inventario o del Vault.")
    try:
        dynamic = yaml.safe_load(required[4].read_text(encoding="utf-8"))
        variables = yaml.safe_load(required[5].read_text(encoding="utf-8"))
        hosts = dynamic["all"]["children"]["spokes"]["hosts"]
        with required[6].open("rb") as secret_file:
            cifrado = secret_file.read(15).startswith(b"$ANSIBLE_VAULT;")
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise RunnerError("El inventario generado no es válido.") from None
    if not isinstance(hosts, dict) or router not in hosts or variables != yaml.safe_load(host_vars(site)):
        raise RunnerError("El inventario no coincide con la reserva del sitio.")
    if not cifrado:
        raise RunnerError("El archivo de variables del Vault debe estar cifrado.")
    if site["underlay_ip"] != direcciones(site["site_id"])["underlay"]:
        raise RunnerError("La IP de underlay no corresponde al router reservado.")
    return comando_operacion(binario, router, vault, tipo), [
        str(vault_bin), "view", "--vault-password-file", str(vault), str(required[6])
    ]


def salida_proceso(command, project, lock_fd, consume, deadline):
    environment = dict(os.environ, ANSIBLE_NOCOLOR="1", PYTHONUNBUFFERED="1",
                       ANSIBLE_CONFIG=str(project / "ansible.cfg"),
                       ANSIBLE_STDOUT_CALLBACK="default",
                       ANSIBLE_CALLBACKS_ENABLED="", ANSIBLE_LOG_PATH=os.devnull,
                       ANSIBLE_DISPLAY_ARGS_TO_STDOUT="False",
                       ANSIBLE_INVENTORY=",".join(str(project / "inventory" / name)
                                                  for name in ("hosts.yml", "zabbix.yml", "sitios.yml")))
    with subprocess.Popen(command, cwd=project, shell=False, stdout=subprocess.PIPE if consume else subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=environment, start_new_session=True,
                          pass_fds=(lock_fd,)) as process:
        try:
            if consume is None:
                return process.wait(timeout=max(0, deadline - monotonic()))
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                pending = b""
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        if pending:
                            consume(pending.decode(errors="replace"))
                        break
                    lines = (pending + chunk).split(b"\n")
                    pending = lines.pop()
                    # La salida cruda es transitoria y las líneas enormes no se conservan.
                    if len(pending) > 65536:
                        pending = b""
                    for line in lines:
                        consume(line.decode(errors="replace"))
            return process.wait(timeout=max(0, deadline - monotonic()))
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise


def salida_simulada(router, resultado, consume, deadline):
    if resultado == "inalcanzable":
        return 4
    for role in ROLES:
        if monotonic() >= deadline:
            raise TimeoutError
        consume(f"TASK [{role} : tarea simulada]")
        sleep(0.6)
        if resultado == "fallo":
            consume(f"{router} : ok=0 changed=0 unreachable=0 failed=1")
            return 2
        consume(f"ok: [{router}]")
    consume(f"{router} : ok=6 changed=0 unreachable=0 failed=0")
    return 0


def baja_simulada(router, resultado, consume, deadline):
    for tarea in TAREAS_BAJA:
        if monotonic() >= deadline:
            raise TimeoutError
        consume(f"TASK [decommission : {tarea}]")
        sleep(0.25)
        if resultado == "fallo":
            consume(f"{router} : ok=0 changed=0 unreachable=0 failed=1")
            return 2
        consume(f"ok: [{router}]")
    consume(f"{router} : ok={len(TAREAS_BAJA)} changed=0 unreachable=0 failed=0")
    return 0


class AltaRunner:
    def __init__(self, ipam, files, *, timeout=600):
        self.ipam = ipam
        self.files = files
        self.timeout = timeout
        database = Path(ipam.database).resolve()
        self.lock_path = database.with_name(database.name + ".runner.lock")

    def adquirir(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            flock(fd, LOCK_EX | LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def recuperar(self):
        try:
            fd = self.adquirir()
        except BlockingIOError:
            return
        try:
            self._interrumpidos()
        finally:
            os.close(fd)

    def _interrumpidos(self):
        with transaction(self.ipam.database) as db:
            jobs = db.execute(
                """SELECT r.id AS run_id, r.tipo, r.simulado, j.usuario, s.id, s.site_id, s.router,
                          b.run_id AS recibo
                   FROM runner_jobs j JOIN runs r ON r.id = j.run_id
                   JOIN sites s ON s.id = r.site_id LEFT JOIN bajas_pendientes b ON b.sitio_id = s.id
                   WHERE r.estado = 'en_curso' AND r.simulado = ?""",
                (self.files.dry_run,),
            ).fetchall()
            for job in jobs:
                codigo = -127 if job["tipo"] == "baja" and job["recibo"] is not None else 125
                self._finalizar(db, job["run_id"], job, job["usuario"], codigo, None,
                                job["simulado"], job["tipo"])

    def iniciar(self, sitio_id, usuario, resultado, *, tipo="alta", confirmacion=None):
        if tipo not in PLAYBOOKS:
            raise RunnerError("Operación inválida.")
        if resultado not in ("exito", "fallo", "inalcanzable") or (
            not self.files.dry_run and resultado != "exito"
        ):
            raise RunnerError("El resultado simulado seleccionado no es válido.")
        try:
            fd = self.adquirir()
        except BlockingIOError:
            raise RunnerBusy("Ya hay una operación Ansible en ejecución.") from None
        try:
            self._interrumpidos()
            with transaction(self.ipam.database) as db:
                site = db.execute(
                    """SELECT s.*, n.gestionado, n.underlay_ip FROM sites s
                       JOIN nodes n ON n.router = s.router WHERE s.id = ?""", (sitio_id,),
                ).fetchone()
                if site is None:
                    return None
                estados = ("aprovisionando", "fallido") if tipo == "alta" else ("activo", "fallido")
                if not site["gestionado"] or site["estado"] not in estados:
                    raise RunnerError("La operación no está disponible para este sitio.")
                router_de_alta(site)
                if tipo == "baja" and (not confirmacion or confirmacion != site["codigo"]):
                    raise RunnerError("Escribe el código exacto del sitio para confirmar la baja.")
                self.files.ensure_mode(db)
                anterior = db.execute(
                    """SELECT r.tipo, r.codigo_salida FROM runs r JOIN runner_jobs j ON j.run_id = r.id
                       WHERE r.site_id = ? ORDER BY r.id DESC LIMIT 1""", (sitio_id,),
                ).fetchone()
                if site["estado"] == "fallido" and tipo != (anterior["tipo"] if anterior else "alta"):
                    raise RunnerError("Reintenta la operación original que falló.")
                if anterior and anterior["codigo_salida"] == -128:
                    raise RunnerError("Verifica la baja en la VM antes de reconciliar este sitio.")
                solo_cierre = tipo == "baja" and db.execute(
                    "SELECT 1 FROM bajas_pendientes WHERE sitio_id = ?", (sitio_id,)
                ).fetchone() is not None
                command = None
                if not self.files.dry_run and not solo_cierre:
                    command = validar_configuracion(self.files, site, tipo)
                if tipo == "baja":
                    self.ipam.retirar_sitio(site["site_id"], usuario=usuario, conexion=db)
                elif site["estado"] == "fallido":
                    self.ipam.reintentar_alta(site["site_id"], usuario=usuario, conexion=db)
                fecha = ahora_utc().isoformat()
                run_id = crear_run(db, sitio_id, tipo=tipo, simulado=self.files.dry_run, fecha=fecha)
                db.execute("INSERT INTO runner_jobs VALUES (?, ?)", (run_id, usuario))
                modalidad = "Simulación" if self.files.dry_run else "Ansible"
                registrar(db, fecha, usuario, f"iniciar_{tipo}", site["router"], f"{modalidad} · Inicio de {tipo}.")
            worker = Thread(target=self._ejecutar, args=(fd, run_id, dict(site), usuario, resultado, command, tipo, solo_cierre),
                            daemon=True)
            try:
                worker.start()
            except RuntimeError:
                with transaction(self.ipam.database) as db:
                    self._finalizar(db, run_id, site, usuario, 125, None, self.files.dry_run, tipo)
                raise RunnerError("No se pudo iniciar el aprovisionamiento en segundo plano.") from None
            fd = None
            return run_id
        finally:
            if fd is not None:
                os.close(fd)

    def _ejecutar(self, fd, run_id, site, usuario, resultado, command, tipo, solo_cierre):
        current = 4 if tipo == "alta" else 1
        anterior_ok = False
        recap = None
        router = site["router"]
        deadline = monotonic() + self.timeout

        def consume(line):
            nonlocal current, anterior_ok, recap
            match = re.match(r"^TASK \[([a-z0-9_]+)\s*:\s*([^\]]+)\]", line)
            target = None
            if match:
                if tipo == "alta":
                    target = ROLES.get(match[1])
                elif match[1] == "decommission":
                    target = TAREAS_BAJA.get(match[2].strip())
            if target == current + 1 and anterior_ok:
                with transaction(self.ipam.database) as db:
                    fecha = ahora_utc().isoformat()
                    # La primera etapa remota sigue a una etapa del portal ya cerrada.
                    if ETAPAS[tipo][current - 1][1] != "portal":
                        aplicar_evento(db, run_id, current, "ok", fecha=fecha)
                    aplicar_evento(db, run_id, target, "en_curso", fecha=fecha)
                    db.execute("INSERT INTO run_log (run_id, numero_linea, texto) VALUES (?, ?, ?)",
                               (run_id, target, f"En ejecución: {ETAPAS[tipo][target - 1][0]}."))
                current = target
                anterior_ok = False
            if re.match(rf"^(?:ok|changed|skipping): \[{router}(?: -> [^\]]+)?\]", line):
                anterior_ok = True
            match = re.match(rf"^{router}\s*:\s*(.+)$", line)
            if match:
                counts = dict(re.findall(r"\b(ok|changed|failed|unreachable)=(\d{1,9})\b", match[1]))
                if len(counts) == 4:
                    recap = {key: int(value) for key, value in counts.items()}

        codigo = 1
        try:
            if solo_cierre:
                codigo = 0
                return
            with transaction(self.ipam.database) as db:
                for orden in range(1, current + 1):
                    fecha = ahora_utc().isoformat()
                    aplicar_evento(db, run_id, orden, "en_curso", fecha=fecha)
                    if tipo == "alta" and orden == 4:
                        break
                    aplicar_evento(db, run_id, orden, "ok", fecha=fecha)
            if command is not None:
                playbook, comprobar_vault = command
                codigo = self._validar_conexion(fd, site, comprobar_vault, deadline)
                if codigo != 0:
                    return
            elif resultado == "inalcanzable":
                codigo = 4
                return
            if tipo == "alta":
                with transaction(self.ipam.database) as db:
                    aplicar_evento(db, run_id, 4, "ok", fecha=ahora_utc().isoformat())
            anterior_ok = True
            if command is None:
                simular = salida_simulada if tipo == "alta" else baja_simulada
                codigo = simular(router, resultado, consume, deadline)
            else:
                codigo = salida_proceso(playbook, self.files.project, fd, consume, deadline)
            if codigo == 0 and (recap is None or recap["failed"] or recap["unreachable"] or not recap["ok"]):
                codigo = 1
        except (TimeoutError, subprocess.TimeoutExpired):
            codigo = 124
        except Exception:
            codigo = 1
        finally:
            try:
                self._cerrar(run_id, site, usuario, codigo, recap, tipo, solo_cierre)
            finally:
                os.close(fd)

    def _validar_conexion(self, fd, site, comprobar_vault, deadline):
        if salida_proceso(comprobar_vault, self.files.project, fd, None, deadline):
            return 126
        ip = str(IPv4Address(site["underlay_ip"]))
        restante = deadline - monotonic()
        if restante <= 0:
            raise TimeoutError
        try:
            with socket.create_connection((ip, 22), timeout=min(5, restante)):
                return 0
        except OSError:
            return 4

    def _cerrar(self, run_id, site, usuario, codigo, recap, tipo, solo_cierre):
        if tipo == "baja" and codigo == 0:
            try:
                if not solo_cierre:
                    with transaction(self.ipam.database) as db:
                        db.execute("INSERT INTO bajas_pendientes VALUES (?, ?)", (site["id"], run_id))
            except sqlite3.Error:
                # Sin un recibo durable no se puede distinguir un fallo remoto de uno local.
                codigo = -128
            else:
                try:
                    with transaction(self.ipam.database) as db:
                        self._finalizar(db, run_id, site, usuario, 0, recap, self.files.dry_run, tipo)
                        db.execute("DELETE FROM bajas_pendientes WHERE sitio_id = ?", (site["id"],))
                        with self.files.publish(self.files.retirement_files(db, site)):
                            db.commit()
                    return
                except (InventoryError, OSError, sqlite3.Error, ValueError):
                    codigo = -127
        with transaction(self.ipam.database) as db:
            self._finalizar(db, run_id, site, usuario, codigo, recap, self.files.dry_run, tipo)

    def _finalizar(self, db, run_id, site, usuario, codigo, recap, simulado, tipo="alta"):
        fecha = ahora_utc().isoformat()
        mensaje = RESULTADOS.get(codigo, "No se pudo completar la ejecución de Ansible.")
        if tipo == "baja" and codigo == 0:
            mensaje = "Baja completada. Comenzó la cuarentena."
        if simulado:
            mensaje = f"Simulación · {mensaje}"
        pendientes = db.execute(
            """SELECT orden, estado FROM run_steps WHERE run_id = ? AND estado <> 'ok'
               ORDER BY orden""", (run_id,),
        ).fetchall()
        for paso in pendientes:
            orden = paso["orden"]
            if paso["estado"] == "pendiente":
                aplicar_evento(db, run_id, orden, "en_curso", fecha=fecha)
            correcto = codigo == 0 or (codigo == -127 and orden < 9)
            aplicar_evento(db, run_id, orden, "ok" if correcto else "error",
                           fecha=fecha, detalle=None if correcto else mensaje)
            if not correcto:
                break
        counts = recap or {}
        db.execute(
            """UPDATE runs SET codigo_salida = ?, ok_count = ?, changed_count = ?, failed_count = ?
               WHERE id = ?""", (codigo, counts.get("ok"), counts.get("changed"), counts.get("failed"), run_id),
        )
        db.execute("INSERT INTO run_log (run_id, numero_linea, texto) VALUES (?, ?, ?)", (run_id, len(ETAPAS[tipo]) + 1, mensaje))
        finalizar = self.ipam.finalizar_alta if tipo == "alta" else self.ipam.finalizar_baja
        finalizar(site["site_id"], codigo, usuario=usuario, conexion=db)
        accion = "resultado_aprovisionamiento" if tipo == "alta" else "resultado_baja"
        registrar(db, fecha, usuario, accion, site["router"], mensaje, codigo)
