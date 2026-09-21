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
from .routers import admin, caseload, checkins, me, meetings, reports, reviews

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


for r in (me.router, meetings.router, checkins.router, caseload.router,
          reviews.router, reports.router, admin.router):
    app.include_router(r)


@app.middleware("http")
async def cache_rules(request, call_next):
    """
    API responses carry personal data, so no browser or proxy may keep a copy.
    Pages and the app's own files are revalidated on every load (a cheap 304
    when unchanged), so a fix reaches phones on the next open rather than
    whenever a heuristic cache decides to let go.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    else:
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


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


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Served from the root so it can control "/", and never cached by the
    # browser's HTTP cache, or a fix to it could take a day to reach phones.
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.webmanifest"),
                        media_type="application/manifest+json")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Load balancer health check. No database hit, no auth."""
    return {"ok": True, "version": app.version}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
