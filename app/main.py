"""
MeetingsCount API.

    uvicorn app.main:app --reload

    /              participant check-in (phone)
    /officer       officer dashboard: caseload, review queue, participant detail
    /ops           map, simulator, engine internals (developer tool)
    /docs          API reference

Two switches govern safety, both ON for local development and both of which
MUST be off in production:

    MC_DEMO=0       stops fake timestamps and demo reseeding
    MC_DEV_AUTH=0   stops header-based login, requires a Cognito token

The app refuses to start if either is on while MC_ENV=production.
"""
from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import create_all
from .routers import admin, caseload, checkins, meetings, reports, reviews

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

if os.environ.get("MC_ENV") == "production" and (settings.DEMO or settings.DEV_AUTH):
    raise RuntimeError(
        "Refusing to start: MC_DEMO and MC_DEV_AUTH must both be 0 in production."
    )

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Dev convenience only. Production runs `alembic upgrade head` on deploy.
    if settings.DATABASE_URL.startswith("sqlite"):
        create_all()
    yield


app = FastAPI(
    title="MeetingsCount",
    version="0.3.0",
    description="Verifiable attendance for court-ordered meetings.",
    lifespan=lifespan,
)


for r in (meetings.router, checkins.router, caseload.router,
          reviews.router, reports.router, admin.router):
    app.include_router(r)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/officer", include_in_schema=False)
def officer():
    return FileResponse(os.path.join(STATIC_DIR, "officer.html"))


@app.get("/ops", include_in_schema=False)
def ops():
    return FileResponse(os.path.join(STATIC_DIR, "ops.html"))


@app.get("/admin", include_in_schema=False)
def admin_redirect():
    # The old URL from v2. Keep it working.
    return FileResponse(os.path.join(STATIC_DIR, "officer.html"))


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Load balancer health check. No database hit, no auth."""
    return {"ok": True, "version": app.version}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
