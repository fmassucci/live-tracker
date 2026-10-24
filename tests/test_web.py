import cv2
import numpy as np
import pytest
from conftest import FakeTools
from fastapi.testclient import TestClient

from portaria.db import Camera, get_setting
from portaria.web.app import create_app


@pytest.fixture
def app(cfg):
    application = create_app(cfg)
    application.state.services.people._tools = FakeTools()  # no model files needed
    return application


DEFAULT_PASSWORD_WARNING = "Você entrou com a senha padrão"


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        test_client.post("/login", data={"username": "admin", "password": "admin"})
        yield test_client


def test_pages_need_a_signed_in_user(app):
    with TestClient(app) as anonymous:
        response = anonymous.get("/people", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/people"


def test_first_start_has_admin_admin_and_asks_to_change_it(app):
    with TestClient(app) as test_client:
        response = test_client.post("/login", data={"username": "admin", "password": "admin"})
        assert response.status_code == 200 and DEFAULT_PASSWORD_WARNING in response.text
        test_client.post("/settings/password", data={"password": "nova-senha-1", "password_again": "nova-senha-1"})
        assert DEFAULT_PASSWORD_WARNING not in test_client.get("/").text
        test_client.post("/logout")
        assert test_client.post("/login", data={"username": "admin", "password": "admin"}).status_code == 400
        assert test_client.post("/login", data={"username": "admin", "password": "nova-senha-1"}).status_code == 200


def test_a_closed_notice_stays_closed_until_the_next_sign_in(client):
    assert DEFAULT_PASSWORD_WARNING in client.get("/").text
    assert client.post("/notices/default-password/hide").status_code == 204
    assert DEFAULT_PASSWORD_WARNING not in client.get("/people").text
    assert client.post("/notices/anything-else/hide").status_code == 404
    client.post("/logout")
    assert DEFAULT_PASSWORD_WARNING in client.post("/login", data={"username": "admin", "password": "admin"}).text


def test_the_default_password_cannot_be_chosen_again(client):
    response = client.post("/settings/password", data={"password": "admin", "password_again": "admin"})
    assert "A senha precisa ter pelo menos 8 caracteres." in response.text


def test_wrong_password_is_refused(app, client):
    with TestClient(app) as other:
        response = other.post("/login", data={"username": "admin", "password": "errada123"})
    assert response.status_code == 400
    assert "Usuário ou senha incorretos." in response.text


@pytest.mark.parametrize("path", ["/", "/people", "/people/new", "/records", "/cameras", "/cameras/new", "/live", "/settings"])
def test_every_page_renders_in_both_languages(client, path):
    assert client.get(path).status_code == 200
    client.post("/language", data={"lang": "en", "next": path})
    english = client.get(path)
    assert english.status_code == 200 and 'lang="en"' in english.text
    client.post("/language", data={"lang": "pt", "next": path})
    assert 'lang="pt-BR"' in client.get(path).text


def test_adding_a_person_with_a_photo(client):
    ok, png = cv2.imencode(".png", np.full((480, 480, 3), 128, np.uint8) + np.random.default_rng(1).integers(0, 60, (480, 480, 3)).astype(np.uint8))
    response = client.post(
        "/people/new",
        data={"name": "João da Silva", "category": "employee", "job_title": "Padeiro", "door_access": "true", "informed": "true"},
        files=[("photos", ("joao.png", png.tobytes(), "image/png"))],
    )
    assert response.status_code == 200
    assert "João da Silva" in response.text and "Fotos salvas: 1." in response.text
    assert "Pronta para ser reconhecida" not in response.text  # one photo is not enough yet
    assert "Reconhecida; mais fotos deixam mais seguro" in response.text


def test_photos_need_the_lgpd_confirmation(client):
    response = client.post(
        "/people/new",
        data={"name": "Ana", "category": "employee"},
        files=[("photos", ("ana.png", b"not really an image", "image/png"))],
    )
    assert response.status_code == 400 and "confirme que a pessoa foi informada" in response.text


def test_photo_check_explains_a_bad_file(client):
    response = client.post("/people/check-photo", files={"photo": ("x.jpg", b"nope", "image/jpeg")})
    assert response.json() == {"ok": False, "message": "O sistema não consegue ler este arquivo como foto. Use JPG ou PNG."}


def test_logo_upload_is_stored_as_png_and_shown(client, cfg):
    ok, jpeg = cv2.imencode(".jpg", np.full((300, 900, 3), (40, 90, 200), np.uint8))
    client.post("/settings/logo", files={"logo": ("logo.jpg", jpeg.tobytes(), "image/jpeg")})
    logo = cfg.paths.data_dir / "branding" / "logo.png"
    assert logo.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert cv2.imread(str(logo)).shape[1] <= 512
    assert '/branding/logo.png?v=' in client.get("/").text


def test_live_frame_state_is_quiet_without_the_recognition_service(client):
    assert client.get("/cameras/1/state").json() == {"tone": "idle", "label": ""}


def test_door_screen_needs_its_secret_address(client, app):
    db = app.state.services.db
    with db.session() as session:
        session.add(Camera(name="Porta", url="0", role="door"))
        session.flush()
        key = get_setting(session, "door_screen_key")
    assert client.get("/door?camera=1&key=wrong").status_code == 404
    assert client.get(f"/door?camera=1&key={key}").status_code == 200
    state = client.get(f"/door/state?camera=1&key={key}").json()
    assert state["tone"] == "bad"  # the recognition service isn't running in tests
