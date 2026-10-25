"""The recognition service: every enabled camera from the database, each running its AI model (face
recognition with its liveness check, people and objects, fire and smoke, or falls), and the doors attached to them.

Run it with `python -m portaria run`. It keeps working on its own: the web UI can be stopped or
restarted without affecting the door. They share the database, and the web UI reads frames and
status from this service's local port (preview.py). Stop the system, in Settings, stops both.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from portaria.camera import Camera, Frame, part_of
from portaria.config import Config
from portaria.db import CAMERAS_VERSION, FACES_VERSION, MODELS_VERSION, STOP_REQUESTS, Database, get_setting
from portaria.db import Camera as CameraRow
from portaria.door import make_door
from portaria.drawing import Overlay, render
from portaria.embedders import load_embedder
from portaria.events import EventLog, face_crop, purge_events
from portaria.faces import FaceDetector
from portaria.falls import PERSON, FallDetector, FallSettings
from portaria.fire import FireDetector, FireSettings
from portaria.gallery import Gallery
from portaria.i18n import Translator
from portaria.liveness import load_liveness
from portaria.models import model_path
from portaria.dfine import DFineDetector
from portaria.objects import ObjectAnalyzer, ObjectDetector, ObjectSettings, Sighting
from portaria.people import door_allowed, load_gallery
from portaria.pipeline import RateMeter, Recognizer, RepeatedFrames, Result
from portaria.preview import PreviewServer
from portaria.recording import APPEAR, DEFAULT_EVERY_S, RecordGate, RecordRule, decision_subject, last_recorded, thing

log = logging.getLogger(__name__)

DECISION_HOLD_S = 3.0  # how long the door screen shows "welcome" or "not recognized"
SYNC_EVERY_S = 1.0
PURGE_EVERY_S = 3600.0
# The model file behind each kind of record a camera makes of things, as Event.model.
THING_MODELS = {"objects": "yolox", "fire": "yolo26n-fire", "falls": "yolov8s-pose"}


def thing_model(spec: CameraSpec) -> str:
    """The model file behind a camera's record of a thing: for people and objects, the one its settings chose."""
    if spec.analysis == "objects" and ObjectSettings.parse(spec.settings).model == "dfine_m":
        return "dfine-m"
    return THING_MODELS.get(spec.analysis, spec.analysis)


@dataclass(frozen=True)
class CameraSpec:
    id: int | None  # None for a camera given with --source, which isn't in the database
    name: str
    url: str
    device: str = ""  # "door": recognizing someone with access opens it; "": nothing attached
    part: str = ""  # "": the whole picture; "top" or "bottom": one lens of a two-lens camera
    analysis: str = "faces"  # what runs on the picture: "faces", "objects", "fire", "falls", or "" for nothing
    settings: str = "{}"  # the analysis's own settings, as JSON (Camera.model_settings)
    record_mode: str = APPEAR  # when a photo of what the model finds is kept (recording.RecordRule)
    record_every_s: int = DEFAULT_EVERY_S

    @property
    def channel(self) -> str:
        return f"cam{self.id or 0}"


