"""Compare recognition models on the enrollment photos, to choose a model and an accept threshold."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from portaria.config import Config
from portaria.embedders import Embedder, load_embedder
from portaria.faces import FaceDetector
from portaria.gallery import IMAGE_SUFFIXES, Gallery, PhotoRejected, build_gallery, embed_photo
from portaria.models import model_path


def leave_one_out(gallery: Gallery) -> tuple[np.ndarray, np.ndarray]:
    """For each photo: its best score against the same person's other photos (genuine), and against
    anyone else's (impostor). That is the comparison the door makes at every attempt."""
    names = np.array(gallery.labels)
    scores = gallery.vectors @ gallery.vectors.T
    np.fill_diagonal(scores, -np.inf)
    same = names[:, None] == names[None, :]
    genuine = np.where(same, scores, -np.inf).max(axis=1, initial=-np.inf)
    impostor = np.where(~same, scores, -np.inf).max(axis=1, initial=-np.inf)
    return genuine[np.isfinite(genuine)], impostor[np.isfinite(impostor)]


def outsider_scores(folder: Path, gallery: Gallery, detector: FaceDetector, embedder: Embedder, cfg: Config) -> np.ndarray:
    """Best score of each photo of a non-employee against everyone enrolled."""
    scores = []
    for photo in sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES):
        try:
            vector = embed_photo(photo, detector, embedder, cfg.detection)
        except PhotoRejected:
            continue
        scores.append(float((gallery.vectors @ vector).max()))
    return np.array(scores)


def evaluate(cfg: Config, models: list[str], outsiders: Path | None = None) -> None:
    detector = FaceDetector(
        model_path(cfg.paths.models_dir, "yunet"), cfg.detection.score_threshold, cfg.detection.detect_width
    )
    threshold = cfg.recognition.accept_threshold
    for name in models:
        embedder = load_embedder(name, cfg.paths.models_dir)
        enrollment = build_gallery(cfg.enroll_dir, detector, embedder, cfg.detection)
        gallery = enrollment.gallery
        genuine, impostor = leave_one_out(gallery)
        if outsiders is not None and gallery.labels:
            impostor = np.concatenate([impostor, outsider_scores(outsiders, gallery, detector, embedder, cfg)])

        print(f"\n== {name}: {len(gallery.people)} people, {len(gallery.labels)} usable photos, "
              f"{len(enrollment.rejected)} rejected")
        if len(genuine) == 0 or len(impostor) == 0:
            print("   needs at least 2 people, with 2 or more photos each")
            continue
        print(f"   same person       lowest {genuine.min():.3f}   mean {genuine.mean():.3f}")
        print(f"   different people  highest {impostor.max():.3f}  mean {impostor.mean():.3f}")
        print(f"   gap               {genuine.min() - impostor.max():+.3f}   (positive: one threshold separates everyone)")
        rejects, accepts = int((genuine < threshold).sum()), int((impostor >= threshold).sum())
        print(f"   at accept_threshold {threshold:.2f}: {rejects}/{len(genuine)} employee photos rejected, "
              f"{accepts}/{len(impostor)} other faces accepted")
        print(f"   suggested accept_threshold: {impostor.max() + 0.10:.2f}   (highest different-people score + 0.10)")
