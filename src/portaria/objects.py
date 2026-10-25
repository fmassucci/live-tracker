"""The people-and-objects model: YOLOX-S, trained on COCO's 80 kinds of things.

Each camera chooses the kinds it records. The model looks a few times per second; a kind is reported once it
has stayed in the picture for the camera's minimum time, then again only after it has been gone for a moment,
or, with the camera's interval, every interval while it stays: a person standing at the counter for an hour is
one record, or one every 15 minutes, not thousands. The camera's rule (recording.py) decides what is kept.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import cv2
import numpy as np

from portaria.camera import Frame
from portaria.drawing import Overlay
from portaria.pipeline import Result

# COCO's 80 kinds in the model's output order: the key stored in settings and records, and the English name,
# which is also the UI text, translated as usual.
THINGS = (
    ("person", "Person"), ("bicycle", "Bicycle"), ("car", "Car"), ("motorcycle", "Motorcycle"),
    ("airplane", "Airplane"), ("bus", "Bus"), ("train", "Train"), ("truck", "Truck"), ("boat", "Boat"),
    ("traffic_light", "Traffic light"), ("fire_hydrant", "Fire hydrant"), ("stop_sign", "Stop sign"),
    ("parking_meter", "Parking meter"), ("bench", "Bench"), ("bird", "Bird"), ("cat", "Cat"), ("dog", "Dog"),
    ("horse", "Horse"), ("sheep", "Sheep"), ("cow", "Cow"), ("elephant", "Elephant"), ("bear", "Bear"),
    ("zebra", "Zebra"), ("giraffe", "Giraffe"), ("backpack", "Backpack"), ("umbrella", "Umbrella"),
    ("handbag", "Handbag"), ("tie", "Tie"), ("suitcase", "Suitcase"), ("frisbee", "Frisbee"), ("skis", "Skis"),
    ("snowboard", "Snowboard"), ("sports_ball", "Ball"), ("kite", "Kite"), ("baseball_bat", "Baseball bat"),
    ("baseball_glove", "Baseball glove"), ("skateboard", "Skateboard"), ("surfboard", "Surfboard"),
    ("tennis_racket", "Tennis racket"), ("bottle", "Bottle"), ("wine_glass", "Wine glass"), ("cup", "Cup"),
    ("fork", "Fork"), ("knife", "Knife"), ("spoon", "Spoon"), ("bowl", "Bowl"), ("banana", "Banana"),
    ("apple", "Apple"), ("sandwich", "Sandwich"), ("orange", "Orange"), ("broccoli", "Broccoli"),
    ("carrot", "Carrot"), ("hot_dog", "Hot dog"), ("pizza", "Pizza"), ("donut", "Donut"), ("cake", "Cake"),
    ("chair", "Chair"), ("couch", "Couch"), ("potted_plant", "Potted plant"), ("bed", "Bed"),
    ("dining_table", "Table"), ("toilet", "Toilet"), ("tv", "TV"), ("laptop", "Laptop"), ("mouse", "Mouse"),
    ("remote", "Remote control"), ("keyboard", "Keyboard"), ("cell_phone", "Cell phone"),
    ("microwave", "Microwave"), ("oven", "Oven"), ("toaster", "Toaster"), ("sink", "Sink"),
    ("refrigerator", "Refrigerator"), ("book", "Book"), ("clock", "Clock"), ("vase", "Vase"),
    ("scissors", "Scissors"), ("teddy_bear", "Teddy bear"), ("hair_drier", "Hair dryer"),
    ("toothbrush", "Toothbrush"),
)
KEYS = tuple(key for key, _ in THINGS)
NAMES = dict(THINGS)

# How camera settings offer them: what a bakery asks for most on its own row, the rest in groups.
COMMON = ("person", "cell_phone", "dog", "cat", "bird", "car", "motorcycle", "truck")
GROUPS = (
    ("Animals", ("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe")),
    ("Vehicles", ("bicycle", "car", "motorcycle", "bus", "truck", "train", "airplane", "boat")),
    ("Electronics", ("laptop", "tv", "keyboard", "mouse", "remote")),
    ("Kitchen", ("knife", "fork", "spoon", "cup", "bottle", "wine_glass", "bowl")),
    ("Food", ("cake", "donut", "sandwich", "pizza", "hot_dog", "banana", "apple", "orange", "broccoli", "carrot")),
    ("Appliances", ("oven", "microwave", "refrigerator", "toaster", "sink")),
    ("Personal items", ("backpack", "handbag", "suitcase", "umbrella", "tie")),
    ("Furniture", ("chair", "dining_table", "couch", "bed", "potted_plant", "toilet")),
    ("Street", ("traffic_light", "fire_hydrant", "stop_sign", "parking_meter", "bench")),
    ("Other things", ("book", "clock", "vase", "scissors", "teddy_bear", "hair_drier", "toothbrush")),
    ("Sports", ("sports_ball", "skateboard", "kite", "tennis_racket", "surfboard", "skis", "snowboard", "frisbee",
                "baseball_bat", "baseball_glove")),
)


# Sensitivity as three named levels: how sure the model must be before something counts.
SENSITIVITY = ((0.35, "Finds more"), (0.5, "Balanced"), (0.65, "Fewer false alarms"))

# The models that can find them: the key a camera's settings keep ("" for YOLOX-S, the first one), and its name and
# file (models.FILES). D-FINE-M (dfine.py) is more accurate and slower.
DETECTORS = {"": ("YOLOX-S", "yolox"), "dfine_m": ("D-FINE-M", "dfine_m")}


@dataclass(frozen=True)
class ObjectSettings:
    """One camera's choices, stored as JSON in Camera.model_settings. Fire and smoke, and falls, have their own
    kinds and defaults (fire.FireSettings, falls.FallSettings)."""

    KNOWN: ClassVar[dict[str, str]] = NAMES  # the kinds this model finds

    kinds: tuple[str, ...] = ("person",)
    confidence: float = 0.5  # how sure the model must be: lower finds more, and makes more mistakes
    min_seconds: float = 2.0  # how long a thing stays in the picture before it is recorded
    fps: float = 4.0  # analyses per second
    model: str = ""  # which model finds them: a key of DETECTORS

    @classmethod
    def parse(cls, text: str) -> ObjectSettings:
        try:
            data = json.loads(text or "{}")
        except ValueError:
            data = {}
        return cls.of(data if isinstance(data, dict) else {})

    @classmethod
    def of(cls, data: dict) -> ObjectSettings:
        """Whatever was stored or sent, as valid settings: unknown kinds are dropped and numbers kept in range."""
        default = cls()
        kinds = tuple(dict.fromkeys(k for k in data.get("kinds", default.kinds) if k in cls.KNOWN)) or default.kinds
        return cls(
            kinds=kinds,
            confidence=_within(data.get("confidence"), 0.2, 0.8, default.confidence),
            min_seconds=_within(data.get("min_seconds"), 0, 600, default.min_seconds),
            fps=_within(data.get("fps"), 1, 10, default.fps),
            model=data["model"] if str(data.get("model")) in DETECTORS else default.model,
        )

    @property
    def level(self) -> float:
        """The named sensitivity level closest to the stored confidence."""
        return min((value for value, _ in SENSITIVITY), key=lambda value: abs(value - self.confidence))

    def dumps(self) -> str:
        data = {"kinds": list(self.kinds), "confidence": self.confidence, "min_seconds": self.min_seconds,
                "fps": self.fps}
        if self.model:  # the first model, YOLOX-S, isn't written: settings from before the choice existed mean it
            data["model"] = self.model
        return json.dumps(data)


def _within(value: object, low: float, high: float, default: float) -> float:
    try:
        return min(high, max(low, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Detection:
    kind: str  # a key of NAMES
    score: float
    box: np.ndarray  # x, y, width, height in the frame's pixels


class ObjectDetector:
    """YOLOX-S on ONNX Runtime: a picture in, every thing found with its kind and score out."""

    SIZE = 640  # the model's input: a 640x640 picture
    THREADS = 4  # with ONNX Runtime's default, 8 threads spinning between looks, 4 looks/s took 4 cores, not 1.4

    def __init__(self, path: Path, overlap: float = 0.45) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = self.THREADS
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name
        self._overlap = overlap  # boxes of one kind overlapping more than this are the same thing
        self._grids, self._strides = anchors(self.SIZE)

    def detect(self, image: np.ndarray, min_score: float) -> list[Detection]:
        blob, ratio = letterbox(image, self.SIZE)
        raw = self._session.run(None, {self._input: blob})[0][0]
        return decode(raw, self._grids, self._strides, ratio, min_score, self._overlap)


def letterbox(image: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    """What the model was trained on: RGB, scaled to fit from the top-left corner, the rest grey (114)."""
    ratio = min(size / image.shape[0], size / image.shape[1])
    resized = cv2.resize(image, (int(image.shape[1] * ratio), int(image.shape[0] * ratio)),
                         interpolation=cv2.INTER_LINEAR)
    padded = np.full((size, size, 3), 114, np.float32)
    padded[: resized.shape[0], : resized.shape[1]] = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    return padded.transpose(2, 0, 1)[np.newaxis], ratio


def anchors(size: int) -> tuple[np.ndarray, np.ndarray]:
    """The grid cell and stride of each of the model's 8400 predictions, at strides 8, 16 and 32."""
    grids, strides = [], []
    for stride in (8, 16, 32):
        cells = size // stride
        xs, ys = np.meshgrid(np.arange(cells), np.arange(cells))
        grids.append(np.stack((xs, ys), 2).reshape(-1, 2))
        strides.append(np.full((cells * cells, 1), stride))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)