class CameraWorker:
    """One camera on its own thread: frames for the web UI, and its AI model's analysis.

    `source` may be shared: the two lenses of a two-lens camera arrive in one picture, read once.
    A camera whose model isn't downloaded keeps sending its picture, and says so in its status.
    """

    def __init__(self, spec: CameraSpec, service: VisionService, source: Camera) -> None:
        self.spec = spec
        self._service = service
        self._camera = source
        self.analyzer: Recognizer | ObjectAnalyzer | None = None
        self.model_missing = False
        rule = RecordRule(spec.record_mode, spec.record_every_s)
        self.gate = RecordGate(rule, service.last_recorded(spec, rule))
        try:
            if spec.analysis == "faces":
                self.analyzer = service.make_recognizer()
            elif spec.analysis == "objects":
                settings = ObjectSettings.parse(spec.settings)
                detector = service.make_dfine_detector() if settings.model == "dfine_m" else service.make_detector()
                self.analyzer = ObjectAnalyzer(detector, settings, service.translate, repeat_s=rule.repeat_s)
            elif spec.analysis == "fire":
                self.analyzer = ObjectAnalyzer(service.make_fire_detector(), FireSettings.parse(spec.settings),
                                               service.translate, repeat_s=rule.repeat_s)
            elif spec.analysis == "falls":
                self.analyzer = ObjectAnalyzer(service.make_fall_detector(), FallSettings.parse(spec.settings),
                                               service.translate, repeat_s=rule.repeat_s, context=(PERSON,))
        except Exception:  # a missing or damaged model file must not stop the other cameras
            log.exception("camera %s: its AI model can't be loaded", spec.name)
            self.model_missing = True
        self.status: dict = {"name": spec.name, "device": spec.device, "online": False, "state": "offline",
                             "model_missing": self.model_missing}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=spec.channel, daemon=True)

    @property
    def ended(self) -> bool:
        return self._camera.ended

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

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
            frame = part_of(frame, self.spec.part)
            if repeats.skip(frame):
                continue
            result = self.analyzer.process(frame, repeats.frozen(frame)) if self.analyzer else Result()
            if result.decision:
                keep = self.gate.allow(decision_subject(result.decision.kind, result.decision.person))
                self._service.on_decision(self.spec, frame, result, keep=keep)
                held, held_until = (result.decision.kind, result.decision.person), time.monotonic() + DECISION_HOLD_S
            for sighting in result.sightings:
                if self.gate.allow(thing(sighting.kind)):
                    self._service.on_sighting(self.spec, frame, result, sighting)
            state, person = held if held and time.monotonic() < held_until else (result.state, None)
            self._publish(frame, result)
            self.status = {
                "name": self.spec.name,
                "device": self.spec.device,
                "online": True,
                "fps": round(rate.tick(), 1),
                "size": f"{frame.image.shape[1]}x{frame.image.shape[0]}",
                "state": state,
                "problem": result.problem if state == "adjust" else "",
                "person": person,
                "person_name": self._service.gallery.name(person) if person else None,
                "model_missing": self.model_missing,
            }

    def _publish(self, frame: Frame, result: Result) -> None:
        preview = self._service.preview
        channel = self.spec.channel
        preview.publish(f"{channel}-raw", frame.image)
        preview.publish(channel, render(frame.image, result.overlays, []))
        if self.spec.device == "door":  # the door screen shows only the main face's box, never names or scores
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
        self._liveness = load_liveness(cfg.paths.models_dir, cfg.liveness.threshold)
        self._door = make_door(cfg.door.mode)
        self._events = EventLog(self.db, cfg.paths.data_dir)
        self._door_allowed: set[str] = set()
        self._workers: dict[int | None, CameraWorker] = {}
        self._sources: dict[str, Camera] = {}  # by address: the cameras cut from one picture share it
        self._versions: dict[str, str | None] = {}
        self._stop = threading.Event()

    def translate(self, text: str) -> str:
        """UI text in the service's language, for what is drawn on the pictures."""
        return self._translate(text)

    def make_detector(self) -> ObjectDetector:
        return ObjectDetector(model_path(self.cfg.paths.models_dir, "yolox"))

    def make_dfine_detector(self) -> DFineDetector:
        return DFineDetector(model_path(self.cfg.paths.models_dir, "dfine_m"))

    def make_fire_detector(self) -> FireDetector:
        return FireDetector(model_path(self.cfg.paths.models_dir, "fire_yolo26n"))

    def make_fall_detector(self) -> FallDetector:
        return FallDetector(model_path(self.cfg.paths.models_dir, "pose_yolov8s"))

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
            log.warning("no liveness check: a photo of a person would be accepted. Download it in Settings, AI models.")
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
            for source in self._sources.values():
                source.stop()
            self.preview.stop()

    def stop(self) -> None:
        self._stop.set()

    def last_recorded(self, spec: CameraSpec, rule: RecordRule) -> dict[str, datetime]:
        """The camera's last photo of each person and kind of thing that its rule still counts."""
        since = rule.remembered_since(datetime.now())
        return last_recorded(self.db, spec.id, since) if spec.id is not None and since else {}

    def on_decision(self, spec: CameraSpec, frame: Frame, result: Result, keep: bool = True) -> None:
        """Called from a camera's thread: opens an attached door when it should, and records the event unless
        the camera's rule leaves this one out."""
        decision = result.decision
        assert decision is not None
        name = self.gallery.name(decision.person) if decision.person else None
        door = ""  # no door attached to this camera
        if spec.device == "door":
            door = "kept"
            if decision.kind == "recognized" and decision.person in self._door_allowed:
                self._door.open(name or "")
                door = "shadow" if self.cfg.door.mode == "shadow" else "opened"
        who = f" {name}" if name else ""
        at_door = f", door {door}" if door else ""
        if not keep:
            log.info("%s%s (score %.2f) at %s%s, no photo: the camera's rule", decision.kind, who, decision.score,
                     spec.name, at_door)
            return
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
        log.info("%s%s (score %.2f) at %s%s", decision.kind, who, decision.score, spec.name, at_door)

    def on_sighting(self, spec: CameraSpec, frame: Frame, result: Result, sighting: Sighting) -> None:
        """Called from a camera's thread: records a kind of thing that stayed in the picture."""
        self._events.record_sighting(
            sighting,
            render(frame.image, result.overlays, [], width=1280),
            thumbnail=face_crop(frame.image, sighting.box, margin=0.25, size=360),
            camera_id=spec.id,
            model=thing_model(spec),
        )
        log.info("detected %s (%d, score %.2f) at %s", sighting.kind, sighting.count, sighting.score, spec.name)

    def _sync(self) -> None:
        """Reloads faces and cameras when the web UI has changed them, and a camera's model once it's downloaded.
        Stops when Settings stops the system."""
        with self.db.session() as session:
            keys = (FACES_VERSION, CAMERAS_VERSION, MODELS_VERSION, STOP_REQUESTS)
            versions = {key: get_setting(session, key, "0") for key in keys}
            # Only a stop asked after this service started: an earlier one was for the service that ran then.
            if self._versions and versions[STOP_REQUESTS] != self._versions[STOP_REQUESTS]:
                log.info("stopping: Settings stopped the system")
                self.stop()
                return
            if versions[FACES_VERSION] != self._versions.get(FACES_VERSION):
                self.gallery = load_gallery(session, self.cfg.recognition.model)
                self._door_allowed = door_allowed(session)
                for worker in self._workers.values():
                    if isinstance(worker.analyzer, Recognizer):
                        worker.analyzer.gallery = self.gallery
                log.info("faces loaded: %d people", len(self.gallery.people))
            models_changed = versions[MODELS_VERSION] != self._versions.get(MODELS_VERSION)
            if models_changed:
                self._reload_liveness()
                for key, worker in list(self._workers.items()):  # started again just below, with their model
                    if worker.model_missing:
                        worker.stop()
                        del self._workers[key]
            if models_changed or versions[CAMERAS_VERSION] != self._versions.get(CAMERAS_VERSION):
                if self.source:
                    specs = [CameraSpec(None, "source", self.source, device="door")]
                else:
                    rows = session.scalars(select(CameraRow).where(CameraRow.enabled).order_by(CameraRow.id))
                    specs = [CameraSpec(c.id, c.name, c.url, device=c.device, part=c.part, analysis=c.analysis,
                                        settings=c.model_settings, record_mode=c.record_mode,
                                        record_every_s=c.record_every_s) for c in rows]
                self._apply(specs)
        self._versions = versions

    def _reload_liveness(self) -> None:
        """The liveness check, once its model is downloaded or after it's removed, for every face camera."""
        liveness = load_liveness(self.cfg.paths.models_dir, self.cfg.liveness.threshold)
        if liveness.name == self._liveness.name:
            return
        self._liveness = liveness
        for worker in self._workers.values():
            if isinstance(worker.analyzer, Recognizer):
                worker.analyzer.liveness = liveness
        log.info("liveness check: %s", liveness.name)

    def _apply(self, specs: list[CameraSpec]) -> None:
        wanted = {spec.id: spec for spec in specs}
        for key, worker in list(self._workers.items()):
            if wanted.get(key) != worker.spec:
                worker.stop()
                del self._workers[key]
                log.info("camera %s stopped", worker.spec.name)
        for url in self._sources.keys() - {spec.url for spec in specs}:  # no camera reads it any more
            self._sources.pop(url).stop()
        for key, spec in wanted.items():
            if key not in self._workers:
                if spec.url not in self._sources:
                    reconnect_s = self.cfg.camera.reconnect_delay_s
                    self._sources[spec.url] = Camera(spec.url, reconnect_s, loop=self.source is None).start()
                self._workers[key] = CameraWorker(spec, self, self._sources[spec.url])
                self._workers[key].start()
                log.info("camera %s started%s", spec.name, " (door)" if spec.device == "door" else "")
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
