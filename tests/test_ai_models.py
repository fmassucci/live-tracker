import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeTools, place_model_file, publish_model_file
from fastapi.testclient import TestClient

from portaria.db import Camera, Event
from portaria.models import Downloads, file_state, model_state
from portaria.web.app import create_app


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
def client(cfg):
    application = create_app(cfg)
    application.state.services.people._tools = FakeTools()
    application.state.services.vision = NoVision()
    with TestClient(application) as test_client:
        test_client.post("/login", data={"username": "admin", "password": "admin"})
        yield test_client


def test_a_model_is_ready_only_while_its_file_matches_the_published_one(tmp_path, monkeypatch):
    publish_model_file(monkeypatch, "fake", b"weights")
    assert file_state(tmp_path, "fake") == "missing"
    path = tmp_path / "fake.onnx"
    path.write_bytes(b"weights")
    assert file_state(tmp_path, "fake") == "ready" and model_state(tmp_path, ("fake",)) == "ready"
    path.write_bytes(b"weightz")  # same size, other bytes
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    assert file_state(tmp_path, "fake") == "damaged" and model_state(tmp_path, ("fake",)) == "damaged"
    path.write_bytes(b"cut")
    assert file_state(tmp_path, "fake") == "damaged"
    assert model_state(tmp_path, ()) == "planned"


@pytest.fixture
def model_server():
    """Serves /good.onnx and /bad.onnx, the latter with bytes that don't match what was published."""
    content = b"x" * 3_000_000

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = content if self.path == "/good.onnx" else b"y" * len(content)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", content
    server.shutdown()
    server.server_close()


def _wait(downloads: Downloads, key: str):
    deadline = time.monotonic() + 20
    while downloads.running(key) and time.monotonic() < deadline:
        time.sleep(0.05)
    return downloads.get(key)


def test_downloads_run_in_the_background_and_keep_only_a_matching_file(tmp_path, monkeypatch, model_server):
    url, content = model_server
    publish_model_file(monkeypatch, "good", content, f"{url}/good.onnx")
    publish_model_file(monkeypatch, "bad", content, f"{url}/bad.onnx")
    finished = []
    downloads = Downloads(tmp_path, lambda: finished.append(True))

    downloads.start("objects", ("good",))
    job = _wait(downloads, "objects")
    assert job.finished and not job.error and job.done == len(content)
    assert file_state(tmp_path, "good") == "ready"

    downloads.start("plates", ("bad",))
    job = _wait(downloads, "plates")
    assert "checksum" in job.error
    assert sorted(p.name for p in tmp_path.iterdir()) == ["good.onnx"]  # nothing half-written is left
    assert finished == [True, True]  # each end tells the recognition service to look again


def test_settings_lists_every_model_and_whether_it_is_downloaded(client):
    assert 'href="/settings/models"' in client.get("/settings").text
    page = client.get("/settings/models").text
    assert "Modelos de IA" in page and "Reconhecimento facial" in page and "Pessoas e objetos" in page
    assert "Não baixado" in page  # the test's models folder is empty
    assert "Em breve" in page  # plates and fire are planned


def test_a_camera_can_only_choose_a_downloaded_model(client, cfg, monkeypatch):
    form = client.get("/cameras/new").text
    assert 'value="faces"' not in form and "Baixar outros modelos" in form
    for key in ("yunet", "auraface"):
        place_model_file(cfg.paths.models_dir, monkeypatch, key)
    assert 'value="faces"' in client.get("/cameras/new").text
    refused = client.post("/cameras/new", data={"name": "Porta", "url": "0", "analysis": "plates", "device": ""})
    assert refused.status_code == 400
    added = client.post("/cameras/new", data={"name": "Sala", "url": "0", "analysis": "", "device": ""})
    assert "Câmera Sala adicionada." in added.text and "Sem modelo de IA" in added.text


def test_a_model_is_removed_only_when_no_camera_uses_it(client, cfg, monkeypatch):
    place_model_file(cfg.paths.models_dir, monkeypatch, "yolox")
    db = client.app.state.services.db
    with db.session() as session:
        session.add(Camera(name="Depósito", url="0", analysis="objects"))
    page = client.get("/settings/models").text
    assert "Depósito" not in page  # the page tells whether a model is downloaded, not which cameras use it
    assert 'action="/settings/models/objects/remove"' in page  # those are named when someone tries
    refused = client.post("/settings/models/objects/remove")
    assert "Estas câmeras usam este modelo: Depósito." in refused.text
    assert (cfg.paths.models_dir / "yolox.onnx").exists()
    with db.session() as session:
        session.get(Camera, 1).analysis = ""
    removed = client.post("/settings/models/objects/remove")
    assert "Modelo removido: Pessoas e objetos." in removed.text
    assert not (cfg.paths.models_dir / "yolox.onnx").exists()
    assert client.post("/settings/models/faces/remove").status_code == 409  # the People page needs it


