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
from stat import S_IMODE
import subprocess
from threading import Thread
from time import monotonic, sleep

import yaml

from ansible_files import host_vars, valid_router
from db import transaction
from ipam import direcciones, registrar
from progress import ETAPAS_ALTA, ahora_utc, aplicar_evento, crear_alta


ROUTERS = frozenset(("R7", "R8", "R9", "R10"))
ROLES = {quien: orden for orden, (_, quien) in enumerate(ETAPAS_ALTA, 1) if quien != "portal"}
RESULTADOS = {
    0: "Alta completada.",
    2: "Falló una tarea de Ansible.",
    4: "El router no está accesible.",
    124: "Se agotó el tiempo límite de aprovisionamiento.",
    125: "La ejecución se interrumpió.",
    126: "No se pudo abrir el Vault con la credencial configurada.",
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
    if router not in ROUTERS:
        raise RunnerError("Solo se permite aprovisionar routers R7–R10.")
    return [str(binario), "playbooks/site_add.yml", "--limit", router,
            "--vault-password-file", str(vault)]


def ruta_local(project, value):
    if not value:
        raise RunnerError("Configura el ejecutable de Ansible y el archivo de contraseña del Vault.")
    return (project / value).resolve()


def validar_configuracion(files, site):
    router = router_de_alta(site)
    if router not in files.day0_ready:
        raise RunnerError("El operador debe confirmar la preparación Day-0 de este router en la VM.")
    project = files.project
    binario = ruta_local(project, files.playbook_bin)
    vault = ruta_local(project, files.vault_file)
    inventory = files.real_inventory
    required = (
        project / "ansible.cfg", project / "playbooks/site_add.yml",
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
    return comando_alta(binario, router, vault), [
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
                """SELECT r.id, r.simulado, j.usuario, s.site_id, s.router
                   FROM runner_jobs j JOIN runs r ON r.id = j.run_id
                   JOIN sites s ON s.id = r.site_id WHERE r.estado = 'en_curso'"""
            ).fetchall()
            for job in jobs:
                self._finalizar(db, job["id"], job, job["usuario"], 125, None, job["simulado"])

    def iniciar(self, sitio_id, usuario, resultado):
        if resultado not in ("exito", "fallo", "inalcanzable") or (
            not self.files.dry_run and resultado != "exito"
        ):
            raise RunnerError("El resultado simulado seleccionado no es válido.")
        try:
            fd = self.adquirir()
        except BlockingIOError:
            raise RunnerBusy("Ya hay un aprovisionamiento en ejecución.") from None
        try:
            self._interrumpidos()
            with transaction(self.ipam.database) as db:
                site = db.execute(
                    """SELECT s.*, n.gestionado, n.underlay_ip FROM sites s
                       JOIN nodes n ON n.router = s.router WHERE s.id = ?""", (sitio_id,),
                ).fetchone()
                if site is None:
                    return None
                if not site["gestionado"] or site["estado"] not in ("aprovisionando", "fallido"):
                    raise RunnerError("El sitio no está disponible para aprovisionamiento.")
                router_de_alta(site)
                if not self.files.dry_run:
                    anterior = db.execute(
                        """SELECT r.simulado FROM runs r JOIN runner_jobs j ON j.run_id = r.id
                           WHERE r.site_id = ? ORDER BY r.id DESC LIMIT 1""", (sitio_id,),
                    ).fetchone()
                    if anterior is not None and anterior["simulado"]:
                        raise RunnerError("Usa una base de producción separada de las ejecuciones simuladas.")
                command = None if self.files.dry_run else validar_configuracion(self.files, site)
                if site["estado"] == "fallido":
                    self.ipam.reintentar_alta(site["site_id"], usuario=usuario, conexion=db)
                fecha = ahora_utc().isoformat()
                run_id = crear_alta(db, sitio_id, simulado=self.files.dry_run, fecha=fecha)
                db.execute("INSERT INTO runner_jobs VALUES (?, ?)", (run_id, usuario))
                registrar(db, fecha, usuario, "aprovisionar", site["router"],
                          "Inicio de simulación." if self.files.dry_run else "Inicio de aprovisionamiento.")
            worker = Thread(target=self._ejecutar, args=(fd, run_id, dict(site), usuario, resultado, command),
                            daemon=True)
            try:
                worker.start()
            except RuntimeError:
                with transaction(self.ipam.database) as db:
                    self._finalizar(db, run_id, site, usuario, 125, None, self.files.dry_run)
                raise RunnerError("No se pudo iniciar el aprovisionamiento en segundo plano.") from None
            fd = None
            return run_id
        finally:
            if fd is not None:
                os.close(fd)

    def _ejecutar(self, fd, run_id, site, usuario, resultado, command):
        current = 4
        resultado_de_tarea = False
        recap = None
        router = site["router"]
        deadline = monotonic() + self.timeout

        def consume(line):
            nonlocal current, resultado_de_tarea, recap
            match = re.match(r"^TASK \[([a-z0-9_]+)\s*:", line)
            target = ROLES.get(match[1]) if match else None
            if target is not None and target == current + 1 and (
                current == 4 or resultado_de_tarea
            ):
                with transaction(self.ipam.database) as db:
                    if current > 4:
                        aplicar_evento(db, run_id, current, "ok", fecha=ahora_utc().isoformat())
                    aplicar_evento(db, run_id, target, "en_curso", fecha=ahora_utc().isoformat())
                    db.execute("INSERT INTO run_log (run_id, numero_linea, texto) VALUES (?, ?, ?)",
                               (run_id, target, f"En ejecución: {ETAPAS_ALTA[target - 1][0]}."))
                current = target
                resultado_de_tarea = False
            if re.match(rf"^(?:ok|changed|skipping): \[{router}\]", line):
                resultado_de_tarea = True
            match = re.match(rf"^{router}\s*:\s*(.+)$", line)
            if match:
                counts = dict(re.findall(r"\b(ok|changed|failed|unreachable)=(\d{1,9})\b", match[1]))
                if len(counts) == 4:
                    recap = {key: int(value) for key, value in counts.items()}

        codigo = 1
        try:
            for orden in range(1, 4):
                with transaction(self.ipam.database) as db:
                    fecha = ahora_utc().isoformat()
                    aplicar_evento(db, run_id, orden, "en_curso", fecha=fecha)
                    aplicar_evento(db, run_id, orden, "ok", fecha=fecha)
            with transaction(self.ipam.database) as db:
                aplicar_evento(db, run_id, 4, "en_curso", fecha=ahora_utc().isoformat())
            if command is not None:
                playbook, comprobar_vault = command
                if salida_proceso(comprobar_vault, self.files.project, fd, None, deadline):
                    codigo = 126
                    return
                ip = str(IPv4Address(site["underlay_ip"]))
                try:
                    with socket.create_connection((ip, 22), timeout=max(0.001, min(5, deadline - monotonic()))):
                        pass
                except OSError:
                    codigo = 4
                    return
            elif resultado == "inalcanzable":
                codigo = 4
                return
            with transaction(self.ipam.database) as db:
                aplicar_evento(db, run_id, 4, "ok", fecha=ahora_utc().isoformat())
            if command is None:
                codigo = salida_simulada(router, resultado, consume, deadline)
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
                with transaction(self.ipam.database) as db:
                    self._finalizar(db, run_id, site, usuario, codigo, recap, self.files.dry_run)
            finally:
                os.close(fd)

    def _finalizar(self, db, run_id, site, usuario, codigo, recap, simulado):
        fecha = ahora_utc().isoformat()
        mensaje = RESULTADOS.get(codigo, "No se pudo completar la ejecución de Ansible.")
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
            aplicar_evento(db, run_id, orden, "ok" if codigo == 0 else "error",
                           fecha=fecha, detalle=mensaje if codigo != 0 else None)
            if codigo != 0:
                break
        counts = recap or {}
        db.execute(
            """UPDATE runs SET codigo_salida = ?, ok_count = ?, changed_count = ?, failed_count = ?
               WHERE id = ?""", (codigo, counts.get("ok"), counts.get("changed"), counts.get("failed"), run_id),
        )
        db.execute("INSERT INTO run_log (run_id, numero_linea, texto) VALUES (?, ?, ?)", (run_id, len(ETAPAS_ALTA) + 1, mensaje))
        self.ipam.finalizar_alta(site["site_id"], codigo, usuario=usuario, conexion=db)
        registrar(db, fecha, usuario, "resultado_aprovisionamiento", site["router"], mensaje, codigo)