def decode(raw: np.ndarray, grids: np.ndarray, strides: np.ndarray, ratio: float, min_score: float,
           overlap: float) -> list[Detection]:
    """The model's 8400 raw predictions as things in the original picture, duplicates removed."""
    scores = raw[:, 4:5] * raw[:, 5:]  # sure there is something, times sure of its kind
    kinds = scores.argmax(axis=1)
    best = scores.max(axis=1)
    keep = best >= min_score
    if not keep.any():
        return []
    raw, kinds, best = raw[keep], kinds[keep], best[keep]
    centers = (raw[:, :2] + grids[keep]) * strides[keep]
    sizes = np.exp(raw[:, 2:4]) * strides[keep]
    boxes = np.concatenate([centers - sizes / 2, sizes], axis=1) / ratio
    chosen = cv2.dnn.NMSBoxesBatched(boxes.tolist(), best.tolist(), kinds.tolist(), min_score, overlap)
    return [Detection(KEYS[kinds[i]], float(best[i]), boxes[i]) for i in np.array(chosen, dtype=int).reshape(-1)]


@dataclass(frozen=True)
class Sighting:
    """A kind that has stayed in the picture for the camera's minimum time: what gets recorded."""

    kind: str
    count: int  # how many at that moment
    score: float  # the best one's
    box: np.ndarray  # the best one's, for the record's thumbnail


