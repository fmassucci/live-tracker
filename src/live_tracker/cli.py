"""Command line: live-tracker <command> --help"""

from __future__ import annotations

import argparse
import errno
import getpass
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from contextlib import suppress
from pathlib import Path

from live_tracker.config import RECOGNITION_MODELS, Config, load_config, rename_legacy_database

START_TIMEOUT_S = 90.0  # for `start`: after the computer starts, loading the AI models can take a while
CLOSE_AFTER_S = 5.0  # how long `start` still shows that the system runs, before its window closes


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # read when cv2 is first imported
    # Windows webcams (Media Foundation) take several seconds to open with hardware transforms on,
    # about one second without them. Also read when cv2 is first imported.
    os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    cfg = load_config(args.config)
    return _COMMANDS[args.command](cfg, args) or 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-tracker", description="Face recognition at the staff entrance.")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="settings file (default: config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("download-models", help="download the face models into models/")

    p = commands.add_parser("discover", help="find ONVIF cameras on the network and list their RTSP URLs")
    p.add_argument("--user", help="camera user; with it, the RTSP URLs of each camera are listed")
    p.add_argument("--password", help="camera password (asked for when omitted)")

    p = commands.add_parser("probe", help="open a camera stream, print its properties and save one frame")
    p.add_argument("--source", help="RTSP URL, video file or webcam index (default: [camera] source)")

    p = commands.add_parser("run", help="start the recognition service (all cameras from the database)")
    p.add_argument("--source", help="use only this camera instead of the database's, for tests")

    p = commands.add_parser("web", help="start the web UI")
    p.add_argument("--open", action="store_true",
                   help="open it in the default browser once it answers; when it already runs, only open it")

    commands.add_parser("start", help="start the recognition service and the web UI in the background, without "
                        "windows, and open the browser; Stop the system, in Settings, stops them")

    p = commands.add_parser("create-user", help="create a web UI account")
    p.add_argument("username")
    p.add_argument("--password", help="asked for when omitted")
    p.add_argument("--admin", action="store_true", help="can add accounts, and download and delete records")

    commands.add_parser("import", help="import the first setup's photo folders, events and camera")

    p = commands.add_parser("evaluate", help="compare the models on photo folders in data/enroll/ and suggest a threshold")
    p.add_argument("--outsiders", type=Path, help="folder with photos of people who are NOT enrolled")
    return parser


def _download_models(cfg: Config, args: argparse.Namespace) -> int:
    from live_tracker.models import download_models

    download_models(cfg.paths.models_dir)
    return 0


def _discover(cfg: Config, args: argparse.Namespace) -> int:
    from live_tracker import onvif

    cameras = onvif.discover()
    if not cameras:
        print("No ONVIF camera answered. Check that the camera is on this network, that ONVIF is enabled "
              "in its app, and that the firewall lets Python receive UDP.")
        return 1
    password = args.password
    if args.user and password is None:
        password = getpass.getpass(f"password for {args.user}: ")
    for ip, urls in cameras.items():
        print(f"{ip}  {' '.join(urls)}")
        if not args.user:
            continue
        try:
            for name, url in onvif.stream_urls(urls[0], args.user, password or ""):
                print(f"    {name}: {url}")
        except OSError as e:
            print(f"    could not list streams: {e} (wrong password, or camera clock far off?)")
    if not args.user:
        print("\nAdd --user to list each camera's RTSP URLs.")
    return 0


def _probe(cfg: Config, args: argparse.Namespace) -> int:
    from live_tracker.camera import probe, redact
    from live_tracker.images import write_jpeg

    source = args.source or cfg.camera.source
    if not source:
        print("Give a --source, or set [camera] source in config.toml")
        return 1
    print(f"source: {redact(source)}")
    info, image = probe(source)
    if not info["opened"]:
        print("could not open it: wrong URL or password, or the camera has no RTSP")
        return 1
    for key in ("size", "codec", "transport", "reported_fps", "measured_fps"):
        if info[key] != "":  # transport is empty for webcams and files
            print(f"{key}: {info[key]}")
    if image is not None:
        path = cfg.paths.data_dir / "probe.jpg"
        write_jpeg(path, image)
        print(f"frame saved to {path}")
    return 0


def _run(cfg: Config, args: argparse.Namespace) -> int:
    from live_tracker.service import VisionService
    from live_tracker.web.vision_client import VisionClient

    try:
        service = VisionService(cfg, source=args.source)
    except OSError as e:
        # On Windows, a port another program holds exclusively fails with WinError 10013 (access denied), not "in use".
        if e.errno != errno.EADDRINUSE and getattr(e, "winerror", None) != 10013:
            raise
        if VisionClient(cfg.preview).status() is None:
            print(f"Port {cfg.preview.port} is in use by another program: set another [preview] port in config.toml.")
            return 1
        print(f"The recognition service is already running (port {cfg.preview.port}).")
        return 0
    service.run()
    return 0


def _web(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from live_tracker.web.app import create_app
    from live_tracker.web.common import web_addresses

    here, others = web_addresses(cfg)
    if args.open and _answers(here):
        print(f"web UI already running at {here}")
        webbrowser.open(here)
        return 0
    print(f"web UI at {here}" + ("  (Ctrl+C to stop)" if sys.stdout.isatty() else ""))  # not when started by start
    if others and others != [here]:
        print(f"from other devices on the network: {' or '.join(others)}")
    if args.open:
        threading.Thread(target=_open_once_answering, args=(here,), name="browser", daemon=True).start()
    app = create_app(cfg)
    # Live pictures stay open on phones and tablets: stopping waits for them a few seconds at most.
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.web.host, port=cfg.web.port, log_level="warning",
                                           timeout_graceful_shutdown=5))

    def stop() -> None:  # Stop the system, in Settings
        server.should_exit = True

    app.state.services.stop_web = stop
    try:
        server.run()
    except KeyboardInterrupt:  # Ctrl+C: uvicorn stops, then raises it again
        pass
    return 0 if server.started else 1


