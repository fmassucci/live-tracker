"""AI models: what each one does, the files it needs and where they come from, whether they are ready on this
computer, and downloading them with checksum verification."""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CHUNK = 1 << 20


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str
    sha256: str
    license: str
    size: int  # bytes


FILES = {
    "yunet": ModelFile(
        "face_detection_yunet_2023mar.onnx",
        "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "MIT",
        232_589,
    ),
    "sface": ModelFile(
        "face_recognition_sface_2021dec.onnx",
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "Apache-2.0",
        38_696_353,
    ),
    # Only the recognition model from the AuraFace repository: its detector and landmark files are
    # byte-for-byte InsightFace's models, which are licensed for non-commercial research only.
    "auraface": ModelFile(
        "auraface_v1_glintr100.onnx",
        "https://huggingface.co/fal/AuraFace-v1/resolve/main/glintr100.onnx",
        "a7933ea5330113b01c9b60351d8f4c33003f145d8470ac5f0e52ee2effe25c60",
        "Apache-2.0",
        260_694_151,
    ),
    # YOLOX-S trained on COCO's 80 kinds of things, as published in OpenCV's model zoo.
    "yolox": ModelFile(
        "object_detection_yolox_2022nov.onnx",
        "https://huggingface.co/opencv/object_detection_yolox/resolve/main/object_detection_yolox_2022nov.onnx",
        "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        "Apache-2.0",
        35_858_002,
    ),
    # D-FINE-M (Apache-2.0), pretrained on Objects365 and trained on COCO, as exported to ONNX by onnx-community: the
    # more accurate choice for people and objects (dfine.py).
    "dfine_m": ModelFile(
        "object_detection_dfine_m.onnx",
        "https://huggingface.co/onnx-community/dfine_m_obj2coco-ONNX/resolve/a2e1133c83887da7d5a000a025594b9d69fef5c2/onnx/model.onnx",
        "347f2faba93248c2e7500c9e604317fb391706c58a04802dd908573376dc1323",
        "Apache-2.0",
        78_624_257,
    ),
    # Liveness: Minivision's MiniFASNet pair (Silent-Face-Anti-Spoofing), as exported to ONNX by yakhyo.
    "minifasnet_v2": ModelFile(
        "liveness_minifasnet_v2.onnx",
        "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx",
        "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907",
        "Apache-2.0",
        1_743_581,
    ),
    "minifasnet_v1se": ModelFile(
        "liveness_minifasnet_v1se.onnx",
        "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV1SE.onnx",
        "ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676",
        "Apache-2.0",
        1_742_335,
    ),
    # Fire and smoke: a YOLO26n trained on fire and smoke photos (DarshanM0di/fireandsmoke). Of the three models
    # published as ONNX, it found the most fire and smoke in 606 of D-Fire's test photos (at Balanced, 79% of the
    # fires and 76% of the smoke), with the fewest false alarms (2% of the photos without either). Ultralytics
    # models are AGPL-3.0, fine for this in-house system.
    "fire_yolo26n": ModelFile(
        "fire_smoke_yolo26n.onnx",
        "https://huggingface.co/DarshanM0di/fireandsmoke/resolve/03e53bbf94ce21a2836900fac0cc4c372576f6ee/best%20(4).onnx",
        "5043e0efd1d474bcc901716e97946b8f8a787e4bd29451891f8dccfbe93322b6",
        "AGPL-3.0",
        9_805_935,
    ),
    # Falls: Ultralytics' YOLOv8s-pose, the 17 points of each person's body, as exported to ONNX by Xenova. It keeps
    # how sure it is of each point, which the falls check needs (the YOLO26 exports drop it).
    "pose_yolov8s": ModelFile(
        "pose_yolov8s.onnx",
        "https://huggingface.co/Xenova/yolov8s-pose/resolve/9dd2818a340ad6682eb38c9d99b6c67dc054da4b/onnx/model.onnx",
        "5c92815cc509a9ccb6a817f4c2816de800092964319ad179a2751eda6df35d83",
        "AGPL-3.0",
        46_787_284,
    ),
}
FACE_FILES = ("yunet", "sface", "auraface")  # what `download-models` fetches


@dataclass(frozen=True)
class Model:
    """Something a camera can run, downloaded once in Settings, AI models. A planned model has no files yet."""

    key: str  # Camera.analysis
    name: str
    description: str
    files: tuple[str, ...] = ()
    source: str = ""
    on_cameras: bool = False  # the recognition service can run it on a camera's picture
    part_of: str = ""  # another model that uses it by itself once it's downloaded; cameras don't choose it


