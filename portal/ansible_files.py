"""Serialización y publicación de los archivos que consume Ansible."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import re
from stat import S_IMODE
from tempfile import NamedTemporaryFile

import yaml

from db import sitios_inventario, transaction
from ipam import ROOT, SITE_NAME


class InventoryError(RuntimeError):
    pass


class InventoryDumper(yaml.SafeDumper):
    pass


InventoryDumper.add_representer(
    type(None), lambda dumper, _: dumper.represent_scalar("tag:yaml.org,2002:null", "")
)


def yaml_text(data):
    return yaml.dump(data, Dumper=InventoryDumper, explicit_start=True, sort_keys=False)


def valid_router(site):
    site_id = site["site_id"]
    if type(site_id) is not int or not 7 <= site_id <= 242 or site["router"] != f"R{site_id}":
        raise ValueError("El router no corresponde a un número administrable.")
    return site["router"]


def host_vars(site):
    valid_router(site)
    if not isinstance(site["site_name"], str) or not SITE_NAME.fullmatch(site["site_name"]):
        raise ValueError("El nombre del sitio no es válido.")
    if not isinstance(site["lan_if"], str) or not re.fullmatch(r"Ethernet[0-9]+/[0-9]+", site["lan_if"]):
        raise ValueError("La interfaz LAN no es válida.")
    return yaml_text({key: site[key] for key in ("site_id", "site_name", "lan_if")})


def dynamic_inventory(sites):
    hosts = {valid_router(site): None for site in sites}
    return yaml_text({"all": {"children": {"spokes": {"hosts": hosts}}}})


class AnsibleFiles:
    def __init__(self, config_file, setting):
        with Path(config_file).open(encoding="utf-8") as file:
            config = yaml.safe_load(file)
        project = Path(setting("PORTAL_ANSIBLE_DIR") or config.get("ansible_dir") or ROOT.parent)
        if not project.is_absolute():
            project = ROOT / project
        self.project = project.resolve()
        self.playbook_bin = setting("PORTAL_ANSIBLE_PLAYBOOK_BIN") or config.get("ansible_playbook_bin")
        self.vault_file = setting("PORTAL_VAULT_PASSWORD_FILE") or config.get("vault_password_file")
        self.day0_ready = frozenset((setting("PORTAL_DAY0_READY_ROUTERS") or "").replace(" ", "").split(","))
        self.real_inventory = (project / "inventory").resolve()
        self.protected = (self.real_inventory, (ROOT.parent / "inventory").resolve())
        dry_run = setting("PORTAL_DRY_RUN", "1")
        if dry_run not in ("0", "1"):
            raise ValueError("PORTAL_DRY_RUN debe ser 0 o 1.")
        self.dry_run = dry_run == "1"
        development = Path(setting("PORTAL_DRY_RUN_DIR", ROOT / ".ansible-dev"))
        if not development.is_absolute():
            development = ROOT / development
        self.directory = (development / "inventory").resolve() if self.dry_run else self.real_inventory

    def validate_path(self, path):
        for entry in (path, path.parent):
            if entry.is_symlink():
                raise InventoryError("El destino del inventario contiene un enlace simbólico.")
        if path.exists() and not path.is_file():
            raise InventoryError("El destino del inventario debe ser un archivo regular.")
        if self.dry_run and any(
            path.resolve().is_relative_to(protected) for protected in self.protected
        ):
            raise InventoryError("El destino de desarrollo coincide con un inventario real.")

    def reserve(self, ipam, valores, usuario):
        with transaction(ipam.database) as db:
            site = ipam.reservar_sitio(**valores, usuario=usuario, conexion=db)
            sites = sitios_inventario(db)
            files = {
                self.directory / "host_vars" / f"{row['router']}.yml": host_vars(row)
                for row in sites
            }
            files[self.directory / "sitios.yml"] = dynamic_inventory(sites)
            with self.publish(files):
                db.commit()
            return site

    @contextmanager
    def publish(self, files):
        with ExitStack() as cleanup:
            staged = {}
            originals = {}
            for path, content in files.items():
                self.validate_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                mode = S_IMODE(path.stat().st_mode) if path.exists() else 0o600
                originals[path] = (
                    stage(path, path.read_bytes(), mode, cleanup) if path.exists() else None
                )
                staged[path] = stage(path, content.encode("utf-8"), mode, cleanup)

            published = []
            try:
                for path, source in staged.items():
                    os.replace(source, path)
                    published.append(path)
                yield
            except BaseException:
                restoration_failed = False
                for path in reversed(published):
                    try:
                        if originals[path] is None:
                            path.unlink()
                        else:
                            os.replace(originals[path], path)
                    except OSError:
                        restoration_failed = True
                if restoration_failed:
                    raise InventoryError("No fue posible restaurar el inventario. Revísalo antes de reintentar.")
                raise


def stage(path, content, mode, cleanup):
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        cleanup.callback(temp.unlink, missing_ok=True)
        os.fchmod(file.fileno(), mode)
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    return temp