def _start(cfg: Config, args: argparse.Namespace) -> int:
    """What the desktop shortcut runs: starts whichever of the recognition service and the web UI isn't running yet,
    each in the background without a window, and opens the browser once both answer. Their messages go to
    data/logs/. When one doesn't start, says why and returns 1, so start.bat keeps the window open."""
    from live_tracker.web.common import web_addresses
    from live_tracker.web.vision_client import VisionClient

    here, _ = web_addresses(cfg)
    vision = VisionClient(cfg.preview)
    logs = cfg.paths.data_dir / "logs"
    answering = {"run": lambda: vision.status() is not None, "web": lambda: _answers(here)}
    names = {"run": "recognition service", "web": "web UI"}
    missing = [command for command, answers in answering.items() if not answers()]
    if len(missing) == len(answering):  # nothing runs, so nothing has the database open
        rename_legacy_database(cfg)
    starting = {}
    for command in missing:
        print(f"Starting the {names[command]}...")
        starting[command] = _background(args, command, logs)
    deadline = time.monotonic() + START_TIMEOUT_S
    while starting:
        for command, (process, log) in list(starting.items()):
            if answering[command]():
                del starting[command]
            elif process.poll() is not None or time.monotonic() > deadline:
                print(f"The {names[command]} didn't start. Its last messages, from {log}:\n{_tail(log)}")
                return 1
        if starting:
            time.sleep(0.5)
    webbrowser.open(here)
    print(f"Running at {here}. This window closes by itself; to stop the system, use Stop the system in Settings.")
    time.sleep(CLOSE_AFTER_S)
    return 0


def _background(args: argparse.Namespace, command: str, logs: Path) -> tuple[subprocess.Popen, Path]:
    """`python -m live_tracker <command>` without a window, outliving this process. Its messages go to
    <logs>/<command>.log; the previous start's are kept in <command>.previous.log."""
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{command}.log"
    with suppress(OSError):
        log.replace(logs / f"{command}.previous.log")
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    with open(log, "wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "live_tracker", "--config", str(args.config.resolve()),
             *(["--verbose"] if args.verbose else []), command],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
            start_new_session=sys.platform != "win32", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    return process, log


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no messages)"


def _answers(url: str) -> bool:
    """Whether something listens at the url's address. The timeout is short because on Windows, a connection
    to a port nobody listens on takes two seconds to be refused."""
    address = urllib.parse.urlsplit(url)
    try:
        socket.create_connection((address.hostname, address.port), timeout=0.5).close()
    except OSError:
        return False
    return True


def _open_once_answering(url: str) -> None:
    """Opens the default browser at url as soon as the web UI answers. It runs beside the server, so if the
    server fails to start, the process ends and this with it."""
    while not _answers(url):
        time.sleep(0.2)
    webbrowser.open(url)


def _create_user(cfg: Config, args: argparse.Namespace) -> int:
    from live_tracker.db import Database
    from live_tracker.users import AccountError, create_user

    password = args.password
    if password is None:
        password = getpass.getpass("password: ")
        if getpass.getpass("again: ") != password:
            print("The passwords don't match.")
            return 1
    db = Database(cfg.db_path)
    db.migrate()
    try:
        with db.session() as session:
            create_user(session, args.username, password, admin=args.admin)
    except AccountError as e:
        print(e)
        return 1
    print(f"{'administrator' if args.admin else 'user'} {args.username} created")
    return 0


def _import(cfg: Config, args: argparse.Namespace) -> int:
    from live_tracker.db import Database
    from live_tracker.importer import import_files
    from live_tracker.people import FaceTools, People

    db = Database(cfg.db_path)
    db.migrate()
    report = import_files(cfg, db, People(cfg, FaceTools(cfg)))
    print(f"people: {report.people}, photos: {report.photos}, events: {report.events}, camera: {report.camera or '-'}")
    for line in report.skipped:
        print(f"    skipped {line}")
    return 0


def _evaluate(cfg: Config, args: argparse.Namespace) -> int:
    from live_tracker.evaluate import evaluate

    evaluate(cfg, list(RECOGNITION_MODELS), args.outsiders)
    return 0


_COMMANDS = {
    "download-models": _download_models,
    "discover": _discover,
    "probe": _probe,
    "run": _run,
    "web": _web,
    "start": _start,
    "create-user": _create_user,
    "import": _import,
    "evaluate": _evaluate,
}
