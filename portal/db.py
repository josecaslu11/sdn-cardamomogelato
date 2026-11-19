from contextlib import closing, contextmanager
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    router TEXT PRIMARY KEY,
    site_id INTEGER NOT NULL UNIQUE CHECK (site_id BETWEEN 1 AND 242),
    underlay_ip TEXT NOT NULL UNIQUE,
    lan_if TEXT,
    gestionado INTEGER NOT NULL CHECK (gestionado IN (0, 1)),
    estado TEXT NOT NULL CHECK (estado IN (
        'disponible', 'aprovisionando', 'activo', 'retirando', 'retirado', 'fallido'
    ))
);
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES nodes(site_id),
    router TEXT NOT NULL REFERENCES nodes(router),
    site_name TEXT NOT NULL,
    codigo TEXT NOT NULL UNIQUE COLLATE NOCASE,
    lan_if TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN (
        'aprovisionando', 'activo', 'retirando', 'retirado', 'fallido'
    )),
    creado_en TEXT NOT NULL,
    retirado_en TEXT,
    cuarentena_hasta TEXT,
    liberado_en TEXT,
    CHECK (router = 'R' || site_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS sites_router_ocupado
    ON sites(router) WHERE liberado_en IS NULL;
CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES nodes(site_id),
    sitio_id INTEGER NOT NULL REFERENCES sites(id),
    recurso TEXT NOT NULL,
    valor TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('reservado', 'en_uso', 'cuarentena', 'liberado')),
    fecha_cambio TEXT NOT NULL,
    UNIQUE (sitio_id, recurso)
);
CREATE UNIQUE INDEX IF NOT EXISTS allocations_valor_ocupado
    ON allocations(valor) WHERE estado <> 'liberado';
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    usuario TEXT NOT NULL,
    accion TEXT NOT NULL,
    router TEXT NOT NULL REFERENCES nodes(router),
    codigo_salida INTEGER,
    resumen TEXT NOT NULL
);
"""


def initialize(database):
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)


@contextmanager
def transaction(database):
    db = sqlite3.connect(database)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()
