"""Signing in and out, the language switch and closing standing notices.
The first account is admin/admin, created on first start."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from live_tracker.db import audit
from live_tracker.i18n import LANGUAGES
from live_tracker.users import DEFAULT_PASSWORD, authenticate
from live_tracker.web.common import page, safe_next, services

router = APIRouter()

NOTICES = ("default-password", "test-mode")  # toasts that stay until closed, then until the next sign-in


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> HTMLResponse:
    return page(request, "login.html", next=safe_next(next), username="", error="")


@router.post("/login", response_model=None)
def login(
    request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")
) -> HTMLResponse | RedirectResponse:
    with services(request).db.session() as session:
        user = authenticate(session, username, password)
        if user:
            audit(session, user.username, "signed in")
    if user is None:
        return page(request, "login.html", next=safe_next(next), username=username,
                    error="Wrong user name or password.", status_code=400)
    request.session["user"] = user.username
    request.session["default_password"] = password == DEFAULT_PASSWORD  # shows the reminder to change it
    request.session.pop("hidden_notices", None)
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    language = request.session.get("lang")
    request.session.clear()
    if language:
        request.session["lang"] = language
    return RedirectResponse("/login", status_code=303)


@router.post("/language")
def set_language(request: Request, lang: str = Form(...), next: str = Form("/")) -> RedirectResponse:
    if lang in LANGUAGES:
        request.session["lang"] = lang
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/notices/{key}/hide", status_code=204)
def hide_notice(request: Request, key: str) -> Response:
    if key not in NOTICES:
        raise HTTPException(404)
    hidden = request.session.get("hidden_notices", [])
    if key not in hidden:
        request.session["hidden_notices"] = [*hidden, key]
    return Response(status_code=204)
