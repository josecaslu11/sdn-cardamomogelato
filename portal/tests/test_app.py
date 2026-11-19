import pytest

from app import create_app
from db import transaction
from ipam import IPAM
from seed import seed


@pytest.fixture
def ipam(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "birthday_day: 11\nbirthday_month_day: '1111'\nquarantine_days: 7\n",
        encoding="utf-8",
    )
    instance = IPAM(tmp_path / "portal.sqlite3", config)
    seed(instance)
    return instance, config


@pytest.fixture
def client(tmp_path, ipam, monkeypatch):
    monkeypatch.setenv("PORTAL_DRY_RUN", "1")
    monkeypatch.setenv("PORTAL_DRY_RUN_DIR", str(tmp_path / "ansible-dev"))
    env_file = tmp_path / ".env"
    env_file.write_text("PORTAL_USER=prueba\nPORTAL_PASSWORD=contraseña\n", encoding="utf-8")
    instance, config = ipam
    return create_app(env_file, database=instance.database, config_file=config).test_client()


def test_protected_home(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.location == "/login"


def test_rejects_wrong_password(client):
    response = client.post("/login", data={"username": "prueba", "password": "incorrecta"})
    assert response.status_code == 401
    assert client.get("/").status_code == 302


def test_login_and_logout(client):
    response = client.post(
        "/login",
        data={"username": "prueba", "password": "contraseña"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Lista de sitios" in response.get_data(as_text=True)
    assert client.get("/logout").status_code == 405

    response = client.post("/logout")
    assert response.location == "/login"
    assert client.get("/").location == "/login"


@pytest.fixture
def autenticado(client):
    client.post("/login", data={"username": "prueba", "password": "contraseña"})
    return client


def reservar(client, **campos):
    client.get("/sitios/nuevo")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    datos = {"nombre": "Escuintla", "codigo": "ESCUINTLA", "lan_if": "Ethernet0/1",
             "csrf_token": token}
    datos.update(campos)
    return client.post("/sitios/nuevo", data=datos)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_alta_protegida(client, method):
    response = client.open("/sitios/nuevo", method=method)
    assert response.status_code == 302
    assert response.location == "/login"


def test_listado_muestra_sitios_e_indicadores(autenticado):
    pagina = autenticado.get("/").get_data(as_text=True)
    for nombre in ("Mazate", "Suchi", "Zacapa", "Reu"):
        assert nombre in pagina
    assert "10.14.0.0/24" in pagina
    assert "2001:1111:3::/64" in pagina
    assert 'class="status status-activo"' in pagina
    assert '<dd id="activos">4</dd>' in pagina
    assert '<dd id="disponibles">4</dd>' in pagina
    assert '<dd id="aprovisionando">0</dd>' in pagina


def test_formulario_y_altas_consecutivas(autenticado, ipam):
    formulario = autenticado.get("/sitios/nuevo")
    assert formulario.status_code == 200
    assert 'value="Ethernet0/1" selected' in formulario.get_data(as_text=True)

    response = reservar(autenticado)
    assert response.status_code == 303
    assert response.location == "/"
    assert reservar(autenticado, nombre="Jutiapa", codigo="JUT").status_code == 303

    pagina = autenticado.get("/").get_data(as_text=True)
    for texto in ("Escuintla", "R7", "R8", "10.18.0.0/24", "2001:1111:8::/64",
                  "status-aprovisionando", "Sitio reservado en R7"):
        assert texto in pagina
    assert '<dd id="disponibles">2</dd>' in pagina
    assert '<dd id="aprovisionando">2</dd>' in pagina
    instance, _ = ipam
    with transaction(instance.database) as db:
        registros = db.execute(
            "SELECT usuario, router FROM audit_log WHERE accion = 'reservar' ORDER BY id"
        )
        assert [tuple(row) for row in registros] == [("prueba", "R7"), ("prueba", "R8")]


@pytest.mark.parametrize(("campos", "mensaje"), [
    ({"nombre": "Nombre inválido"}, "El nombre debe tener"),
    ({"codigo": "A_B"}, "El código solo admite"),
    ({"lan_if": "Ethernet9/9"}, "Selecciona una interfaz LAN"),
    ({"lan_if": "Ethernet0/2"}, "La interfaz LAN debe coincidir"),
    ({"csrf_token": ""}, "La solicitud no es válida"),
])
def test_rechazo_de_alta(autenticado, campos, mensaje):
    response = reservar(autenticado, **campos)
    assert response.status_code == 400
    pagina = response.get_data(as_text=True)
    assert mensaje in pagina
    assert f'value="{campos.get("nombre", "Escuintla")}"' in pagina
    assert f'value="{campos.get("codigo", "ESCUINTLA")}"' in pagina
    assert '<dd id="disponibles">4</dd>' in autenticado.get("/").get_data(as_text=True)


def test_codigo_duplicado(autenticado):
    assert reservar(autenticado).status_code == 303
    response = reservar(autenticado, nombre="Coban", codigo="escuintla")
    assert response.status_code == 400
    assert "El código ya existe." in response.get_data(as_text=True)


def test_pool_agotado(autenticado):
    for numero in range(4):
        assert reservar(autenticado, codigo=f"ESC{numero}").status_code == 303
    response = reservar(autenticado)
    assert response.status_code == 400
    assert "No hay routers disponibles." in response.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/sitios/1", "/sitios/999", "/ipam"])
def test_consultas_ipam_protegidas(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert response.location == "/login"


@pytest.mark.parametrize("sitio_id", [0, 999, 2**63])
def test_detalle_inexistente(autenticado, sitio_id):
    response = autenticado.get(f"/sitios/{sitio_id}")
    assert response.status_code == 404
    assert "El sitio solicitado no existe." in response.get_data(as_text=True)


def test_detalle_desde_listado_con_datos_guardados(autenticado, ipam):
    pagina = autenticado.get("/").get_data(as_text=True)
    assert '<a href="/sitios/1">Mazate</a>' in pagina
    instancia, _ = ipam
    with transaction(instancia.database) as db:
        db.execute(
            "UPDATE allocations SET valor = ? WHERE sitio_id = 1 AND recurso = 'lan_v4'",
            ("10.99.0.0/24",),
        )
        asignaciones = db.execute(
            "SELECT valor, fecha_cambio FROM allocations WHERE sitio_id = 1"
        ).fetchall()
    response = autenticado.get("/sitios/1")
    assert response.status_code == 200
    detalle = response.get_data(as_text=True)
    for dato in ("Mazate", "Código", "R3", "Ethernet0/2", "Creado", "Activo"):
        assert dato in detalle
    assert detalle.count('class="status status-en_uso"') == len(asignaciones)
    for valor, fecha in asignaciones:
        assert f"<code>{valor}</code>" in detalle
        assert f'datetime="{fecha}"' in detalle
    assert "10.14.0.0/24" not in detalle


def test_ipam_y_filtros(autenticado):
    assert reservar(autenticado).status_code == 303
    sin_filtros = autenticado.get("/ipam").get_data(as_text=True)
    assert "2001:1111:7::/64" in sin_filtros
    assert "2001:1111:3::/64" in sin_filtros

    filtros = {"router": "R7", "recurso": "lan_v6", "estado": "reservado"}
    pagina = autenticado.get("/ipam", query_string=filtros).get_data(as_text=True)
    for valor in filtros.values():
        assert f'value="{valor}" selected' in pagina
    assert pagina.count("<code>") == 1
    assert "<code>2001:1111:7::/64</code>" in pagina
    assert "status-reservado" in pagina
    for campo, valor in filtros.items():
        pagina = autenticado.get("/ipam", query_string={campo: valor}).get_data(as_text=True)
        assert "<code>2001:1111:7::/64</code>" in pagina
        assert pagina.count("<code>") < sin_filtros.count("<code>")

    vacia = autenticado.get("/ipam", query_string={"router": "R7", "estado": "en_uso"})
    assert vacia.status_code == 200
    assert "No se encontraron asignaciones." in vacia.get_data(as_text=True)


def test_detalle_e_ipam_preservan_historial_del_router(autenticado, ipam):
    from datetime import datetime, timedelta, timezone

    instancia, _ = ipam
    inicio = datetime(2026, 1, 1, tzinfo=timezone.utc)
    instancia.clock = lambda: inicio
    viejo = instancia.reservar_sitio("Anterior", "ANT")
    instancia.finalizar_alta(7, 0)
    instancia.retirar_sitio(7)
    instancia.finalizar_baja(7, 0)
    detalle = autenticado.get(f"/sitios/{viejo['id']}").get_data(as_text=True)
    assert "Cuarentena hasta" in detalle
    assert "status-cuarentena" in detalle

    instancia.clock = lambda: inicio + timedelta(days=7)
    nuevo = instancia.reservar_sitio("Actual", "ACT")
    assert viejo["router"] == nuevo["router"] == "R7"
    detalle = autenticado.get(f"/sitios/{viejo['id']}").get_data(as_text=True)
    assert "Anterior" in detalle
    assert "status-liberado" in detalle
    detalle = autenticado.get(f"/sitios/{nuevo['id']}").get_data(as_text=True)
    assert "Actual" in detalle
    assert "status-reservado" in detalle

    pagina = autenticado.get("/ipam?router=R7&recurso=lan_v4").get_data(as_text=True)
    assert pagina.count("<code>10.18.0.0/24</code>") == 2
    assert "status-reservado" in pagina
    assert "status-liberado" in pagina


def test_vista_previa_yaml_en_detalle(autenticado):
    assert reservar(autenticado).status_code == 303
    pagina = autenticado.get("/sitios/5").get_data(as_text=True)
    assert "inventory/host_vars/R7.yml" in pagina
    assert "inventory/sitios.yml" in pagina
    assert "site_id: 7" in pagina
    assert "site_name: Escuintla" in pagina
    assert "Modo de desarrollo" in pagina


def test_error_de_escritura_muestra_mensaje_y_permite_reintentar(autenticado, monkeypatch):
    from ansible_files import AnsibleFiles

    def fail(*args):
        raise OSError("Fallo simulado")

    with monkeypatch.context() as patch:
        patch.setattr(AnsibleFiles, "publish", fail)
        response = reservar(autenticado)
    assert response.status_code == 500
    assert "La reserva no se completó" in response.get_data(as_text=True)
    assert reservar(autenticado).status_code == 303
    assert "inventory/host_vars/R7.yml" in autenticado.get("/sitios/5").get_data(as_text=True)


@pytest.fixture
def alta_en_progreso(autenticado):
    assert reservar(autenticado).status_code == 303
    return autenticado


def simular_etapa(client, accion, sitio=5, *, con_token=True):
    client.get(f"/sitios/{sitio}")
    with client.session_transaction() as session:
        csrf = session.get("csrf_token", "")
    return client.post(f"/sitios/{sitio}/progreso/simular", data={
        "accion": accion, "csrf_token": csrf if con_token else "",
    })


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/sitios/5/progreso"),
    ("GET", "/runs/1/progress"),
    ("POST", "/sitios/5/progreso/simular"),
])
def test_endpoints_progreso_privados(client, method, path):
    response = client.open(path, method=method)
    assert response.status_code == 302
    assert response.location == "/login"


def test_progreso_inicial_y_etapas(alta_en_progreso):
    response = alta_en_progreso.get("/sitios/5/progreso")
    progreso = response.get_json()
    assert progreso["id"] is None
    assert (progreso["porcentaje"], progreso["hechos"], progreso["total"]) == (0, 0, 11)
    assert [paso["quien"] for paso in progreso["pasos"][4:10]] == [
        "ipsec", "dmvpn", "lan", "routing", "snmp", "zabbix",
    ]
    assert {paso["estado"] for paso in progreso["pasos"]} == {"pendiente"}
    pagina = alta_en_progreso.get("/sitios/5").get_data(as_text=True)
    assert "Simulación de aprovisionamiento" in pagina
    assert "Simular avance" in pagina
    assert "site_progress.js" in pagina


def test_avance_persistente_y_fin_sin_cambiar_ipam(alta_en_progreso, ipam, tmp_path):
    client = alta_en_progreso
    for _ in range(7):
        response = simular_etapa(client, "avanzar")
        assert response.status_code == 200
    progreso = response.get_json()
    assert (progreso["estado"], progreso["porcentaje"], progreso["hechos"]) == ("en_curso", 64, 7)
    assert progreso["pasos"][7]["estado"] == "en_curso"
    assert progreso["pasos"][6]["segundos"] >= 0
    assert progreso["simulado"] is True
    assert len(progreso["salida"]) == 7

    instance, config = ipam
    reiniciado = create_app(
        tmp_path / ".env", database=instance.database, config_file=config
    ).test_client()
    reiniciado.post("/login", data={"username": "prueba", "password": "contraseña"})
    assert reiniciado.get("/sitios/5/progreso").get_json()["porcentaje"] == 64
    assert "64 % · 7 de 11 etapas" in reiniciado.get("/sitios/5").get_data(as_text=True)
    assert reiniciado.get(f'/runs/{progreso["id"]}/progress').get_json()["hechos"] == 7

    for _ in range(4):
        response = simular_etapa(reiniciado, "avanzar")
        assert response.status_code == 200
    final = response.get_json()
    assert (final["estado"], final["porcentaje"], final["hechos"]) == ("ok", 100, 11)
    assert simular_etapa(reiniciado, "avanzar").status_code == 409
    with transaction(instance.database) as db:
        assert db.execute("SELECT estado FROM sites WHERE id = 5").fetchone()[0] == "aprovisionando"
        assert db.execute("SELECT estado FROM nodes WHERE router = 'R7'").fetchone()[0] == "aprovisionando"
        assert db.execute("SELECT DISTINCT estado FROM allocations WHERE sitio_id = 5").fetchone()[0] == "reservado"


def test_fallo_marca_etapa_actual_y_detiene_simulacion(alta_en_progreso):
    client = alta_en_progreso
    for _ in range(3):
        assert simular_etapa(client, "avanzar").status_code == 200
    response = simular_etapa(client, "fallar")
    assert response.status_code == 200
    progreso = response.get_json()
    assert (progreso["estado"], progreso["hechos"], progreso["porcentaje"]) == ("error", 3, 27)
    assert progreso["pasos"][3]["estado"] == "error"
    assert "Comprobar que el router responde" in progreso["pasos"][3]["detalle"]
    assert progreso["pasos"][4]["estado"] == "pendiente"
    assert progreso["salida"][-1].startswith("Simulación")
    assert "Fallida" in client.get("/sitios/5").get_data(as_text=True)
    assert simular_etapa(client, "avanzar").status_code == 409


def test_progreso_valida_dry_run_csrf_y_sitios(alta_en_progreso, ipam, tmp_path, monkeypatch):
    client = alta_en_progreso
    assert client.get("/sitios/999/progreso").status_code == 404
    assert client.get("/runs/999/progress").status_code == 404
    assert simular_etapa(client, "otra").status_code == 400
    assert simular_etapa(client, "fallar", con_token=False).status_code == 403
    assert simular_etapa(client, "avanzar", sitio=1).status_code == 409
    assert simular_etapa(client, "avanzar", sitio=999).status_code == 404
    assert client.get("/sitios/5/progreso").get_json()["id"] is None

    monkeypatch.setenv("PORTAL_DRY_RUN", "0")
    instance, config = ipam
    real_mode = create_app(tmp_path / ".env", database=instance.database, config_file=config).test_client()
    real_mode.post("/login", data={"username": "prueba", "password": "contraseña"})
    assert "Simular avance" not in real_mode.get("/sitios/5").get_data(as_text=True)
    assert simular_etapa(real_mode, "avanzar").status_code == 403


def test_eventos_de_una_ejecucion_no_simulada(alta_en_progreso, ipam):
    from progress import ProgresoNoDisponible, aplicar_evento, crear_alta

    instance, _ = ipam
    fecha = "2026-01-01T00:00:00+00:00"
    with transaction(instance.database) as db:
        run_id = crear_alta(db, 5, simulado=False, fecha=fecha)
        with pytest.raises(ProgresoNoDisponible):
            aplicar_evento(db, run_id, 2, "en_curso", fecha=fecha)
        aplicar_evento(db, run_id, 1, "en_curso", fecha=fecha)
        aplicar_evento(db, run_id, 1, "ok", fecha=fecha)

    progreso = alta_en_progreso.get(f"/runs/{run_id}/progress").get_json()
    assert progreso["simulado"] is False
    assert (progreso["hechos"], progreso["porcentaje"]) == (1, 9)
    assert simular_etapa(alta_en_progreso, "avanzar").status_code == 409


def test_aprovisionar_requiere_sesion_csrf_y_es_asincrono(alta_en_progreso, monkeypatch, ipam):
    from test_runner import esperar

    client = alta_en_progreso
    assert client.post("/sitios/5/aprovisionar").status_code == 403
    assert "Aprovisionar" in client.get("/sitios/5").get_data(as_text=True)
    assert "Aprovisionar" not in client.get("/sitios/1").get_data(as_text=True)
    with client.session_transaction() as session:
        csrf = session["csrf_token"]
    monkeypatch.setattr("runner.sleep", lambda _: None)
    response = client.post("/sitios/5/aprovisionar", data={"csrf_token": csrf})
    assert response.status_code == 202
    instance, _ = ipam
    esperar(instance.database, response.get_json()["id"])
    pagina = client.get("/").get_data(as_text=True)
    assert "Activo · Simulado" in pagina
    assert '<dd id="activos">4</dd>' in pagina
    client.post("/logout")
    assert client.post("/sitios/5/aprovisionar").location == "/login"


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/sitios/5/baja"), ("POST", "/sitios/5/liberar-cuarentena"),
    ("GET", "/bitacora"), ("GET", "/sitios/5/historial"), ("GET", "/runs/1"),
])
def test_baja_e_historial_exigen_autenticacion(client, method, path):
    assert client.open(path, method=method).location == "/login"


