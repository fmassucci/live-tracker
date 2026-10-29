import io
import re
import threading
import zipfile
from dataclasses import replace
from datetime import date, datetime, time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from conftest import FakeTools, place_model_file
from fastapi.testclient import TestClient
from sqlalchemy import select

from live_tracker.db import (
    STOP_REQUESTS, AuditEntry, Camera, Device, DeviceCommand, DeviceInput, DeviceOutput, Event, Person, User, get_setting,
)
from live_tracker.devices.tuya import CloudDevice, CloudOutput
from live_tracker.events import face_path
from live_tracker.users import authenticate, create_user, is_admin, user_exists
from live_tracker.web.app import create_app
from live_tracker.web.common import recording_state


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


@pytest.mark.parametrize("path", ["/", "/people", "/people/new", "/records", "/cameras", "/cameras/new", "/live", "/settings",
                                  "/devices", "/devices/tuya"])
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
        data={"name": "João da Silva", "category": "employee", "job_title": "Padeiro", "access": "true", "informed": "true"},
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
    monkeypatch.setattr("live_tracker.web.common.local_ipv4", lambda: ["192.168.0.19", "10.0.0.5"])
    text = client.get("/settings").text
    assert 'value="http://127.0.0.1:8000"' in text
    assert 'value="http://192.168.0.19:8000"' in text and 'value="http://10.0.0.5:8000"' in text
    assert "Se o primeiro endereço não abrir, tente o próximo." in text


def test_the_recognition_screen_address_is_the_one_the_tablet_opens(client, app, monkeypatch):
    monkeypatch.setattr("live_tracker.web.common.local_ipv4", lambda: ["192.168.0.19"])
    with app.state.services.db.session() as session:
        session.add(Camera(name="Porta", url="0", analysis="faces"))
        session.add(Camera(name="Caixa", url="1", analysis="objects"))  # recognizes nobody: no screen
        session.flush()
        key = get_setting(session, "screen_key")
    settings = client.get("/settings").text
    assert f'value="http://192.168.0.19:8000/screen?camera=1&key={key}"' in settings
    assert "/screen?camera=2" not in settings


def test_settings_says_when_only_this_computer_can_open_the_app(client, cfg):
    cfg.web.host = "127.0.0.1"
    text = client.get("/settings").text
    assert 'value="http://127.0.0.1:8000"' in text
    assert "Só este computador consegue acessar o sistema." in text


def _index(text):
    """Where the list beside the menu jumps to."""
    return re.findall(r'<a href="#([\w-]+)" data-for="\1">', text)


def test_settings_lists_its_parts_beside_the_menu(app, client):
    general, models = client.get("/settings").text, client.get("/settings/models").text
    assert "Nesta página" in general and "Nesta página" in models
    assert _index(general) == ["identity", "accounts", "system", "data"]
    assert _index(models)[0] == "model-faces" and _index(models)[-1] == "planned"  # each model, then Coming soon
    assert len(_index(models)) == models.count('class="panel model-card"') + 1
    for text in (general, models):
        assert all(f'id="{part}"' in text for part in _index(text))
    with app.state.services.db.session() as session:
        create_user(session, "caixa", "senha-do-caixa")
    with TestClient(app) as cashier:
        cashier.post("/login", data={"username": "caixa", "password": "senha-do-caixa"})
        assert _index(cashier.get("/settings").text) == ["identity", "accounts", "system"]  # no Database


@pytest.fixture
def opened(monkeypatch):
    """Folders the Settings button showed, instead of showing them on the screen."""
    folders = []
    monkeypatch.setattr("live_tracker.web.data.open_folder", folders.append)
    monkeypatch.setattr("live_tracker.web.common.local_ipv4", lambda: ["192.168.0.19"])
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


class StoppingVision(NoVision):
    """A recognition service that still answers a few times once it's asked to stop."""

    def __init__(self, answers: int) -> None:
        self.answers = answers

    def status(self):
        self.answers -= 1
        return {"cameras": {}} if self.answers >= 0 else None


