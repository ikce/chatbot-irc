from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from .bible_broadcaster import broadcaster as bible_broadcaster
from .config import settings
from .db import init_db
from .irc_client import supervisor
from .moderation import patterns as patterns_mod
from .moderation import spamhaus, tracelog
from .moderation import whitelist as whitelist_mod
from .routes import api as api_routes
from .routes import moderation as moderation_routes
from .routes import ui as ui_routes
from .routes import ws as ws_routes


logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth.announce_setup_if_needed()  # logge le code de setup si aucun compte
    whitelist_mod.refresh()   # pré-charge le cache whitelist
    patterns_mod.refresh()    # compile les regex patterns actifs (P3)
    tracelog.start_worker()   # worker background pour l'écriture tracelog
    spamhaus.start_worker()   # download/refresh listes Spamhaus DROP/EDROP
    await supervisor.start()
    bible_broadcaster.bind(supervisor)
    await bible_broadcaster.start()
    try:
        yield
    finally:
        await bible_broadcaster.stop()
        await supervisor.stop()
        await spamhaus.stop_worker()
        await tracelog.stop_worker()


app = FastAPI(title="chatbot-irc", lifespan=lifespan)

app.state.templates = templates

# Tout est protégé (HTTP + WebSocket) sauf /login et /healthz — cf. auth.py.
app.add_middleware(auth.AuthMiddleware)

app.include_router(auth.build_router(templates))
app.include_router(ui_routes.build_router(templates))
app.include_router(moderation_routes.build_router(templates))
app.include_router(api_routes.router, prefix="/api")
app.include_router(ws_routes.router)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"
