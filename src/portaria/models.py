"""Model files: where they come from, and downloading them with checksum verification."""

from __future__ import annotations

import hashlib
import logging
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str
    sha256: str
    license: str


MODELS = {
    "yunet": ModelFile(
        "face_detection_yunet_2023mar.onnx",
        "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "MIT",
    ),
    "sface": ModelFile(
        "face_recognition_sface_2021dec.onnx",
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "Apache-2.0",
    ),
    # Only the recognition model from the AuraFace repository: its detector and landmark files are
    # byte-for-byte InsightFace's models, which are licensed for non-commercial research only.
    "auraface": ModelFile(
        "auraface_v1_glintr100.onnx",
        "https://huggingface.co/fal/AuraFace-v1/resolve/main/glintr100.onnx",
        "a7933ea5330113b01c9b60351d8f4c33003f145d8470ac5f0e52ee2effe25c60",
        "Apache-2.0",
    ),
}


def model_path(models_dir: Path, name: str) -> Path:
    path = models_dir / MODELS[name].filename
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; run: python -m portaria download-models")
    return path


def download_models(models_dir: Path) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        target = models_dir / model.filename
        if target.exists() and _sha256(target) == model.sha256:
            log.info("%s: already downloaded", name)
            continue
        log.info("%s (%s): downloading %s", name, model.license, model.url)
        partial = target.with_name(target.name + ".part")
        urllib.request.urlretrieve(model.url, partial, _progress)
        sys.stderr.write("\n")
        if _sha256(partial) != model.sha256:
            partial.unlink()
            raise RuntimeError(f"{name}: checksum mismatch, download discarded")
        partial.replace(target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _progress(blocks: int, block_size: int, total: int) -> None:
    if total > 0:
        sys.stderr.write(f"\r  {min(100, blocks * block_size * 100 // total):3d}% of {total / 1e6:.0f} MB")
