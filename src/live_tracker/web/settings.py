"""Settings: the name and logo shown in the app, accounts, the app's addresses and the door screen's, and
stopping the system.

The Database panel's actions are in data.py.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from live_tracker.db import STOP_REQUESTS, Camera, User, audit, bump, get_setting, set_setting
from live_tracker.users import AccountError, create_user, is_admin, remove_user, rename_user, set_admin, set_password
from live_tracker.web.common import (
    flash, on_this_computer, page, require_admin, require_user, services, web_addresses,
)
from live_tracker.web.data import summary
from live_tracker.web.vision_client import VisionClient

router = APIRouter()
admin = APIRouter(dependencies=[Depends(require_user)])

LOGO_MAX_SIDE = 512
SERVICE_STOP_WAIT_S = 20.0  # how long Stop the system waits for the recognition service
WEB_STOP_DELAY_S = 2.0  # then how long this web UI still answers, for the page saying it stopped


@admin.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    s = services(request)
    with s.db.session() as session:
        display_name = get_setting(session, "display_name") or ""
        door_key = get_setting(session, "door_screen_key")
        door_cameras = session.scalars(select(Camera).where(Camera.device == "door").order_by(Camera.name)).all()
        me = request.session["user"]
        users = session.scalars(select(User).order_by(User.username != me, User.username)).all()  # yours first
        admin_signed_in = is_admin(session, me)
    here, others = web_addresses(s.cfg)
    door_base_url = others[0] if others else str(request.base_url).rstrip("/")  # the address the tablet opens
    return page(request, "settings.html", section="settings", display_name=display_name, door_key=door_key,
                door_cameras=door_cameras, users=users, cfg=s.cfg, door_base_url=door_base_url,
                here_address=here, network_addresses=others, is_admin=admin_signed_in,
                data_folder=s.cfg.paths.data_dir.resolve(), on_this_computer=on_this_computer(request),
                summary=summary(request) if admin_signed_in else None, dated=False)


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


@admin.post("/settings/stop", response_model=None)
def stop_system(
    request: Request, confirm: bool = Form(False), user: str = Depends(require_admin)
) -> HTMLResponse | RedirectResponse:
    """Stops the recognition service, then this web UI, as closing both their windows would. Only from a browser
    on this computer: the system can only be started again here."""
    if not on_this_computer(request):
        raise HTTPException(403)
    if not confirm:
        flash(request, "Confirm that nothing is recorded until the system starts again.", "bad")
        return RedirectResponse("/settings#stop", status_code=303)
    s = services(request)
    running = s.vision.status() is not None
    with s.db.session() as session:
        bump(session, STOP_REQUESTS)
        audit(session, user, "system stopped")
    response = page(request, "stopped.html", service_stopped=not running or _service_stopped(s.vision))
    response.background = BackgroundTask(_stop_web_soon, s.stop_web)
    return response


def _service_stopped(vision: VisionClient) -> bool:
    """Whether the recognition service stops within SERVICE_STOP_WAIT_S. It stops its cameras first, and its
    port, which the status comes from, last."""
    deadline = time.monotonic() + SERVICE_STOP_WAIT_S
    while vision.status() is not None:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.25)
    return True


def _stop_web_soon(stop_web: Callable[[], None]) -> None:
    time.sleep(WEB_STOP_DELAY_S)  # meanwhile the browser loads the stopped page's styles and logo
    stop_web()


@admin.post("/settings/users")
def add_user(
    request: Request, username: str = Form(""), password: str = Form(""), administrator: bool = Form(False),
    user: str = Depends(require_admin),  # otherwise anyone could make themselves an administrator
) -> RedirectResponse:
    try:
        with services(request).db.session() as session:
            create_user(session, username, password, admin=administrator)
            audit(session, user, "account created", username.strip() + (" (administrator)" if administrator else ""))
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


@admin.post("/settings/users/{user_id}")
def edit_user(
    request: Request, user_id: int, username: str = Form(""), administrator: bool = Form(False),
    password: str = Form(""), user: str = Depends(require_admin),
) -> RedirectResponse:
    """Another account's name and role, and a new password when the person forgot theirs. Nothing is saved when
    any of it can't be, such as a name another account has."""
    try:
        with services(request).db.session() as session:
            account = _other_account(session, user_id, user)
            name, changes = account.username, []
            if administrator != account.is_admin:
                set_admin(session, account, administrator)
                changes.append("administrator" if administrator else "not administrator")
            if password:
                set_password(session, name, password)
                changes.append("new password")
            if username.strip() and username.strip() != name:
                rename_user(session, account, username)
                changes.append(f"renamed to {account.username}")
            if changes:
                audit(session, user, "account changed", f"{name}: {', '.join(changes)}")
            name = account.username
    except AccountError as e:
        flash(request, str(e), "bad")
    else:
        flash(request, "Account {name} saved.", name=name)
    return RedirectResponse("/settings#accounts", status_code=303)


@admin.post("/settings/users/{user_id}/delete")
def delete_user(
    request: Request, user_id: int, confirm: bool = Form(False), user: str = Depends(require_admin)
) -> RedirectResponse:
    if not confirm:
        flash(request, "Confirm that you understand this can't be undone.", "bad")
        return RedirectResponse("/settings#accounts", status_code=303)
    try:
        with services(request).db.session() as session:
            account = _other_account(session, user_id, user)
            name = account.username
            remove_user(session, account)
            audit(session, user, "account removed", name)
    except AccountError as e:
        flash(request, str(e), "bad")
    else:
        flash(request, "Account {name} removed.", name=name)
    return RedirectResponse("/settings#accounts", status_code=303)


def _other_account(session: Session, user_id: int, signed_in: str) -> User:
    """Someone else's account: yours changes in Change my password, and you can't remove it yourself."""
    account = session.get(User, user_id)
    if account is None:
        raise HTTPException(404)
    if account.username == signed_in:
        raise HTTPException(403)
    return account


@router.get("/branding/logo.png")
def logo(request: Request) -> FileResponse:
    """Public: the sign-in page and the door screen show it too."""
    path = services(request).cfg.paths.data_dir / "branding" / "logo.png"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000"})


router.include_router(admin)
