"""Command line: python -m portaria <command> --help"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from portaria.config import RECOGNITION_MODELS, Config, load_config


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
    parser = argparse.ArgumentParser(prog="portaria", description="Face recognition at the staff entrance.")
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

    commands.add_parser("web", help="start the web UI")

    p = commands.add_parser("create-user", help="create a web UI account")
    p.add_argument("username")
    p.add_argument("--password", help="asked for when omitted")

    commands.add_parser("import", help="import the first setup's photo folders, events and camera")

    p = commands.add_parser("evaluate", help="compare the models on photo folders in data/enroll/ and suggest a threshold")
    p.add_argument("--outsiders", type=Path, help="folder with photos of people who are NOT enrolled")
    return parser


def _download_models(cfg: Config, args: argparse.Namespace) -> int:
    from portaria.models import download_models

    download_models(cfg.paths.models_dir)
    return 0


def _discover(cfg: Config, args: argparse.Namespace) -> int:
    from portaria import onvif

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
    from portaria.camera import probe, redact
    from portaria.images import write_jpeg

    source = args.source or cfg.camera.source
    if not source:
        print("Give a --source, or set [camera] source in config.toml")
        return 1
    print(f"source: {redact(source)}")
    info, image = probe(source)
    if not info["opened"]:
        print("could not open it: wrong URL or password, or the camera has no RTSP")
        return 1
    for key in ("size", "codec", "reported_fps", "measured_fps"):
        print(f"{key}: {info[key]}")
    if image is not None:
        path = cfg.paths.data_dir / "probe.jpg"
        write_jpeg(path, image)
        print(f"frame saved to {path}")
    return 0


def _run(cfg: Config, args: argparse.Namespace) -> int:
    from portaria.service import VisionService

    VisionService(cfg, source=args.source).run()
    return 0


def _web(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from portaria.web.app import create_app

    print(f"web UI at http://{cfg.web.host}:{cfg.web.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(cfg), host=cfg.web.host, port=cfg.web.port, log_level="warning")
    return 0


def _create_user(cfg: Config, args: argparse.Namespace) -> int:
    from portaria.db import Database
    from portaria.users import AccountError, create_user

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
            create_user(session, args.username, password)
    except AccountError as e:
        print(e)
        return 1
    print(f"user {args.username} created")
    return 0


def _import(cfg: Config, args: argparse.Namespace) -> int:
    from portaria.db import Database
    from portaria.importer import import_files
    from portaria.people import FaceTools, People

    db = Database(cfg.db_path)
    db.migrate()
    report = import_files(cfg, db, People(cfg, FaceTools(cfg)))
    print(f"people: {report.people}, photos: {report.photos}, events: {report.events}, camera: {report.camera or '-'}")
    for line in report.skipped:
        print(f"    skipped {line}")
    return 0


def _evaluate(cfg: Config, args: argparse.Namespace) -> int:
    from portaria.evaluate import evaluate

    evaluate(cfg, list(RECOGNITION_MODELS), args.outsiders)
    return 0


_COMMANDS = {
    "download-models": _download_models,
    "discover": _discover,
    "probe": _probe,
    "run": _run,
    "web": _web,
    "create-user": _create_user,
    "import": _import,
    "evaluate": _evaluate,
}