def test_baja_liberacion_manual_e_historial_en_flask(alta_en_progreso, ipam, monkeypatch):
    from test_runner import esperar

    client = alta_en_progreso
    instance, _ = ipam
    monkeypatch.setattr("runner.sleep", lambda _: None)

    def operacion(path, **datos):
        client.get("/sitios/5")
        with client.session_transaction() as session:
            datos["csrf_token"] = session["csrf_token"]
        return client.post(path, data=datos)

    def terminar(response):
        from fcntl import LOCK_EX, flock

        assert response.status_code == 202
        run_id = response.get_json()["id"]
        progreso = esperar(instance.database, run_id)
        with instance.database.with_name(instance.database.name + ".runner.lock").open("rb") as lock:
            flock(lock, LOCK_EX)
        return progreso

    alta = terminar(operacion("/sitios/5/aprovisionar"))
    assert client.post("/sitios/5/baja").status_code == 403
    assert operacion("/sitios/5/baja", confirmacion="incorrecto").status_code == 400
    fallo = terminar(operacion("/sitios/5/baja", confirmacion="ESCUINTLA", resultado="fallo"))
    assert "Reintentar baja" in client.get("/sitios/5").get_data(as_text=True)
    liberacion = "/sitios/5/liberar-cuarentena"
    assert operacion(liberacion, confirmacion="ESCUINTLA").status_code == 303
    with transaction(instance.database) as db:
        assert db.execute("SELECT liberado_en FROM sites WHERE id = 5").fetchone()[0] is None
    baja = terminar(operacion("/sitios/5/baja", confirmacion="ESCUINTLA"))
    assert baja["id"] != fallo["id"]
    ficha = client.get("/sitios/5").get_data(as_text=True)
    assert "Inicio de cuarentena" in ficha
    assert "Cuarentena hasta" in ficha
    assert "Liberar cuarentena" in ficha
    assert operacion(liberacion, confirmacion="incorrecto").status_code == 303
    with transaction(instance.database) as db:
        assert db.execute("SELECT liberado_en FROM sites WHERE id = 5").fetchone()[0] is None
    assert operacion(liberacion, confirmacion="ESCUINTLA").status_code == 303

    assert reservar(client, nombre="Otra", codigo="OTRA").status_code == 303
    with transaction(instance.database) as db:
        assert db.execute("SELECT router FROM sites WHERE codigo = 'OTRA'").fetchone()[0] == "R7"
        assert db.execute("SELECT DISTINCT estado FROM allocations WHERE sitio_id = 5").fetchone()[0] == "liberado"
    historial = client.get("/sitios/5/historial").get_data(as_text=True)
    for run in (alta, fallo, baja):
        assert f'/runs/{run["id"]}' in historial
    assert "Cuarentena liberada manualmente" in historial
    assert "No se encontraron operaciones" in client.get("/sitios/6/historial").get_data(as_text=True)
    filtro = client.get("/bitacora?router=R7&operacion=baja&resultado=error").get_data(as_text=True)
    assert f'/runs/{fallo["id"]}' in filtro
    assert f'/runs/{alta["id"]}' not in filtro
    assert f'/runs/{baja["id"]}' not in filtro
    assert "Simulada" in filtro
    assert "Salida resumida" in client.get(f'/runs/{baja["id"]}').get_data(as_text=True)
