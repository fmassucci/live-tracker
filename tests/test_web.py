import io
import re
import zipfile
from datetime import date, datetime, time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from conftest import FakeTools
from fastapi.testclient import TestClient
from sqlalchemy import select

from portaria.db import AuditEntry, Camera, Event, Person, User, get_setting
from portaria.events import face_path
from portaria.users import authenticate, create_user, is_admin
from portaria.web.app import create_app
from portaria.web.common import recording_state


class NoVision:
    """The recognition service isn't running in tests."""

    def status(self):
        return None

    def camera_status(self, camera_id):
        return None

    def frame(self, channel):
        return None

    def stream(self, channel):
        return None


@pytest.fixture
def app(cfg):
    application = create_app(cfg)
    application.state.services.people._tools = FakeTools()  # no model files needed
    application.state.services.vision = NoVision()
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


def test_settings_shows_the_addresses_to_open_the_app(client, monkeypatch):
    monkeypatch.setattr("portaria.web.common.local_ipv4", lambda: ["192.168.0.19", "10.0.0.5"])
    text = client.get("/settings").text
    assert 'value="http://127.0.0.1:8000"' in text
    assert 'value="http://192.168.0.19:8000"' in text and 'value="http://10.0.0.5:8000"' in text
    assert "Se o primeiro endereço não abrir, tente o próximo." in text


def test_the_door_screen_address_is_the_one_the_tablet_opens(client, app, monkeypatch):
    monkeypatch.setattr("portaria.web.common.local_ipv4", lambda: ["192.168.0.19"])
    with app.state.services.db.session() as session:
        session.add(Camera(name="Porta", url="0", device="door"))
        session.flush()
        key = get_setting(session, "door_screen_key")
    assert f'value="http://192.168.0.19:8000/door?camera=1&key={key}"' in client.get("/settings").text


def test_settings_says_when_only_this_computer_can_open_the_app(client, cfg):
    cfg.web.host = "127.0.0.1"
    text = client.get("/settings").text
    assert 'value="http://127.0.0.1:8000"' in text
    assert "Só este computador consegue acessar o sistema." in text


@pytest.fixture
def opened(monkeypatch):
    """Folders the Settings button showed, instead of showing them on the screen."""
    folders = []
    monkeypatch.setattr("portaria.web.data.open_folder", folders.append)
    monkeypatch.setattr("portaria.web.common.local_ipv4", lambda: ["192.168.0.19"])
    return folders


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.0.19"])  # this computer, by either of its addresses
def test_settings_opens_the_images_folder_on_this_computer(app, cfg, opened, address):
    with TestClient(app, client=(address, 50000)) as here:
        here.post("/login", data={"username": "admin", "password": "admin"})
        assert 'action="/settings/data-folder"' in here.get("/settings").text
        assert here.post("/settings/data-folder", follow_redirects=False).status_code == 303
    assert opened == [cfg.paths.data_dir.resolve()]


def test_other_devices_see_where_the_images_are_but_cannot_open_the_folder(client, cfg, opened):
    text = client.get("/settings").text
    assert f'value="{cfg.paths.data_dir.resolve()}"' in text
    assert 'action="/settings/data-folder"' not in text
    assert "acesse as Configurações no computador onde o sistema está instalado" in text
    assert client.post("/settings/data-folder").status_code == 403
    assert opened == []


def test_only_administrators_see_the_database_and_add_accounts(app, client, opened):
    with app.state.services.db.session() as session:
        create_user(session, "caixa", "senha-do-caixa")
    with TestClient(app, client=("127.0.0.1", 50000)) as cashier:
        cashier.post("/login", data={"username": "caixa", "password": "senha-do-caixa"})
        text = cashier.get("/settings").text
        assert 'id="data"' not in text and 'action="/settings/users"' not in text
        assert cashier.post("/settings/users", data={"username": "chefe", "password": "senha-do-chefe",
                                                     "administrator": "true"}).status_code == 403
        assert cashier.post("/settings/data/export").status_code == 403
        assert cashier.post("/settings/data/delete", data={"confirm": "true"}).status_code == 403
        assert cashier.get("/settings/data/summary").status_code == 403
        assert cashier.post("/settings/data-folder").status_code == 403
        assert 'action="/settings/users/1"' not in text  # admin's account: no editing it
        assert cashier.post("/settings/users/1", data={"password": "tomei-a-conta"}).status_code == 403
        assert cashier.post("/settings/users/1/delete", data={"confirm": "true"}).status_code == 403
    assert opened == []
    assert 'id="data"' in client.get("/settings").text  # admin, the first account, is an administrator
    client.post("/settings/users", data={"username": "gerente", "password": "senha-do-gerente", "administrator": "true"})
    with app.state.services.db.session() as session:
        assert is_admin(session, "gerente") and not is_admin(session, "caixa")


