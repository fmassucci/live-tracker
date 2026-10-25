import socket
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from conftest import free_port

from portaria import cli
from portaria.cli import main
from portaria.preview import PreviewServer
from portaria.service import VisionService
from portaria.web.vision_client import VisionClient


def _config_file(tmp_path, cfg, web_port: int) -> str:
    """config.toml with cfg's folders and service port, and the web UI on this computer only, at web_port."""
    path = tmp_path / "config.toml"
    path.write_text(
        f"[paths]\ndata_dir = '{cfg.paths.data_dir.as_posix()}'\nmodels_dir = '{cfg.paths.models_dir.as_posix()}'\n"
        f"[preview]\nport = {cfg.preview.port}\n"
        f"[web]\nhost = '127.0.0.1'\nport = {web_port}\n"
    )
    return str(path)


def fake_server(serve: Callable[[Any], None]) -> type:
    """uvicorn's server, running serve(server) instead of the web UI."""

    class Server:
        def __init__(self, config: uvicorn.Config) -> None:
            self.config, self.should_exit, self.started = config, False, True

        def run(self) -> None:
            serve(self)

    return Server


def test_the_web_ui_opens_in_the_browser_once_it_answers(tmp_path, cfg, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    def serve(server) -> None:
        with socket.create_server((server.config.host, server.config.port)):
            deadline = time.monotonic() + 10
            while not opened and time.monotonic() < deadline:
                time.sleep(0.05)

    monkeypatch.setattr(uvicorn, "Server", fake_server(serve))
    port = free_port()
    assert main(["--config", _config_file(tmp_path, cfg, port), "web", "--open"]) == 0
    assert opened == [f"http://127.0.0.1:{port}"]


def test_stop_the_system_stops_the_web_ui(tmp_path, cfg, monkeypatch):
    stopped: list[bool] = []

    def serve(server) -> None:
        server.config.app.state.services.stop_web()  # what Stop the system, in Settings, does last
        stopped.append(server.should_exit)

    monkeypatch.setattr(uvicorn, "Server", fake_server(serve))
    assert main(["--config", _config_file(tmp_path, cfg, free_port()), "web"]) == 0
    assert stopped == [True]


def test_a_web_ui_already_running_is_only_opened(tmp_path, cfg, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(uvicorn, "Server", lambda *args, **kwargs: pytest.fail("started a second web UI"))
    with socket.create_server(("127.0.0.1", 0)) as running:
        port = running.getsockname()[1]
        assert main(["--config", _config_file(tmp_path, cfg, port), "web", "--open"]) == 0
    assert opened == [f"http://127.0.0.1:{port}"]


class Launches:
    """The programs `start` launches in the background: each answers once launched, or ends at once if it fails."""

    def __init__(self, running: set[str], failing: set[str]) -> None:
        self.started: list[str] = []
        self.running, self.failing = running, failing

    def background(self, args, command: str, logs: Path) -> tuple[SimpleNamespace, Path]:
        self.started.append(command)
        log = logs / f"{command}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if command in self.failing:
            log.write_text("Port 8000 is in use by another program\n", encoding="utf-8")
            return SimpleNamespace(poll=lambda: 1), log
        self.running.add(command)
        return SimpleNamespace(poll=lambda: None), log


@pytest.fixture
def launches(monkeypatch):
    def install(running: set[str] = frozenset(), failing: set[str] = frozenset()) -> Launches:
        fake = Launches(set(running), set(failing))
        monkeypatch.setattr(cli, "_background", fake.background)
        monkeypatch.setattr(cli, "_answers", lambda url: "web" in fake.running)
        monkeypatch.setattr(VisionClient, "status", lambda self: {} if "run" in fake.running else None)
        monkeypatch.setattr(cli, "CLOSE_AFTER_S", 0)
        return fake

    return install


def test_start_launches_both_in_the_background_and_opens_the_browser(tmp_path, cfg, monkeypatch, launches):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    fake = launches()
    port = free_port()
    assert main(["--config", _config_file(tmp_path, cfg, port), "start"]) == 0
    assert fake.started == ["run", "web"]
    assert opened == [f"http://127.0.0.1:{port}"]


def test_start_only_launches_what_is_not_running(tmp_path, cfg, monkeypatch, launches):
    monkeypatch.setattr(webbrowser, "open", lambda url: None)
    fake = launches(running={"run"})
    assert main(["--config", _config_file(tmp_path, cfg, free_port()), "start"]) == 0
    assert fake.started == ["web"]


def test_start_shows_why_a_program_did_not_start(tmp_path, cfg, monkeypatch, launches, capsys):
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("opened the browser"))
    launches(failing={"web"})
    assert main(["--config", _config_file(tmp_path, cfg, free_port()), "start"]) == 1
    out = capsys.readouterr().out
    assert "The web UI didn't start" in out and "Port 8000 is in use by another program" in out


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