@pytest.fixture
def stops(app, monkeypatch):
    """Each time Stop the system stopped the web UI."""
    stopped = []
    app.state.services.stop_web = lambda: stopped.append(True)
    monkeypatch.setattr("live_tracker.web.settings.WEB_STOP_DELAY_S", 0)
    return stopped


def test_stop_the_system_stops_the_service_and_then_the_web_ui(app, stops):
    with TestClient(app, client=("127.0.0.1", 50000)) as here:
        here.post("/login", data={"username": "admin", "password": "admin"})
        assert 'action="/settings/stop"' in here.get("/settings").text
        app.state.services.vision = StoppingVision(answers=3)
        response = here.post("/settings/stop", data={"confirm": "true"})
    assert response.status_code == 200 and "O sistema está desligado" in response.text
    assert app.state.services.vision.answers == -1  # the page came once the service no longer answered
    assert stops == [True]
    with app.state.services.db.session() as session:
        assert get_setting(session, STOP_REQUESTS) == "1"
        assert session.scalars(select(AuditEntry.action).order_by(AuditEntry.id.desc())).first() == "system stopped"


def test_stopping_says_when_the_service_takes_a_while(app, stops, monkeypatch):
    monkeypatch.setattr("live_tracker.web.settings.SERVICE_STOP_WAIT_S", 0)
    with TestClient(app, client=("127.0.0.1", 50000)) as here:
        here.post("/login", data={"username": "admin", "password": "admin"})
        app.state.services.vision = StoppingVision(answers=100)
        text = here.post("/settings/stop", data={"confirm": "true"}).text
    assert "O sistema está sendo desligado" in text and "termina sozinho em até um minuto" in text
    assert stops == [True]  # the web UI stops all the same


def test_only_an_administrator_on_this_computer_stops_the_system(app, client, stops):
    text = client.get("/settings").text  # an administrator, on a phone
    assert 'action="/settings/stop"' not in text
    assert "Para desligar o sistema, acesse as Configurações no computador onde ele está instalado." in text
    assert client.post("/settings/stop", data={"confirm": "true"}).status_code == 403
    with app.state.services.db.session() as session:
        create_user(session, "caixa", "senha-do-caixa")
    with TestClient(app, client=("127.0.0.1", 50000)) as cashier:
        cashier.post("/login", data={"username": "caixa", "password": "senha-do-caixa"})
        assert 'id="stop"' not in cashier.get("/settings").text
        assert cashier.post("/settings/stop", data={"confirm": "true"}).status_code == 403
    with TestClient(app, client=("127.0.0.1", 50000)) as here:
        here.post("/login", data={"username": "admin", "password": "admin"})
        response = here.post("/settings/stop", follow_redirects=False)  # the box not ticked
        assert response.headers["location"] == "/settings#stop"
    assert stops == []
    with app.state.services.db.session() as session:
        assert get_setting(session, STOP_REQUESTS) is None


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


def test_an_administrator_renames_an_account_to_a_name_nobody_has(app, client):
    with app.state.services.db.session() as session:
        create_user(session, "caixa", "senha-do-caixa")
        create_user(session, "gerente", "senha-do-gerente")
    with app.state.services.db.session() as session:
        cashier_id = session.scalar(select(User.id).where(User.username == "caixa"))
    assert 'name="username" value="caixa"' in client.get("/settings").text
    with TestClient(app) as cashier:
        cashier.post("/login", data={"username": "caixa", "password": "senha-do-caixa"})
        taken = client.post(f"/settings/users/{cashier_id}", data={"username": "Gerente", "password": "nova-senha-1"})
        assert "Este nome de usuário já existe." in taken.text
        with app.state.services.db.session() as session:  # nothing was saved, not even the password
            assert authenticate(session, "caixa", "senha-do-caixa")
        renamed = client.post(f"/settings/users/{cashier_id}", data={"username": " caixa.ana "})
        assert "Conta caixa.ana salva." in renamed.text
        assert cashier.get("/people", follow_redirects=False).status_code == 303  # signs in again, with the new name
    with app.state.services.db.session() as session:
        assert authenticate(session, "caixa.ana", "senha-do-caixa") and not user_exists(session, "caixa")
        changes = session.scalars(select(AuditEntry.detail).where(AuditEntry.action == "account changed")).all()
        assert changes == ["caixa: renamed to caixa.ana"]
    assert "Use de 3 a 60 letras" in client.post(f"/settings/users/{cashier_id}", data={"username": "a b"}).text