def test_an_administrator_edits_and_removes_the_other_accounts(app, client):
    with app.state.services.db.session() as session:
        create_user(session, "caixa", "senha-do-caixa")
    with app.state.services.db.session() as session:
        cashier_id, admin_id = (session.scalar(select(User.id).where(User.username == name)) for name in ("caixa", "admin"))
    text = client.get("/settings").text
    assert f'action="/settings/users/{cashier_id}"' in text and f'action="/settings/users/{admin_id}"' not in text
    response = client.post(f"/settings/users/{cashier_id}", data={"administrator": "true", "password": "nova-senha-1"})
    assert "Conta caixa salva." in response.text
    with app.state.services.db.session() as session:
        assert is_admin(session, "caixa") and authenticate(session, "caixa", "nova-senha-1")
    with TestClient(app) as cashier:
        cashier.post("/login", data={"username": "caixa", "password": "nova-senha-1"})
        client.post(f"/settings/users/{cashier_id}/delete")  # not confirmed
        assert cashier.get("/people", follow_redirects=False).status_code == 200
        response = client.post(f"/settings/users/{cashier_id}/delete", data={"confirm": "true"})
        assert "Conta caixa removida." in response.text
        assert cashier.get("/people", follow_redirects=False).status_code == 303  # signed out at once
    # Your own account changes in Change my password, and you can't remove it yourself.
    assert client.post(f"/settings/users/{admin_id}", data={"password": "outra-senha-1"}).status_code == 403
    assert client.post(f"/settings/users/{admin_id}/delete", data={"confirm": "true"}).status_code == 403


def _record(app, cfg, at: datetime, kind: str = "recognized", name: str | None = "Ana") -> str:
    """A record with its picture and face crop on disk, as the recognition service leaves them."""
    snapshot = f"snapshots/{at:%Y-%m-%d}/{at:%H%M%S_%f}_{kind}.jpg"
    for path in (cfg.paths.data_dir / snapshot, cfg.paths.data_dir / face_path(snapshot)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"picture")
    with app.state.services.db.session() as session:
        session.add(Event(at=at, kind=kind, person_name=name, score=0.712, snapshot=snapshot, door="shadow",
                          model="auraface", liveness="none"))
    return snapshot


def test_records_download_as_a_spreadsheet_and_pictures(app, cfg, client):
    first = _record(app, cfg, datetime(2026, 10, 22, 7, 58))
    second = _record(app, cfg, datetime(2026, 10, 23, 8, 1), kind="unknown", name=None)
    _record(app, cfg, datetime(2026, 10, 24, 8, 3))  # after the period
    response = client.post("/settings/data/export", data={"start": "2026-10-22", "end": "2026-10-23"})
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''Registros_2026-10-22_2026-10-23.zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == sorted(["Registros_2026-10-22_2026-10-23.csv", first, second])
    table = archive.read("Registros_2026-10-22_2026-10-23.csv").decode("utf-8-sig").splitlines()
    assert table == [
        "Data;Hora;Pessoa;Resultado;Câmera;Porta;Semelhança;Imagem",
        f"2026-10-22;07:58:00;Ana;Reconhecido;;modo de teste, porta continuou fechada;0,71;{first}",
        f"2026-10-23;08:01:00;;Não reconhecido;;modo de teste, porta continuou fechada;0,71;{second}",
    ]
    assert client.post("/settings/data/export", data={"start": "2026-11-01"}).url.path == "/settings"  # nothing to send