CATALOG = (
    Model("faces", "Face recognition", "Recognizes the people registered in People.", ("yunet", "auraface"),
          "OpenCV, fal.ai", on_cameras=True),
    Model("liveness", "Liveness check",
          "Tells a real face from a photo or a screen. Face recognition uses it by itself once it's downloaded.",
          ("minifasnet_v2", "minifasnet_v1se"), "Minivision (MiniFASNet)", part_of="faces"),
    Model("objects", "People and objects",
          "80 kinds of things, among them people, cell phones, animals and vehicles.", ("yolox",), "OpenCV (YOLOX-S)",
          on_cameras=True),
    Model("dfine_m", "People and objects, more accurate",
          "D-FINE-M finds more than YOLOX-S, and takes about 0.3 s per picture instead of 0.12. Choose it in the "
          "People and objects settings of each camera.", ("dfine_m",), "USTC (D-FINE-M)", part_of="objects"),
    Model("fire", "Fire and smoke", "Warns of fire and smoke, in addition to the smoke detectors.",
          ("fire_yolo26n",), "Ultralytics (YOLO26n)", on_cameras=True),
    Model("falls", "Falls", "Notices someone lying on the floor.", ("pose_yolov8s",), "Ultralytics (YOLOv8s-pose)",
          on_cameras=True),
    Model("plates", "Number plates", "Reads the plates of cars and motorbikes."),
)
MODELS = {model.key: model for model in CATALOG}


def files_of(model: Model, recognizer: str) -> tuple[str, ...]:
    """The files a model needs. Face recognition uses the recognizer set in config.toml ([recognition] model)."""
    return ("yunet", recognizer) if model.key == "faces" else model.files


def model_path(models_dir: Path, name: str) -> Path:
    path = models_dir / FILES[name].filename
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing: download it in Settings, AI models")
    return path


_checked: dict[tuple[str, int, int], bool] = {}  # (path, size, mtime) -> checksum matched; hashing 260 MB takes a while


def file_state(models_dir: Path, key: str) -> str:
    """"ready", "missing", or "damaged": another size or checksum, like an interrupted copy or a changed file."""
    model = FILES[key]
    path = models_dir / model.filename
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "missing"
    if stat.st_size != model.size:
        return "damaged"
    seen = (str(path), stat.st_size, stat.st_mtime_ns)
    if seen not in _checked:
        _checked[seen] = _sha256(path) == model.sha256
    return "ready" if _checked[seen] else "damaged"


def model_state(models_dir: Path, files: tuple[str, ...]) -> str:
    """"planned" (no files yet), "ready", "damaged" or "missing"."""
    if not files:
        return "planned"
    states = {file_state(models_dir, key) for key in files}
    if states == {"ready"}:
        return "ready"
    return "damaged" if "damaged" in states else "missing"


def download_file(models_dir: Path, key: str, progress: Callable[[int], None] | None = None) -> None:
    """Downloads one file next to the others. It replaces nothing until its checksum matches."""
    model = FILES[key]
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / model.filename
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(model.url, timeout=30) as response, partial.open("wb") as out:
            while chunk := response.read(CHUNK):
                out.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(len(chunk))
        if digest.hexdigest() != model.sha256:
            raise RuntimeError(f"{key}: the downloaded file doesn't match its checksum")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def remove_files(models_dir: Path, files: tuple[str, ...]) -> None:
    for key in files:
        (models_dir / FILES[key].filename).unlink(missing_ok=True)


def download_models(models_dir: Path, keys: tuple[str, ...] = FACE_FILES) -> None:
    """The command line's download, with its progress on the console."""
    for key in keys:
        model = FILES[key]
        if file_state(models_dir, key) == "ready":
            log.info("%s: already downloaded", key)
            continue
        log.info("%s (%s): downloading %s", key, model.license, model.url)
        download_file(models_dir, key, _ConsoleProgress(model.size))
        sys.stderr.write("\n")


@dataclass
class Download:
    total: int  # bytes
    done: int = 0
    error: str = ""
    finished: bool = False
    announced: bool = False  # the page has shown how it ended


class Downloads:
    """Model downloads running in the background, at most one per model, with their progress."""

    def __init__(self, models_dir: Path, on_finished: Callable[[], None] = lambda: None) -> None:
        self._dir = models_dir
        self._on_finished = on_finished  # called after every download that ends, well or not
        self._lock = threading.Lock()
        self._jobs: dict[str, Download] = {}

    def get(self, key: str) -> Download | None:
        return self._jobs.get(key)

    def running(self, key: str) -> bool:
        job = self._jobs.get(key)
        return job is not None and not job.finished

    def start(self, key: str, files: tuple[str, ...]) -> None:
        with self._lock:
            if self.running(key):
                return
            needed = [name for name in files if file_state(self._dir, name) != "ready"]
            job = self._jobs[key] = Download(total=sum(FILES[name].size for name in needed) or 1)
        threading.Thread(target=self._run, args=(job, needed), name=f"download-{key}", daemon=True).start()

    def _run(self, job: Download, files: list[str]) -> None:
        def advance(size: int) -> None:
            job.done += size

        try:
            for name in files:
                download_file(self._dir, name, advance)
        except Exception as e:  # no internet, a full disk, a file changed upstream: the page offers to try again
            log.warning("model download failed: %s", e)
            job.error = str(e)
        finally:
            job.finished = True
            self._on_finished()


class _ConsoleProgress:
    def __init__(self, total: int) -> None:
        self.total, self.done = total, 0

    def __call__(self, size: int) -> None:
        self.done += size
        sys.stderr.write(f"\r  {self.done * 100 // self.total:3d}% of {self.total / 1e6:.0f} MB")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
