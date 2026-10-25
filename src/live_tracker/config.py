"""Settings: defaults here, overridden by config.toml (see config.example.toml)."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

RECOGNITION_MODELS = ("auraface", "sface")

# Keys from earlier versions that no longer do anything: skipped, so old config files keep working.
_RETIRED = {("preview", "enabled")}


@dataclass
class CameraConfig:
    source: str = ""  # only used by `import` (first camera) and as the default for `probe`
    stale_after_s: float = 2.0
    reconnect_delay_s: float = 2.0


@dataclass
class DetectionConfig:
    detect_width: int = 640
    score_threshold: float = 0.8
    min_face_px: int = 80
    max_yaw: float = 0.25         # at the door: about 30 degrees of head turn
    enroll_max_yaw: float = 0.38  # enrollment photos: about 40 degrees, since some variety helps
    min_sharpness: float = 30.0


@dataclass
class RecognitionConfig:
    model: str = "auraface"
    accept_threshold: float = 0.45
    min_margin: float = 0.08


@dataclass
class LivenessConfig:
    threshold: float = 0.5  # how likely a face must look to be real, 0 to 1, once the liveness model is downloaded


@dataclass
class DecisionConfig:
    window_s: float = 1.5
    min_votes: int = 3
    rearm_after_s: float = 1.0
    person_cooldown_s: float = 5.0
    unknown_cooldown_s: float = 10.0
    unknown_after_s: float = 2.0


@dataclass
class DoorConfig:
    mode: str = "shadow"


@dataclass
class PreviewConfig:
    # The recognition service's local HTTP port (video and status), read by the web UI.
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class WebConfig:
    host: str = "0.0.0.0"  # every network this computer is on, so phones and the door tablet can open it
    port: int = 8000
    language: str = "pt"  # default for new sessions, the door screen and the video labels


@dataclass
class StorageConfig:
    retention_days: int = 90  # events and snapshots older than this are deleted


@dataclass
class PathsConfig:
    data_dir: Path = Path("data")
    models_dir: Path = Path("models")


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    liveness: LivenessConfig = field(default_factory=LivenessConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    door: DoorConfig = field(default_factory=DoorConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    web: WebConfig = field(default_factory=WebConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)

    @property
    def db_path(self) -> Path:
        return self.paths.data_dir / "portaria.db"

    @property
    def enroll_dir(self) -> Path:
        """Photo folders per person: read by `evaluate`, and by `import` for the first setup."""
        return self.paths.data_dir / "enroll"


def load_config(path: Path | None) -> Config:
    """Defaults, overridden by `path` when it exists. Unknown sections or keys are errors, to catch typos."""
    config = Config()
    if path is None or not path.exists():
        return config
    with path.open("rb") as f:
        raw = tomllib.load(f)
    sections = {f.name for f in fields(config)}
    for section_name, values in raw.items():
        if section_name not in sections or not isinstance(values, dict):
            raise ValueError(f"{path}: unknown section [{section_name}]")
        section = getattr(config, section_name)
        keys = {f.name for f in fields(section)}
        for key, value in values.items():
            if (section_name, key) in _RETIRED:
                log.warning("%s: [%s] %s is no longer used; you can delete it", path, section_name, key)
                continue
            if key not in keys:
                raise ValueError(f"{path}: unknown key '{key}' in [{section_name}]")
            setattr(section, key, type(getattr(section, key))(value))
    return config