def test_the_count_follows_the_days_chosen(app, cfg, client):
    for day in (22, 23, 23):
        _record(app, cfg, datetime(2026, 10, day, 8, 0))
    assert "3 registros, de 22/10/2026 a 23/10/2026." in client.get("/settings").text
    assert client.get("/settings/data/summary?start=2026-10-23&end=").text.strip() == "2 registros, em 23/10/2026."
    assert client.get("/settings/data/summary?start=2026-10-24").text.strip() == "Nenhum registro neste período."
    assert client.get("/settings/data/summary?start=amanha").status_code == 400  # never read as "every day"


def test_deleting_records_needs_the_confirmation_and_keeps_the_other_days(app, cfg, client):
    kept = _record(app, cfg, datetime(2026, 10, 22, 7, 58))
    deleted = _record(app, cfg, datetime(2026, 10, 23, 8, 1))
    client.post("/settings/data/delete", data={"start": "2026-10-23", "end": "2026-10-23"})
    assert (cfg.paths.data_dir / deleted).is_file()  # not confirmed
    response = client.post("/settings/data/delete", data={"start": "2026-10-23", "end": "2026-10-23", "confirm": "true"})
    assert "Registros excluídos: 1." in response.text
    assert not (cfg.paths.data_dir / deleted).parent.exists()  # the day's folder went with its last picture
    assert (cfg.paths.data_dir / kept).is_file() and (cfg.paths.data_dir / face_path(kept)).is_file()
    with app.state.services.db.session() as session:
        assert [e.snapshot for e in session.scalars(select(Event))] == [kept]
        assert session.scalar(select(AuditEntry.detail).where(AuditEntry.action == "records deleted")) == (
            "2026-10-23 to 2026-10-23: 1"
        )


def test_recording_state_says_why_a_camera_is_not_recording():
    camera = SimpleNamespace(enabled=True)
    assert recording_state(camera, {"online": True}, service_up=True) == "recording"
    assert recording_state(camera, {"online": False}, service_up=True) == "no-image"
    assert recording_state(camera, None, service_up=False) == "service-down"
    assert recording_state(SimpleNamespace(enabled=False), {"online": True}, service_up=True) == "stopped"


def test_a_camera_has_an_ai_model_and_an_attached_device(client, app):
    form = client.get("/cameras/new").text
    assert "Modelo de IA" in form and "Reconhecimento facial" in form and "Dispositivo ligado" in form
    client.post("/cameras/new", data={"name": "Cozinha", "url": "0", "analysis": "faces", "device": ""})
    cameras = client.post("/cameras/new", data={"name": "Porta", "url": "1", "analysis": "faces", "device": "door"})
    assert cameras.text.count("Parar gravação") == 2  # every camera records
    assert cameras.text.count("Abre a porta") == 1
    with app.state.services.db.session() as session:
        assert [c.device for c in session.scalars(select(Camera).order_by(Camera.id))] == ["", "door"]


def test_a_camera_can_be_stopped_and_started_from_its_settings(client, app):
    with app.state.services.db.session() as session:
        session.add(Camera(name="Porta", url="0", device="door"))
    assert "Parar gravação" in client.get("/cameras").text
    today = client.post("/cameras/1/stop", data={"next": "/"})  # follows the redirect back to Today
    assert "Gravação parada: Porta." in today.text
    assert "A gravação está parada" in today.text and "Iniciar gravação" in today.text
    with app.state.services.db.session() as session:
        assert session.get(Camera, 1).enabled is False
    cameras = client.post("/cameras/1/start")
    assert "Gravação iniciada: Porta." in cameras.text and "Parar gravação" in cameras.text
    assert client.post("/cameras/99/start").status_code == 404


class SeenVision(NoVision):
    """The recognition service receives camera 1's picture at this size."""

    def __init__(self, size: str) -> None:
        self.size = size

    def status(self):
        return {"cameras": {"1": {"online": True, "size": self.size, "fps": 17.2, "state": "idle"}}}


def test_a_one_lens_camera_is_not_offered_a_split(client, app):
    app.state.services.vision = SeenVision("1920x1080")
    with app.state.services.db.session() as session:
        session.add(Camera(name="Jortan", url="rtsp://admin:segredo@192.168.0.38:554/onvif1", device="door"))
    edit = client.get("/cameras/1").text
    assert "Separar em duas câmeras" not in edit and "Metade de cima" not in edit


