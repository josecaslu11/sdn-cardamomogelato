import pytest

from app import create_app


@pytest.fixture
def client(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTAL_USER=prueba\nPORTAL_PASSWORD=contraseña\n", encoding="utf-8")
    return create_app(env_file).test_client()


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
