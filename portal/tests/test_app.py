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
def client(tmp_path, ipam):
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