def test_a_two_lens_camera_can_be_split_into_one_camera_per_lens(client, app):
    app.state.services.vision = SeenVision("1920x2160")  # two 1920x1080 lenses, one above the other
    with app.state.services.db.session() as session:
        session.add(Camera(name="Jortan", url="rtsp://admin:segredo@192.168.0.37:554/onvif1", device="door"))
    assert "Separar em duas câmeras" in client.get("/cameras/1").text
    cameras = client.post("/cameras/1/split")
    assert "Jortan agora são duas câmeras, uma para cada lente." in cameras.text
    assert cameras.text.count("Parar gravação") == 2  # both lenses record
    with app.state.services.db.session() as session:
        top, bottom = session.scalars(select(Camera).order_by(Camera.id)).all()
        assert (top.name, top.part, top.device) == ("Jortan (cima)", "top", "door")
        assert (bottom.name, bottom.part, bottom.device) == ("Jortan (baixo)", "bottom", "")  # the door opens once
        assert bottom.url == top.url  # the same address: the camera is still read once
    edit = client.get("/cameras/1").text
    assert "Separar em duas câmeras" not in edit and "Metade de cima" in edit


def test_open_pages_can_follow_every_camera(client, app):
    with app.state.services.db.session() as session:
        session.add(Camera(name="Porta", url="0", device="door"))
    states = client.get("/cameras-states").json()
    assert states == {"1": {"state": "service-down", "label": "Serviço de reconhecimento parado"}}


def _event(kind: str, name: str | None, at: datetime | None = None) -> Event:
    return Event(at=at or datetime.now(), kind=kind, person_name=name, score=0.7, door="shadow", model="auraface",
                 liveness="none")


def test_today_lists_who_came_in_and_shows_faces_not_recognized_only_when_there_are_some(client, app):
    assert "Não reconhecidos hoje" not in client.get("/").text
    with app.state.services.db.session() as session:
        session.add_all([_event("recognized", "Ana Souza"), _event("unknown", None)])
    today = client.get("/").text
    assert "Ana Souza" in today and "Não reconhecidos hoje" in today
    assert "Ainda não chegaram" not in today


def test_each_entry_shows_the_arrival_and_the_last_return(client, app):
    def recognized_at(hour: int, minute: int) -> None:
        with app.state.services.db.session() as session:
            session.add(_event("recognized", "Ana Souza", datetime.combine(date.today(), time(hour, minute))))

    recognized_at(7, 58)
    today = client.get("/").text
    assert "Chegada" in today and ">07:58</time>" in today
    assert "Retorno" not in today  # the column appears once somebody went out and came back
    recognized_at(12, 10)
    today = client.get("/").text
    assert "Retorno" in today and ">12:10</time>" in today and "vezes" not in today
    recognized_at(13, 5)
    today = client.get("/").text
    assert ">07:58</time>" in today and ">13:05</time>" in today and "2 vezes" in today


def test_a_camera_without_a_door_records_but_its_sightings_are_not_entries(client, app):
    with app.state.services.db.session() as session:
        seen = _event("recognized", "Ana Souza")
        seen.door = ""  # no door attached to the camera that saw her
        session.add(seen)
    assert "Ana Souza" not in client.get("/").text  # Today lists entries at a door
    assert "Ana Souza" in client.get("/records").text


def test_live_frame_state_is_quiet_without_the_recognition_service(client):
    assert client.get("/cameras/1/state").json() == {"tone": "idle", "label": ""}


def test_door_screen_needs_its_secret_address(client, app):
    db = app.state.services.db
    with db.session() as session:
        session.add(Camera(name="Porta", url="0", device="door"))
        session.flush()
        key = get_setting(session, "door_screen_key")
    assert client.get("/door?camera=1&key=wrong").status_code == 404
    assert client.get(f"/door?camera=1&key={key}").status_code == 200
    state = client.get(f"/door/state?camera=1&key={key}").json()
    assert state["tone"] == "bad"  # the recognition service isn't running in tests