def _record(app, cfg, at: datetime, kind: str = "recognized", name: str | None = "Ana") -> str:
    """A record with its picture and face crop on disk, as the recognition service leaves them."""
    snapshot = f"snapshots/{at:%Y-%m-%d}/{at:%H%M%S_%f}_{kind}.jpg"
    for path in (cfg.paths.data_dir / snapshot, cfg.paths.data_dir / face_path(snapshot)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"picture")
    with app.state.services.db.session() as session:
        session.add(Event(at=at, kind=kind, person_name=name, score=0.712, snapshot=snapshot, action="shadow",
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
        "Data;Hora;Pessoa;Resultado;Câmera;Dispositivo;Semelhança;Imagem",
        f"2026-10-22;07:58:00;Ana;Reconhecido;;modo de teste, nada acionado;0,71;{first}",
        f"2026-10-23;08:01:00;;Não reconhecido;;modo de teste, nada acionado;0,71;{second}",
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
    camera = SimpleNamespace(enabled=True, analysis="faces")
    assert recording_state(camera, {"online": True}, service_up=True) == "recording"
    assert recording_state(camera, {"online": False}, service_up=True) == "no-image"
    assert recording_state(camera, None, service_up=False) == "service-down"
    assert recording_state(SimpleNamespace(enabled=False), {"online": True}, service_up=True) == "stopped"
    assert recording_state(camera, {"online": True, "model_missing": True}, service_up=True) == "no-model"
    assert recording_state(SimpleNamespace(enabled=True, analysis=""), {"online": True}, service_up=True) == "live"


def test_a_camera_has_an_ai_model_and_an_attached_device(client, app, cfg, monkeypatch):
    for key in ("yunet", "auraface"):  # face recognition is offered once downloaded
        place_model_file(cfg.paths.models_dir, monkeypatch, key)
    form = client.get("/cameras/new").text
    assert "Modelo de IA" in form and "Reconhecimento facial" in form and "Dispositivo ligado" in form
    assert "Nenhum dispositivo ainda" in form
    client.post("/cameras/new", data={"name": "Cozinha", "url": "0", "analysis": "faces"})
    with app.state.services.db.session() as session:
        output_id = _output(session)
    cameras = client.post("/cameras/new", data={"name": "Porta", "url": "1", "analysis": "faces",
                                                "output_id": str(output_id)})
    assert cameras.text.count("Parar gravação") == 2  # every camera records
    assert cameras.text.count("Aciona Módulo, Fechadura") == 1
    with app.state.services.db.session() as session:
        assert [c.output_id for c in session.scalars(select(Camera).order_by(Camera.id))] == [None, output_id]


def _output(session) -> int:
    """A module's channel for a camera to switch."""
    device = Device(kind="tuya", uid="eb2002", name="Módulo",
                    outputs=[DeviceOutput(key="1", name="Fechadura", kind="lock", timer_dp=9)])
    session.add(device)
    session.flush()
    return device.outputs[0].id


def test_a_camera_switches_a_devices_output(client, app):
    with app.state.services.db.session() as session:
        session.add(Device(kind="tuya", uid="eb2002", name="Módulo", outputs=[
            DeviceOutput(key="1", name="Fechadura", kind="lock", pulse_ms=3000, timer_dp=9),
            DeviceOutput(key="2", name="Luz", kind="light", timer_dp=10)]))
        session.add(Camera(id=1, name="Porta", url="0", analysis="faces"))
        session.add(Camera(id=2, name="Fundos", url="1", analysis=""))
        session.flush()
        lock_id = session.scalar(select(DeviceOutput.id).where(DeviceOutput.key == "1"))
    form = client.get("/cameras/1").text
    assert "Nenhuma saída" in form and '<optgroup label="Módulo">' in form and "Fechadura, 3 s" in form
    assert "door" not in form.lower()
    client.post("/cameras/1", data={"name": "Porta", "url": "0", "analysis": "faces", "output_id": str(lock_id)})
    client.post("/cameras/2", data={"name": "Fundos", "url": "1", "analysis": "",
                                    "output_id": str(lock_id)})  # left in the hidden choice: only faces switch it
    assert "Aciona Módulo, Fechadura" in client.get("/cameras").text
    with app.state.services.db.session() as session:
        assert [c.output_id for c in session.scalars(select(Camera).order_by(Camera.id))] == [lock_id, None]
        session.delete(session.get(Device, session.scalar(select(Device.id))))
    with app.state.services.db.session() as session:
        assert session.get(Camera, 1).output_id is None  # its device was removed
    gone = client.post("/cameras/1", data={"name": "Porta", "url": "0", "analysis": "faces",
                                           "output_id": str(lock_id)})
    assert gone.status_code == 400 and "Essa saída foi removida" in gone.text


def test_a_camera_can_be_stopped_and_started_from_its_settings(client, app):
    with app.state.services.db.session() as session:
        session.add(Camera(name="Porta", url="0"))
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
        session.add(Camera(name="Jortan", url="rtsp://admin:segredo@192.168.0.38:554/onvif1"))
    edit = client.get("/cameras/1").text
    assert "Separar em duas câmeras" not in edit and "Metade de cima" not in edit


def test_a_two_lens_camera_can_be_split_into_one_camera_per_lens(client, app):
    app.state.services.vision = SeenVision("1920x2160")  # two 1920x1080 lenses, one above the other
    with app.state.services.db.session() as session:
        output_id = _output(session)
        session.add(Camera(name="Jortan", url="rtsp://admin:segredo@192.168.0.37:554/onvif1", output_id=output_id))
    assert "Separar em duas câmeras" in client.get("/cameras/1").text
    cameras = client.post("/cameras/1/split")
    assert "Jortan agora são duas câmeras, uma para cada lente." in cameras.text
    assert cameras.text.count("Parar gravação") == 2  # both lenses record
    with app.state.services.db.session() as session:
        top, bottom = session.scalars(select(Camera).order_by(Camera.id)).all()
        assert (top.name, top.part, top.output_id) == ("Jortan (cima)", "top", output_id)
        assert (bottom.name, bottom.part, bottom.output_id) == ("Jortan (baixo)", "bottom", None)  # switched once
        assert bottom.url == top.url  # the same address: the camera is still read once
    edit = client.get("/cameras/1").text
    assert "Separar em duas câmeras" not in edit and "Metade de cima" in edit


def test_open_pages_can_follow_every_camera(client, app):
    with app.state.services.db.session() as session:
        session.add(Camera(name="Porta", url="0"))
    states = client.get("/cameras-states").json()
    assert states == {"1": {"state": "service-down", "label": "Serviço de reconhecimento parado"}}


def _event(kind: str, name: str | None, at: datetime | None = None) -> Event:
    return Event(at=at or datetime.now(), kind=kind, person_name=name, score=0.7, action="shadow", model="auraface",
                 liveness="none")


def _at_camera(event: Event, camera_id: int) -> Event:
    event.camera_id = camera_id
    return event


def test_today_lists_who_came_in_and_shows_faces_not_recognized_only_when_there_are_some(client, app):
    with app.state.services.db.session() as session:
        session.add(Camera(id=1, name="Porta", url="0", analysis="faces"))
    assert "Não reconhecidos hoje" not in client.get("/").text
    with app.state.services.db.session() as session:
        session.add_all([_at_camera(_event("recognized", "Ana Souza"), 1), _at_camera(_event("unknown", None), 1)])
    today = client.get("/").text
    assert "Ana Souza" in today and "Não reconhecidos hoje" in today
    assert "Ainda não chegaram" not in today


def test_each_person_shows_when_they_arrived_and_were_last_seen(client, app):
    with app.state.services.db.session() as session:
        session.add(Camera(id=1, name="Porta", url="0", analysis="faces"))

    def recognized_at(hour: int, minute: int) -> None:
        with app.state.services.db.session() as session:
            at = datetime.combine(date.today(), time(hour, minute))
            session.add(_at_camera(_event("recognized", "Ana Souza", at), 1))

    recognized_at(7, 58)
    today = client.get("/").text
    assert "Chegou" in today and ">07:58</time>" in today
    assert "Última vez" not in today  # the column appears once somebody was seen again
    recognized_at(12, 10)
    recognized_at(13, 5)
    today = client.get("/").text
    assert "Última vez" in today and ">07:58</time>" in today and ">13:05</time>" in today
    assert ">12:10</time>" not in today


def test_each_face_camera_lists_the_people_it_recognized(client, app):
    with app.state.services.db.session() as session:
        session.add_all([Camera(id=1, name="Entrada", url="0", analysis="faces", output_id=_output(session)),
                         Camera(id=2, name="Cozinha", url="1", analysis="faces")])
        session.flush()
        for at, camera_id in ((time(7, 58), 1), (time(13, 40), 2)):  # the kitchen switches nothing
            session.add(_at_camera(_event("recognized", "Ana Souza", datetime.combine(date.today(), at)), camera_id))
    today = client.get("/").text
    entrance, kitchen = today.split('id="camera-2"')  # a camera that switches a device comes first, as on Cameras
    assert ">07:58</time>" in entrance and ">13:40</time>" not in entrance
    assert ">13:40</time>" in kitchen and ">07:58</time>" not in kitchen
    assert 'href="#camera-1"' in entrance and 'href="#camera-2"' in entrance  # the list of cameras beside the menu
    with app.state.services.db.session() as session:
        session.get(Camera, 2).enabled, session.get(Camera, 2).analysis = False, ""
    assert 'href="#camera-1"' not in client.get("/").text  # one camera: no list


def test_a_camera_shows_what_it_recorded_today_also_with_an_earlier_model(client, app):
    with app.state.services.db.session() as session:
        session.add(Camera(id=1, name="Salão", url="0", analysis="falls"))  # it recognized faces this morning
        session.flush()
        session.add(_at_camera(_event("recognized", "Ana Souza"), 1))
        session.add(Event(at=datetime.now(), kind="detected", label="fall", count=1, score=0.8, camera_id=1, action="",
                          model="yolov8s-pose", liveness="none"))
    today = client.get("/").text
    assert "Ana Souza" in today and "Pessoas hoje" in today
    assert "Encontrado hoje" in today and "Queda<span>1</span>" in today
    assert "Só a imagem ao vivo" not in today


class OnlineVision(NoVision):
    """The recognition service receives every camera's picture."""

    def status(self):
        return {"cameras": {str(i): {"online": True, "size": "1280x720", "fps": 15.0, "state": "idle"}
                            for i in range(1, 10)}}


def test_today_shows_every_camera_with_a_model_or_turned_on(client, app):
    with app.state.services.db.session() as session:
        session.add_all([
            Camera(id=1, name="Caixa", url="0", analysis="objects"),
            Camera(id=2, name="Fundos", url="1", analysis=""),  # only the picture
            Camera(id=3, name="Garagem", url="2", analysis="", enabled=False),  # off, without a model: not shown
            Camera(id=4, name="Porta", url="3", analysis="faces", output_id=_output(session), enabled=False),  # stopped
        ])
        session.flush()
        for label, count in (("person", 1), ("person", 2), ("dining_table", 1)):
            session.add(Event(at=datetime.now(), kind="detected", label=label, count=count, score=0.8, camera_id=1,
                              action="", model="yolox", liveness="none"))
    app.state.services.vision = OnlineVision()
    today = client.get("/").text
    assert "Caixa" in today and "Fundos" in today and "Porta" in today and "Garagem" not in today
    assert _index(today) == ["camera-4", "camera-1", "camera-2"]  # beside the menu, switching first as on Cameras
    assert re.search(r"Pessoa<span>2</span>.*Mesa<span>1</span>", today, re.S)  # what it found, the most first
    assert "Encontrado hoje" in today and "Só a imagem ao vivo" in today and "Iniciar gravação" in today
    assert today.count('data-stream-for="') == 2  # a few cameras play as video
    with app.state.services.db.session() as session:
        session.add_all([Camera(id=i, name=f"Extra {i}", url=str(i), analysis="") for i in (5, 6, 7)])
    today = client.get("/").text
    assert 'data-stream-for="' not in today and today.count('data-every="1000"') == 5  # more: a picture a second


def test_people_show_when_any_face_camera_last_saw_them(client, app):
    db = app.state.services.db
    with db.session() as session:
        ana = Person(name="Ana Souza")
        session.add(ana)
        session.flush()
        seen = _event("recognized", "Ana Souza", datetime(2026, 10, 20, 9, 30))
        seen.person_id, seen.action = ana.id, ""  # a camera without a device
        session.add(seen)
    assert "Visto por último: 20/10 09:30" in client.get("/people").text


def test_cameras_show_when_each_keeps_a_photo_and_live_opens_the_settings(client, app):
    with app.state.services.db.session() as session:
        session.add_all([Camera(id=1, name="Caixa", url="0", analysis="objects", record_mode="interval",
                                record_every_s=900),
                         Camera(id=2, name="Porta", url="1", analysis="faces", record_mode="daily"),
                         Camera(id=3, name="Fundos", url="2", analysis="objects", record_mode="interval",
                                record_every_s=3600, record_while_present=False)])
    cameras = client.get("/cameras").text
    assert "A cada 15\u00a0min</span>" in cameras and "Primeira vez do dia" in cameras
    assert "A cada 1\u00a0h, ao voltar</span>" in cameras
    assert 'href="/cameras/1"' in client.get("/live/1").text and "Editar câmera" in client.get("/live/1").text


def test_live_frame_state_is_quiet_without_the_recognition_service(client):
    assert client.get("/cameras/1/state").json() == {"tone": "idle", "label": ""}


def test_the_recognition_screen_needs_its_secret_address(client, app):
    db = app.state.services.db
    with db.session() as session:
        session.add(Camera(name="Porta", url="0"))
        session.flush()
        key = get_setting(session, "screen_key")
    assert client.get("/screen?camera=1&key=wrong").status_code == 404
    assert client.get(f"/screen?camera=1&key={key}").status_code == 200
    state = client.get(f"/screen/state?camera=1&key={key}").json()
    assert state["tone"] == "bad"  # the recognition service isn't running in tests


TUYA_MODULE = CloudDevice(uid="eb2002", name="Módulo Mini Ms-107 2 Canais Wi-Fi", model="MS-107 2C WF",
                          local_key="chave-local-1234", outputs=(CloudOutput("1", 1, 9), CloudOutput("2", 2, 10)),
                          online=True, power_on="on")


@pytest.fixture
def tuya_account(monkeypatch):
    """The Tuya account as the developer project lists it; `module` is what it says about the one module."""
    account = SimpleNamespace(module=TUYA_MODULE, asked=[])

    def cloud_devices(access_id, secret, region):
        account.asked.append((access_id, secret, region))
        return [account.module]

    monkeypatch.setattr("live_tracker.web.devices.cloud_devices", cloud_devices)
    return account


def test_tuya_modules_are_imported_with_an_output_per_channel_and_the_secret_is_never_shown(app, client, tuya_account):
    page = client.post("/devices/tuya", data={"access_id": "h7e5geh8", "secret": "segredo-do-projeto", "region": "us"})
    assert page.status_code == 200 and "Módulo Mini Ms-107 2 Canais Wi-Fi" in page.text and "2 canais" in page.text
    assert "os relés ligam" in page.text  # it switches its relays on after a power cut
    assert tuya_account.asked == [("h7e5geh8", "segredo-do-projeto", "us")]
    assert client.post("/devices/tuya/import", data={"uids": "eb2002"}).status_code == 200
    with app.state.services.db.session() as session:
        device = session.scalar(select(Device))
        assert device.kind == "tuya" and device.local_key == "chave-local-1234" and device.model == "MS-107 2C WF"
        assert [(o.key, o.name, o.timer_dp) for o in device.outputs] == [("1", "Canal 1", 9), ("2", "Canal 2", 10)]
        device.outputs[0].name = "Fechadura"
    for path in ("/devices", "/devices/tuya", f"/devices/{device.id}"):
        text = client.get(path).text
        assert "segredo-do-projeto" not in text and "chave-local-1234" not in text
    # Paired again: a new key. Importing again keeps the names, and an empty Secret keeps the saved one.
    tuya_account.module = replace(TUYA_MODULE, local_key="chave-nova-5678")
    client.post("/devices/tuya", data={"access_id": "h7e5geh8", "secret": "", "region": "us"})
    client.post("/devices/tuya/import", data={"uids": "eb2002"})
    assert tuya_account.asked[-1] == ("h7e5geh8", "segredo-do-projeto", "us")
    with app.state.services.db.session() as session:
        device = session.scalar(select(Device))
        assert device.local_key == "chave-nova-5678" and [o.name for o in device.outputs] == ["Fechadura", "Canal 2"]


def _device(app, kind: str = "tuya") -> Device:
    with app.state.services.db.session() as session:
        device = Device(kind=kind, uid=f"{kind}-1", name="Porta", new=kind == "esp32",
                        outputs=[DeviceOutput(key="1", name="Canal 1", timer_dp=9, max_ms=3_600_000)],
                        inputs=[DeviceInput(key="button1", name="button1")] if kind == "esp32" else [])
        session.add(device)
    return device


def test_a_device_and_its_outputs_are_named_and_checked(app, client):
    device = _device(app, "esp32")
    page = client.get("/devices").text
    assert "Novo" in page and "Dar nome" in page
    [output], [board_input] = device.outputs, device.inputs
    form = {"name": "Porta dos funcionários", "output_id": output.id, "output_name": "Fechadura",
            "output_kind": "lock", "output_seconds": "3", "input_id": board_input.id, "input_name": "Botão de saída"}
    assert client.post(f"/devices/{device.id}", data={**form, "output_seconds": "2.5"}).status_code == 400
    assert client.post(f"/devices/{device.id}", data={**form, "output_kind": "door"}).status_code == 400
    assert client.post(f"/devices/{device.id}", data=form).status_code == 200
    with app.state.services.db.session() as session:
        saved = session.get(Device, device.id)
        assert saved.name == "Porta dos funcionários" and not saved.new
        assert (saved.outputs[0].name, saved.outputs[0].kind, saved.outputs[0].pulse_ms) == ("Fechadura", "lock", 3000)
        assert saved.inputs[0].name == "Botão de saída"


def test_a_tuya_module_takes_a_fixed_address_only_as_four_numbers(app, client):
    device = _device(app)
    form = {"name": "Porta", "output_id": device.outputs[0].id, "output_name": "Fechadura", "output_kind": "lock",
            "output_seconds": "3"}
    assert client.post(f"/devices/{device.id}", data={**form, "address": "modulo.local"}).status_code == 400
    client.post(f"/devices/{device.id}", data={**form, "address": "192.168.100.102"})
    with app.state.services.db.session() as session:
        assert session.get(Device, device.id).address == "192.168.100.102"


def test_only_administrators_change_or_test_devices(app, client):
    device = _device(app)
    with app.state.services.db.session() as session:
        create_user(session, "caixa", "senha-do-caixa")
    with TestClient(app) as cashier:
        cashier.post("/login", data={"username": "caixa", "password": "senha-do-caixa"})
        text = cashier.get("/devices").text
        assert "Porta" in text and "/outputs/" not in text and 'href="/devices/tuya"' not in text
        assert cashier.get(f"/devices/{device.id}").status_code == 403
        assert cashier.post(f"/devices/{device.id}", data={"name": "Outra"}).status_code == 403
        assert cashier.post(f"/devices/{device.id}/delete", data={"confirm": "true"}).status_code == 403
        assert cashier.post(f"/devices/{device.id}/outputs/{device.outputs[0].id}/test").status_code == 403
        assert cashier.get("/devices/tuya").status_code == 403
    with app.state.services.db.session() as session:
        assert session.scalars(select(DeviceCommand)).all() == []


def test_testing_an_output_needs_the_recognition_service(app, client):
    device = _device(app)
    response = client.post(f"/devices/{device.id}/outputs/{device.outputs[0].id}/test")
    assert "Não deu para acionar Canal 1" in response.text
    assert "serviço de reconhecimento não está rodando" in response.text


class DeviceVision(NoVision):
    """A recognition service that carries out every command the page adds, as devices/manager.py does."""

    def __init__(self, db, result: str = "ok", error: str = "") -> None:
        self.db, self.result, self.error = db, result, error
        self.carried_out: list[tuple[str, int]] = []
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def status(self):
        return {"cameras": {}, "devices": {}, "broker": "off"}

    def _run(self):
        while not self.stop.wait(0.05):
            with self.db.session() as session:
                for command in session.scalars(select(DeviceCommand).where(DeviceCommand.result == "")):
                    command.result, command.error, command.done_at = self.result, self.error, datetime.now()
                    self.carried_out.append((command.output_key, command.ms))


def test_testing_an_output_pulses_it_for_a_second_and_says_how_it_went(app, client):
    device = _device(app)
    url = f"/devices/{device.id}/outputs/{device.outputs[0].id}/test"
    vision = app.state.services.vision = DeviceVision(app.state.services.db)
    response = client.post(url)
    vision.stop.set()
    assert "Canal 1: ligou por um segundo" in response.text and vision.carried_out == [("1", 1000)]
    vision = app.state.services.vision = DeviceVision(app.state.services.db, "failed", "offline")
    response = client.post(url)
    vision.stop.set()
    assert "Não deu para acionar Canal 1" in response.text and "O dispositivo está offline." in response.text
    with app.state.services.db.session() as session:
        tests = session.scalars(select(AuditEntry).where(AuditEntry.action == "output tested")).all()
    assert [entry.detail for entry in tests] == ["Porta: Canal 1", "Porta: Canal 1"]


def test_removing_a_device_needs_the_confirmation(app, client):
    device = _device(app)
    client.post(f"/devices/{device.id}/delete")
    with app.state.services.db.session() as session:
        assert session.get(Device, device.id) is not None
    client.post(f"/devices/{device.id}/delete", data={"confirm": "true"})
    with app.state.services.db.session() as session:
        assert session.get(Device, device.id) is None and session.scalars(select(DeviceOutput)).all() == []
