"""Settings: defaults here, overridden by config.toml (see config.example.toml)."""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from contextlib import closing
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

RECOGNITION_MODELS = ("auraface", "sface")
DB_NAME = "live-tracker.db"
LEGACY_DB_NAME = "portaria.db"  # its name before the app was called Live Tracker

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
        """The database. One still under the app's old name is used where it is, until `start` renames it."""
        path, legacy = self.paths.data_dir / DB_NAME, self.paths.data_dir / LEGACY_DB_NAME
        return legacy if legacy.exists() and not path.exists() else path

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


def rename_legacy_database(cfg: Config) -> bool:
    """Gives a database still under the app's old name the new one, while nothing uses it: `start` calls this
    before starting anything. True when it was renamed.

    Closing the last connection folds SQLite's journal (-wal, -shm) into the main file and deletes it, so the one
    file left can move alone. When the journal stays, something still has the database open: it keeps its name.
    """
    path, legacy = cfg.paths.data_dir / DB_NAME, cfg.paths.data_dir / LEGACY_DB_NAME
    if path.exists() or not legacy.exists():
        return False
    try:
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if any(legacy.with_name(legacy.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            return False
        legacy.rename(path)
    except (OSError, sqlite3.Error) as e:
        log.warning("the database keeps its old name, %s, for now: %s", legacy.name, e)
        return False
    log.info("database renamed: %s is now %s", legacy.name, path.name)
    return True
