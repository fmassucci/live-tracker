import socket
import time
import webbrowser

import pytest
import uvicorn
from conftest import free_port

from portaria.cli import main
from portaria.preview import PreviewServer
from portaria.service import VisionService


def _config_file(tmp_path, cfg, web_port: int) -> str:
    """config.toml with cfg's folders and service port, and the web UI on this computer only, at web_port."""
    path = tmp_path / "config.toml"
    path.write_text(
        f"[paths]\ndata_dir = '{cfg.paths.data_dir.as_posix()}'\nmodels_dir = '{cfg.paths.models_dir.as_posix()}'\n"
        f"[preview]\nport = {cfg.preview.port}\n"
        f"[web]\nhost = '127.0.0.1'\nport = {web_port}\n"
    )
    return str(path)


def test_the_web_ui_opens_in_the_browser_once_it_answers(tmp_path, cfg, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    def serve(app, host: str, port: int, log_level: str) -> None:
        with socket.create_server((host, port)):
            deadline = time.monotonic() + 10
            while not opened and time.monotonic() < deadline:
                time.sleep(0.05)

    monkeypatch.setattr(uvicorn, "run", serve)
    port = free_port()
    assert main(["--config", _config_file(tmp_path, cfg, port), "web", "--open"]) == 0
    assert opened == [f"http://127.0.0.1:{port}"]


def test_a_web_ui_already_running_is_only_opened(tmp_path, cfg, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("started a second web UI"))
    with socket.create_server(("127.0.0.1", 0)) as running:
        port = running.getsockname()[1]
        assert main(["--config", _config_file(tmp_path, cfg, port), "web", "--open"]) == 0
    assert opened == [f"http://127.0.0.1:{port}"]


def test_a_second_recognition_service_does_not_start(tmp_path, cfg, monkeypatch, capsys):
    started: list[VisionService] = []
    monkeypatch.setattr(VisionService, "run", lambda self: started.append(self))
    running = PreviewServer(cfg.preview.host, cfg.preview.port)  # the first service's port
    running.start()
    try:
        assert main(["--config", _config_file(tmp_path, cfg, free_port()), "run"]) == 0
    finally:
        running.stop()
    assert started == []
    assert "already running" in capsys.readouterr().out


def test_another_program_on_the_service_port_is_reported(tmp_path, cfg, monkeypatch, capsys):
    monkeypatch.setattr(VisionService, "run", lambda self: pytest.fail("started the service"))
    with socket.create_server(("127.0.0.1", cfg.preview.port)):  # answers nothing, unlike the service
        assert main(["--config", _config_file(tmp_path, cfg, free_port()), "run"]) == 1
    assert "another program" in capsys.readouterr().out
