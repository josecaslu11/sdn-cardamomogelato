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
CREATE TABLE IF NOT EXISTS audit_releases (
    log_id INTEGER PRIMARY KEY REFERENCES audit_log(id),
    sitio_id INTEGER NOT NULL REFERENCES sites(id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    tipo TEXT NOT NULL CHECK (tipo IN ('alta', 'baja')),
    simulado INTEGER NOT NULL CHECK (simulado IN (0, 1)),
    estado TEXT NOT NULL CHECK (estado IN ('en_curso', 'ok', 'error')),
    inicio TEXT NOT NULL,
    fin TEXT,
    codigo_salida INTEGER,
    ok_count INTEGER,
    changed_count INTEGER,
    failed_count INTEGER
);
CREATE INDEX IF NOT EXISTS runs_por_sitio ON runs(site_id, id DESC);
DROP INDEX IF EXISTS runs_simulado_alta;
CREATE TABLE IF NOT EXISTS runner_jobs (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id),
    usuario TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bajas_pendientes (
    sitio_id INTEGER PRIMARY KEY REFERENCES sites(id),
    run_id INTEGER NOT NULL REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS portal_inventory_mode (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    simulado INTEGER NOT NULL CHECK (simulado IN (0, 1))
);
CREATE TABLE IF NOT EXISTS run_steps (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    orden INTEGER NOT NULL,
    nombre TEXT NOT NULL,
    quien TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('pendiente', 'en_curso', 'ok', 'error')),
    inicio TEXT,
    fin TEXT,
    detalle TEXT,
    UNIQUE (run_id, orden)
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    numero_linea INTEGER NOT NULL,
    texto TEXT NOT NULL,
    UNIQUE (run_id, numero_linea)
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


def resumen_sitios(database):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        sitios = db.execute(
            """SELECT s.id, s.site_name, s.router, s.site_id, s.estado,
                      (SELECT r.simulado FROM runs r JOIN runner_jobs j ON j.run_id = r.id
                       WHERE r.site_id = s.id ORDER BY r.id DESC LIMIT 1) AS alta_simulada,
                      (SELECT ROUND(100.0 * SUM(estado = 'ok') / COUNT(*))
                       FROM run_steps WHERE run_id = (
                           SELECT id FROM runs WHERE site_id = s.id ORDER BY id DESC LIMIT 1
                       )) AS progreso,
                      v4.valor AS lan_v4, v6.valor AS lan_v6
               FROM sites AS s
               LEFT JOIN allocations AS v4 ON v4.sitio_id = s.id AND v4.recurso = 'lan_v4'
               LEFT JOIN allocations AS v6 ON v6.sitio_id = s.id AND v6.recurso = 'lan_v6'
               ORDER BY s.site_id, s.id DESC"""
        ).fetchall()
        indicadores = db.execute(
            """SELECT
                   (SELECT COUNT(*) FROM sites s WHERE s.estado = 'activo'
                    AND COALESCE((SELECT r.simulado FROM runs r JOIN runner_jobs j ON j.run_id = r.id
                                  WHERE r.site_id = s.id ORDER BY r.id DESC LIMIT 1), 0) = 0
                   ) AS activos,
                   (SELECT COUNT(*) FROM nodes
                    WHERE gestionado = 1 AND estado = 'disponible') AS disponibles,
                   (SELECT COUNT(*) FROM sites WHERE estado = 'aprovisionando')
                       AS aprovisionando"""
        ).fetchone()
    return sitios, indicadores


def detalle_sitio(database, sitio_id):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        sitio = db.execute("SELECT * FROM sites WHERE id = ?", (sitio_id,)).fetchone()
        if sitio is None:
            return None
        asignaciones = db.execute(
            """SELECT recurso, valor, estado, fecha_cambio
               FROM allocations WHERE sitio_id = ? ORDER BY id""",
            (sitio_id,),
        ).fetchall()
    return sitio, asignaciones


def listado_ipam(database, *, router="", recurso="", estado=""):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        opciones = {
            "router": db.execute(
                """SELECT s.router AS valor FROM sites s
                   WHERE EXISTS (SELECT 1 FROM allocations a WHERE a.sitio_id = s.id)
                   GROUP BY s.router ORDER BY MIN(s.site_id)"""
            ).fetchall(),
            "recurso": db.execute(
                "SELECT DISTINCT recurso AS valor FROM allocations ORDER BY recurso"
            ).fetchall(),
            "estado": db.execute(
                "SELECT DISTINCT estado AS valor FROM allocations ORDER BY estado"
            ).fetchall(),
        }
        asignaciones = db.execute(
            """SELECT s.id AS sitio_id, s.site_name, s.router,
                      a.recurso, a.valor, a.estado, a.fecha_cambio
               FROM allocations a
               JOIN sites s ON s.id = a.sitio_id
               WHERE (:router = '' OR s.router = :router)
                 AND (:recurso = '' OR a.recurso = :recurso)
                 AND (:estado = '' OR a.estado = :estado)
               ORDER BY s.site_id, s.id DESC, a.id""",
            {"router": router, "recurso": recurso, "estado": estado},
        ).fetchall()
    return asignaciones, opciones


def sitios_inventario(db):
    return db.execute(
        """SELECT s.id, s.router, s.site_id, s.site_name, s.lan_if
           FROM sites AS s JOIN nodes AS n ON n.router = s.router
           WHERE n.gestionado = 1 AND s.liberado_en IS NULL
             AND s.estado IN ('aprovisionando', 'activo', 'retirando', 'fallido')
           ORDER BY s.site_id"""
    ).fetchall()


def vista_inventario(database):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        return sitios_inventario(db)

def bitacora(database, *, sitio_id=None, router="", operacion="", resultado=""):
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            """WITH eventos AS (
                   SELECT r.inicio AS fecha, j.usuario, r.tipo AS operacion, s.router,
                          r.simulado, r.codigo_salida, r.estado AS resultado,
                          COALESCE((SELECT texto FROM run_log WHERE run_id = r.id
                                    ORDER BY numero_linea DESC LIMIT 1), '') AS resumen,
                          r.id AS run_id, s.id AS sitio_id,
                          strftime('%s', r.fin) - strftime('%s', r.inicio) AS segundos
                   FROM runs r JOIN runner_jobs j ON j.run_id = r.id JOIN sites s ON s.id = r.site_id
                   UNION ALL
                   SELECT a.fecha, a.usuario, 'liberar', a.router,
                          (SELECT simulado FROM portal_inventory_mode WHERE id = 1),
                          a.codigo_salida, 'ok', a.resumen, NULL, l.sitio_id, NULL
                   FROM audit_log a JOIN audit_releases l ON l.log_id = a.id
               )
               SELECT * FROM eventos
               WHERE (:sitio_id IS NULL OR sitio_id = :sitio_id)
                 AND (:router = '' OR router = :router)
                 AND (:operacion = '' OR operacion = :operacion)
                 AND (:resultado = '' OR resultado = :resultado)
               ORDER BY fecha DESC, run_id DESC""",
            {"sitio_id": sitio_id, "router": router, "operacion": operacion, "resultado": resultado},
        ).fetchall()