def test_a_camera_chooses_the_things_to_look_for(client, cfg, monkeypatch):
    place_model_file(cfg.paths.models_dir, monkeypatch, "yolox")
    form = client.get("/cameras/new").text
    assert 'value="objects"' in form and "O que procurar" in form and "Celular" in form and "Animais" in form
    fields = {"name": "Depósito", "url": "0", "analysis": "objects", "device": "", "confidence": "0.6",
              "min_seconds": "5", "fps": "2"}
    refused = client.post("/cameras/new", data=fields)
    assert refused.status_code == 400 and "Escolha pelo menos uma coisa para procurar." in refused.text
    client.post("/cameras/new", data={**fields, "kinds": ["person", "cell_phone", "unicorn"]})
    with client.app.state.services.db.session() as session:
        camera = session.get(Camera, 1)
        assert json.loads(camera.model_settings) == {"kinds": ["person", "cell_phone"], "confidence": 0.6,
                                                     "min_seconds": 5.0, "fps": 2.0}
    edit = client.get("/cameras/1").text
    assert 'value="cell_phone" checked' in edit and 'value="dog" checked' not in edit


def test_the_camera_page_reads_its_settings_back_as_one_sentence(client, cfg, monkeypatch):
    place_model_file(cfg.paths.models_dir, monkeypatch, "yolox")
    fields = {"name": "Caixa", "url": "0", "analysis": "objects", "device": "", "confidence": "0.35",
              "min_seconds": "3", "fps": "4"}
    client.post("/cameras/new", data={**fields, "kinds": ["person", "cell_phone", "tv"]})
    edit = client.get("/cameras/1").text
    rule = "Registra <strong>pessoa</strong>, <strong>celular</strong> e <strong>TV</strong> depois de 3\u00a0s na imagem."
    assert rule in edit
    assert 'name="confidence" value="0.35" checked' in edit  # Finds more
    client.post("/cameras/1", data={**fields, "min_seconds": "0", "kinds": ["dog"]})
    assert "Registra <strong>cachorro</strong> ao aparecer na imagem." in client.get("/cameras/1").text
    refused = client.post("/cameras/1", data=fields)  # nothing ticked
    assert refused.status_code == 400 and "Escolha pelo menos uma coisa para procurar." in refused.text
    assert 'name="kinds"' in refused.text and not re.search(r'name="kinds" value="\w+" checked', refused.text)


def test_a_camera_chooses_when_to_keep_a_photo(client, cfg, monkeypatch):
    place_model_file(cfg.paths.models_dir, monkeypatch, "yolox")
    fields = {"name": "Caixa", "url": "0", "analysis": "objects", "device": "", "kinds": ["person"], "min_seconds": "2",
              "record_mode": "interval", "every_value": "15", "every_unit": "min"}
    assert 'name="record_mode" value="appear" checked' in client.get("/cameras/new").text
    client.post("/cameras/new", data=fields)
    db = client.app.state.services.db
    with db.session() as session:
        assert (session.get(Camera, 1).record_mode, session.get(Camera, 1).record_every_s) == ("interval", 900)
    edit = client.get("/cameras/1").text
    assert 'name="record_mode" value="interval" checked' in edit and '<option value="min" selected>' in edit
    assert "Registra <strong>pessoa</strong> depois de 2\u00a0s na imagem, e de novo a cada 15\u00a0min." in edit
    refused = client.post("/cameras/1", data={**fields, "every_value": "3", "every_unit": "s"})
    assert refused.status_code == 400 and "Escolha um intervalo entre 5 segundos e 24 horas." in refused.text
    client.post("/cameras/1", data={**fields, "record_mode": "daily", "every_value": ""})
    with db.session() as session:
        assert (session.get(Camera, 1).record_mode, session.get(Camera, 1).record_every_s) == ("daily", 900)
    edit = client.get("/cameras/1").text
    assert "Registra <strong>pessoa</strong> uma vez por dia, depois de 2\u00a0s na imagem." in edit
    assert 'value="2" checked' not in edit and 'value="15"' in edit  # the interval stays, for later


def test_the_records_tabs_count_each_view(client):
    with client.app.state.services.db.session() as session:
        session.add(Event(at=datetime.now(), kind="detected", label="dog", count=1, score=0.8, door="", model="yolox",
                          liveness="none"))
    faces = client.get("/records").text
    assert 'Rostos<span class="tab-count">0</span>' in faces
    assert 'Pessoas e objetos<span class="tab-count">1</span>' in faces
    assert "Ver os registros em Pessoas e objetos" in faces  # the faces view is empty: it points to the findings


def test_records_show_the_things_seen_in_their_own_view(client):
    with client.app.state.services.db.session() as session:
        for label, count in (("person", 2), ("person", 1), ("cell_phone", 1)):
            session.add(Event(at=datetime.now(), kind="detected", label=label, count=count, score=0.87, door="",
                              model="yolox", liveness="none"))
    found = client.get("/records?view=objects").text
    assert "&times;2" in found and "87%" in found
    assert re.search(r"Tudo<span>3</span>", found) and re.search(r"Pessoa<span>2</span>", found)
    only_phones = client.get("/records?view=objects&thing=cell_phone").text
    assert "&times;2" not in only_phones and "Celular" in only_phones
    assert "&times;2" not in client.get("/records").text  # the faces view keeps to faces
    assert "Nada encontrado neste dia." in client.get("/records?view=objects&day=2020-01-01").text
