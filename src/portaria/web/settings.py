"""Settings: the name and logo shown in the app, the door screen address, and accounts."""

from __future__ import annotations

import secrets
import time

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select

from portaria.db import Camera, User, audit, get_setting, set_setting
from portaria.users import AccountError, create_user, set_password
from portaria.web.common import flash, page, require_user, services

router = APIRouter()
admin = APIRouter(dependencies=[Depends(require_user)])

LOGO_MAX_SIDE = 512


@admin.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    s = services(request)
    with s.db.session() as session:
        display_name = get_setting(session, "display_name") or ""
        door_key = get_setting(session, "door_screen_key")
        door_cameras = session.scalars(select(Camera).where(Camera.device == "door").order_by(Camera.name)).all()
        users = session.scalars(select(User).order_by(User.username)).all()
    return page(request, "settings.html", section="settings", display_name=display_name, door_key=door_key,
                door_cameras=door_cameras, users=users, cfg=s.cfg, base_url=str(request.base_url).rstrip("/"))


@admin.post("/settings/identity")
def save_identity(request: Request, display_name: str = Form("")) -> RedirectResponse:
    with services(request).db.session() as session:
        set_setting(session, "display_name", display_name.strip()[:60])
        audit(session, request.session["user"], "name changed", display_name.strip()[:60])
    flash(request, "Changes saved.")
    return RedirectResponse("/settings#identity", status_code=303)


@admin.post("/settings/logo")
def upload_logo(request: Request, logo: UploadFile = File(...)) -> RedirectResponse:
    """Stored as PNG whatever was sent: decoding and re-encoding also drops anything that isn't the picture."""
    image = cv2.imdecode(np.frombuffer(logo.file.read(), np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim not in (2, 3):
        flash(request, "This file is not a picture the system can read. Use PNG or JPG.", "bad")
        return RedirectResponse("/settings#identity", status_code=303)
    scale = LOGO_MAX_SIDE / max(image.shape[:2])
    if scale < 1:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    s = services(request)
    path = s.cfg.paths.data_dir / "branding" / "logo.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".png", image)
    path.write_bytes(buffer.tobytes())
    with s.db.session() as session:
        set_setting(session, "logo_version", str(int(time.time())))
        audit(session, request.session["user"], "logo changed")
    flash(request, "Logo updated.")
    return RedirectResponse("/settings#identity", status_code=303)


@admin.post("/settings/logo/remove")
def remove_logo(request: Request) -> RedirectResponse:
    s = services(request)
    (s.cfg.paths.data_dir / "branding" / "logo.png").unlink(missing_ok=True)
    with s.db.session() as session:
        set_setting(session, "logo_version", "")
        audit(session, request.session["user"], "logo removed")
    flash(request, "Logo removed.")
    return RedirectResponse("/settings#identity", status_code=303)


@admin.post("/settings/door-screen/new-key")
def new_door_key(request: Request) -> RedirectResponse:
    with services(request).db.session() as session:
        set_setting(session, "door_screen_key", secrets.token_urlsafe(12))
        audit(session, request.session["user"], "door screen address changed")
    flash(request, "New door screen address created. Open it again on the tablet; the old one no longer works.")
    return RedirectResponse("/settings#door-screen", status_code=303)


@admin.post("/settings/users")
def add_user(request: Request, username: str = Form(""), password: str = Form("")) -> RedirectResponse:
    try:
        with services(request).db.session() as session:
            create_user(session, username, password)
            audit(session, request.session["user"], "account created", username.strip())
    except AccountError as e:
        flash(request, str(e), "bad")
    else:
        flash(request, "Account {name} created.", name=username.strip())
    return RedirectResponse("/settings#accounts", status_code=303)


@admin.post("/settings/password")
def change_password(request: Request, password: str = Form(""), password_again: str = Form("")) -> RedirectResponse:
    if password != password_again:
        flash(request, "The passwords don't match.", "bad")
        return RedirectResponse("/settings#accounts", status_code=303)
    try:
        with services(request).db.session() as session:
            set_password(session, request.session["user"], password)
            audit(session, request.session["user"], "password changed")
    except AccountError as e:
        flash(request, str(e), "bad")
    else:
        request.session.pop("default_password", None)
        flash(request, "Password changed.")
    return RedirectResponse("/settings#accounts", status_code=303)


@router.get("/branding/logo.png")
def logo(request: Request) -> FileResponse:
    """Public: the sign-in page and the door screen show it too."""
    path = services(request).cfg.paths.data_dir / "branding" / "logo.png"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000"})


router.include_router(admin)
