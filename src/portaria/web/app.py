"""The web UI app: people, today's entries, records, cameras, live view and settings, plus the door screen.

Every text is English in the code and templates, and translated through portaria.i18n.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from portaria.camera import redact
from portaria.config import Config
from portaria.db import Database, ensure_setting
from portaria.people import FaceTools, People
from portaria.users import ensure_default_user
from portaria.web import auth, cameras, kiosk, monitor, people, settings
from portaria.web.common import LoginRequired, Services, aspect
from portaria.web.vision_client import VisionClient

HERE = Path(__file__).parent
log = logging.getLogger(__name__)


def create_app(cfg: Config) -> FastAPI:
    db = Database(cfg.db_path)
    db.migrate()
    with db.session() as session:
        secret = ensure_setting(session, "session_secret", lambda: secrets.token_urlsafe(32))
        ensure_setting(session, "door_screen_key", lambda: secrets.token_urlsafe(12))
        if ensure_default_user(session):
            log.warning("created the first account: user admin, password admin. Change the password in Settings.")

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["redact"] = redact
    templates.env.filters["aspect"] = aspect

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", max_age=12 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.services = Services(
        cfg=cfg,
        db=db,
        people=People(cfg, FaceTools(cfg)),
        vision=VisionClient(cfg.preview),
        templates=templates,
    )

    @app.exception_handler(LoginRequired)
    async def go_to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    for module in (auth, monitor, people, cameras, settings, kiosk):
        app.include_router(module.router)
    return app
