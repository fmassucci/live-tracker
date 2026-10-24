"""The recognition service: every enabled camera from the database, with recognition on the door cameras.

Run it with `python -m portaria run`. It keeps working on its own: the web UI can be stopped or
restarted without affecting the door. They share the database, and the web UI reads frames and
status from this service's local port (preview.py).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select

from portaria.camera import Camera, Frame
from portaria.config import Config
from portaria.db import CAMERAS_VERSION, FACES_VERSION, Database, get_setting
from portaria.db import Camera as CameraRow
from portaria.door import make_door
from portaria.drawing import Overlay, render
from portaria.embedders import load_embedder
from portaria.events import EventLog, face_crop, purge_events
from portaria.faces import FaceDetector
from portaria.gallery import Gallery
from portaria.i18n import Translator
from portaria.liveness import NoLiveness
from portaria.models import model_path
from portaria.people import door_allowed, load_gallery
from portaria.pipeline import RateMeter, Recognizer, RepeatedFrames, Result
from portaria.preview import PreviewServer

log = logging.getLogger(__name__)

DECISION_HOLD_S = 3.0  # how long the door screen shows "welcome" or "not recognized"
SYNC_EVERY_S = 1.0
PURGE_EVERY_S = 3600.0


@dataclass(frozen=True)
class CameraSpec:
    id: int | None  # None for a camera given with --source, which isn't in the database
    name: str
    url: str
    role: str  # "door": recognition runs here; "view": image only

    @property
    def channel(self) -> str:
        return f"cam{self.id or 0}"


class CameraWorker:
    """One camera on its own thread: frames for the web UI always, recognition on door cameras."""

    def __init__(self, spec: CameraSpec, service: VisionService) -> None:
        self.spec = spec
        self._service = service
        self._camera = Camera(spec.url, service.cfg.camera.reconnect_delay_s, loop=service.source is None)
        self.recognizer = service.make_recognizer() if spec.role == "door" else None
        self.status: dict = {"name": spec.name, "role": spec.role, "online": False, "state": "offline"}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=spec.channel, daemon=True)

    @property
    def ended(self) -> bool:
        return self._camera.ended

    def start(self) -> None:
        self._camera.start()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self._camera.stop()

    def _run(self) -> None:
        cfg = self._service.cfg
        rate = RateMeter()
        repeats = RepeatedFrames()
        seq = 0
        held: tuple[str, str | None] | None = None  # (state, person) shown for a moment after a decision
        held_until = 0.0
        while not self._stop.is_set():
            frame = self._camera.next_frame(seq, timeout=cfg.camera.stale_after_s)
            if frame is None:
                if self._camera.ended:
                    break
                self.status = {**self.status, "online": False, "state": "offline", "fps": 0}
                continue
            seq = frame.seq
            if repeats.skip(frame):
                continue
            result = self.recognizer.process(frame, repeats.frozen(frame)) if self.recognizer else Result()
            if result.decision:
                self._service.on_decision(self.spec, frame, result)
                held, held_until = (result.decision.kind, result.decision.person), time.monotonic() + DECISION_HOLD_S
            state, person = held if held and time.monotonic() < held_until else (result.state, None)
            self._publish(frame, result)
            self.status = {
                "name": self.spec.name,
                "role": self.spec.role,
                "online": True,
                "fps": round(rate.tick(), 1),
                "size": f"{frame.image.shape[1]}x{frame.image.shape[0]}",
                "state": state,
                "problem": result.problem if state == "adjust" else "",
                "person": person,
                "person_name": self._service.gallery.name(person) if person else None,
            }

    def _publish(self, frame: Frame, result: Result) -> None:
        preview = self._service.preview
        channel = self.spec.channel
        preview.publish(f"{channel}-raw", frame.image)
        preview.publish(channel, render(frame.image, result.overlays, []))
        if self.recognizer:  # the door screen shows only the main face's box, never names or scores
            boxes = [Overlay(o.face, "", o.tone) for o in result.overlays if o.face is result.face]
            preview.publish(f"{channel}-kiosk", render(frame.image, boxes, []))


class VisionService:
    def __init__(self, cfg: Config, source: str | None = None) -> None:
        self.cfg = cfg
        self.source = source  # a single camera to use instead of the database's, for tests
        self.db = Database(cfg.db_path)
        self.db.migrate()
        self.preview = PreviewServer(cfg.preview.host, cfg.preview.port)
        self.gallery = Gallery(cfg.recognition.model, [], [])
        self._translate = Translator(cfg.web.language)
        self._liveness = NoLiveness()
        self._door = make_door(cfg.door.mode)
        self._events = EventLog(self.db, cfg.paths.data_dir)
        self._door_allowed: set[str] = set()
        self._workers: dict[int | None, CameraWorker] = {}
        self._versions: dict[str, str | None] = {}
        self._stop = threading.Event()

    def make_recognizer(self) -> Recognizer:
        detector = FaceDetector(
            model_path(self.cfg.paths.models_dir, "yunet"),
            self.cfg.detection.score_threshold,
            self.cfg.detection.detect_width,
        )
        embedder = load_embedder(self.cfg.recognition.model, self.cfg.paths.models_dir)
        return Recognizer(self.cfg, detector, embedder, self.gallery, self._liveness, self._translate)

    def run(self) -> None:
        log.info("recognition model %s, door mode %s", self.cfg.recognition.model, self.cfg.door.mode)
        if self._liveness.name == "none":
            log.warning("no liveness check yet: a photo of a person would be accepted. Keep the door in shadow mode.")
        self.preview.start()
        last_purge = float("-inf")
        try:
            while not self._stop.is_set():
                self._sync()
                if time.monotonic() - last_purge > PURGE_EVERY_S:
                    last_purge = time.monotonic()
                    purged = purge_events(self.db, self.cfg.paths.data_dir, self.cfg.storage.retention_days)
                    if purged:
                        log.info("deleted %d events older than %d days", purged, self.cfg.storage.retention_days)
                self.preview.set_status(self._status())
                if self.source and self._workers and all(w.ended for w in self._workers.values()):
                    break
                self._stop.wait(SYNC_EVERY_S)
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            for worker in self._workers.values():
                worker.stop()
            self.preview.stop()

    def stop(self) -> None:
        self._stop.set()

    def on_decision(self, spec: CameraSpec, frame: Frame, result: Result) -> None:
        """Called from a camera's thread: opens the door when it should and records the event."""
        decision = result.decision
        assert decision is not None
        name = self.gallery.name(decision.person) if decision.person else None
        door = "kept"
        if decision.kind == "recognized" and decision.person in self._door_allowed:
            self._door.open(name or "")
            door = "shadow" if self.cfg.door.mode == "shadow" else "opened"
        self._events.record(
            decision,
            render(frame.image, result.overlays, [], width=1280),
            face=face_crop(frame.image, result.face.box) if result.face else None,
            camera_id=spec.id,
            person_name=name,
            door=door,
            model=self.cfg.recognition.model,
            liveness=self._liveness.name,
        )
        who = f" {name}" if name else ""
        log.info("%s%s (score %.2f) at %s, door %s", decision.kind, who, decision.score, spec.name, door)

    def _sync(self) -> None:
        """Reloads faces and cameras when the web UI has changed them."""
        with self.db.session() as session:
            versions = {key: get_setting(session, key, "0") for key in (FACES_VERSION, CAMERAS_VERSION)}
            if versions[FACES_VERSION] != self._versions.get(FACES_VERSION):
                self.gallery = load_gallery(session, self.cfg.recognition.model)
                self._door_allowed = door_allowed(session)
                for worker in self._workers.values():
                    if worker.recognizer:
                        worker.recognizer.gallery = self.gallery
                log.info("faces loaded: %d people", len(self.gallery.people))
            if versions[CAMERAS_VERSION] != self._versions.get(CAMERAS_VERSION):
                if self.source:
                    specs = [CameraSpec(None, "source", self.source, "door")]
                else:
                    rows = session.scalars(select(CameraRow).where(CameraRow.enabled).order_by(CameraRow.id))
                    specs = [CameraSpec(c.id, c.name, c.url, c.role) for c in rows]
                self._apply(specs)
        self._versions = versions

    def _apply(self, specs: list[CameraSpec]) -> None:
        wanted = {spec.id: spec for spec in specs}
        for key, worker in list(self._workers.items()):
            if wanted.get(key) != worker.spec:
                worker.stop()
                del self._workers[key]
                log.info("camera %s stopped", worker.spec.name)
        for key, spec in wanted.items():
            if key not in self._workers:
                self._workers[key] = CameraWorker(spec, self)
                self._workers[key].start()
                log.info("camera %s started (%s)", spec.name, spec.role)
        if not self._workers:
            log.warning("no cameras: add one in the web UI (Cameras)")

    def _status(self) -> dict:
        return {
            "model": self.cfg.recognition.model,
            "liveness": self._liveness.name,
            "door": self.cfg.door.mode,
            "people": len(self.gallery.people),
            "cameras": {str(spec_id or 0): worker.status for spec_id, worker in self._workers.items()},
        }