@dataclass
class _Stay:
    since: float
    last_seen: float
    reported_at: float | None = None


class Presence:
    """When each kind arrives and leaves. A kind is reported once per stay, after min_seconds in the picture,
    and with repeat_s, again every repeat_s while it stays. A stay ends after GONE_AFTER_S without it, so a few
    missed looks don't start a new one."""

    GONE_AFTER_S = 3.0

    def __init__(self, min_seconds: float, repeat_s: float = 0.0) -> None:
        self._min_seconds = min_seconds
        self._repeat_s = repeat_s
        self._stays: dict[str, _Stay] = {}

    def update(self, found: list[Detection], now: float) -> list[Sighting]:
        by_kind: dict[str, list[Detection]] = {}
        for detection in found:
            by_kind.setdefault(detection.kind, []).append(detection)
        sightings = []
        for kind, detections in by_kind.items():
            stay = self._stays.setdefault(kind, _Stay(since=now, last_seen=now))
            stay.last_seen = now
            if self._due(stay, now):
                stay.reported_at = now
                best = max(detections, key=lambda d: d.score)
                sightings.append(Sighting(kind, len(detections), best.score, best.box))
        for kind, stay in list(self._stays.items()):
            if kind not in by_kind and now - stay.last_seen > self.GONE_AFTER_S:
                del self._stays[kind]
        return sightings

    def _due(self, stay: _Stay, now: float) -> bool:
        if stay.reported_at is None:
            return now - stay.since >= self._min_seconds
        return self._repeat_s > 0 and now - stay.reported_at >= self._repeat_s


class ObjectAnalyzer:
    """One camera's people-and-objects analysis: looks a few times per second, marks what it found on the picture,
    and reports the kinds that stayed. Fire and smoke, and falls, run the same way with their own detector."""

    def __init__(self, detector: ObjectDetector, settings: ObjectSettings, name: Callable[[str], str],
                 clock: Callable[[], float] = time.monotonic, repeat_s: float = 0.0,
                 context: Iterable[str] = ()) -> None:
        self._detector = detector
        self._settings = settings
        self._kinds = set(settings.kinds)
        self._context = set(context)  # kinds only marked on the picture, never recorded: the people a fall needs
        self._presence = Presence(settings.min_seconds, repeat_s)
        self._name = name  # a kind's name in the service's language, for the marks on the picture
        self._clock = clock
        self._last_look = float("-inf")
        self._found: list[Detection] = []  # from the last look, marked on every frame until the next one
        self._seen: list[Detection] = []  # the context kinds from the last look

    def process(self, frame: Frame, frozen: bool) -> Result:
        now = self._clock()
        sightings: list[Sighting] = []
        if not frozen and now - self._last_look >= 1.0 / self._settings.fps:
            self._last_look = now
            detections = self._detector.detect(frame.image, self._settings.confidence)
            self._found = [d for d in detections if d.kind in self._kinds]
            self._seen = [d for d in detections if d.kind in self._context]
            sightings = self._presence.update(self._found, now)
        known = self._settings.KNOWN
        overlays = [Overlay(d, f"{self._name(known[d.kind])} {d.score:.0%}", "object") for d in self._found]
        overlays += [Overlay(d, "", "dim") for d in self._seen]
        return Result(state="checking" if self._found else "idle", overlays=overlays, sightings=sightings)
