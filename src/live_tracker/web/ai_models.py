"""Settings, AI models: which models exist, whether they are downloaded, downloading and removing them.

A model is downloaded once for the whole system; each camera then chooses among the downloaded ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from live_tracker.db import MODELS_VERSION, Camera, audit, bump
from live_tracker.models import (
    CATALOG, FILES, MODELS, Downloads, Model, ModelFile, file_state, files_of, model_state, remove_files,
)
from live_tracker.objects import DETECTORS, ObjectSettings
from live_tracker.web.common import flash, fragment, page, require_user, services, translator

router = APIRouter(dependencies=[Depends(require_user)])

STATES = {
    "ready": "Ready",
    "missing": "Not downloaded",
    "damaged": "File damaged",
    "downloading": "Downloading",
    "failed": "Download failed",
    "planned": "Coming soon",
}


@dataclass
class Card:
    model: Model
    state: str  # a key of STATES
    files: list[tuple[ModelFile, str]] = field(default_factory=list)  # each file and its state
    percent: int = 0
    removable: bool = True
    toast: str = ""  # "ok" or "bad" when a download has just ended


@router.get("/settings/models", response_class=HTMLResponse)
def models_page(request: Request) -> HTMLResponse:
    cards = [_card(request, model) for model in CATALOG]
    return page(request, "settings_models.html", section="settings", cards=cards, states=STATES, gpu=_gpu())


@router.get("/settings/models/{key}", response_class=HTMLResponse)
def model_card(request: Request, key: str) -> HTMLResponse:
    """One card, refreshed while its download runs."""
    return fragment(request, "_model_card.html", card=_card(request, _model(key), announce=True), states=STATES)


@router.post("/settings/models/{key}/download", response_model=None)
def download(request: Request, key: str) -> HTMLResponse | RedirectResponse:
    model = _model(key)
    if not model.files:
        raise HTTPException(404)
    _downloads(request).start(key, files_of(model, services(request).cfg.recognition.model))
    with services(request).db.session() as session:
        audit(session, request.session["user"], "model download", key)
    if request.headers.get("HX-Request"):
        return fragment(request, "_model_card.html", card=_card(request, model), states=STATES)
    return RedirectResponse("/settings/models", status_code=303)


@router.post("/settings/models/{key}/remove")
def remove(request: Request, key: str) -> RedirectResponse:
    s = services(request)
    model = _model(key)
    if not model.files or not _removable(model) or _downloads(request).running(key):
        raise HTTPException(409)
    with s.db.session() as session:
        cameras = [camera.name for camera in session.scalars(select(Camera).order_by(Camera.name)) if _uses(camera, key)]
    if cameras:
        flash(request, "These cameras use this model: {cameras}. Choose another model for them first.", "bad",
              cameras=", ".join(cameras))
        return RedirectResponse("/settings/models", status_code=303)
    remove_files(s.cfg.paths.models_dir, files_of(model, s.cfg.recognition.model))
    with s.db.session() as session:
        bump(session, MODELS_VERSION)
        audit(session, request.session["user"], "model removed", key)
    flash(request, "Model removed: {name}.", name=translator(request)(model.name))
    return RedirectResponse("/settings/models", status_code=303)


def _removable(model: Model) -> bool:
    """Face recognition stays: People also uses its face finder to check every photo."""
    return model.key != "faces"


def _uses(camera: Camera, key: str) -> bool:
    """Whether the camera needs the model's files. People and objects runs the model its settings chose."""
    if camera.analysis != "objects":
        return camera.analysis == key
    return DETECTORS[ObjectSettings.parse(camera.model_settings).model][1] in MODELS[key].files


def _card(request: Request, model: Model, announce: bool = False) -> Card:
    s = services(request)
    models_dir = s.cfg.paths.models_dir
    files = files_of(model, s.cfg.recognition.model)
    card = Card(model, model_state(models_dir, files), [(FILES[f], file_state(models_dir, f)) for f in files],
                removable=_removable(model))
    job = _downloads(request).get(model.key)
    if job and not job.finished:
        card.state, card.percent = "downloading", min(99, job.done * 100 // job.total)
    elif job and job.error and card.state != "ready":
        card.state = "failed"
    if announce and job and job.finished and not job.announced:
        job.announced = True
        card.toast = "bad" if job.error else "ok"
    return card


def _model(key: str) -> Model:
    if key not in MODELS:
        raise HTTPException(404)
    return MODELS[key]


def _downloads(request: Request) -> Downloads:
    """One set of downloads per app, so a download keeps going while nobody looks at the page."""
    state = request.app.state
    if not hasattr(state, "downloads"):
        db = services(request).db

        def changed() -> None:  # the recognition service then loads the model for the cameras waiting for it
            with db.session() as session:
                bump(session, MODELS_VERSION)

        state.downloads = Downloads(services(request).cfg.paths.models_dir, changed)
    return state.downloads


def _gpu() -> bool:
    """Whether ONNX Runtime can use a GPU here. Without it, every model runs on the processor."""
    import onnxruntime

    return bool({"CUDAExecutionProvider", "DmlExecutionProvider"} & set(onnxruntime.get_available_providers()))
