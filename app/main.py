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

import base64
import os
import secrets

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import create_all
from .routers import admin, auth, caseload, checkins, me, meetings, reports, reviews

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

if os.environ.get("MC_ENV") == "production" and (settings.DEMO or settings.DEV_AUTH):
    raise RuntimeError(
        "Refusing to start: MC_DEMO and MC_DEV_AUTH must both be 0 in production."
    )

if settings.DEMO:
    print("MeetingsCount: MC_DEMO is on. The sign-in screen lists the demo accounts and "
          "participant sign-in codes are shown on screen. Demo data only.")
if settings.DEV_AUTH:
    print("MeetingsCount: MC_DEV_AUTH is on. Anyone can claim to be anyone with a header. "
          "Local development only.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Dev convenience only. Production runs `alembic upgrade head` on deploy.
    if settings.DATABASE_URL.startswith("sqlite"):
        create_all()
    yield


IS_PRODUCTION = os.environ.get("MC_ENV") == "production"

app = FastAPI(
    title="MeetingsCount",
    version="0.4.0",
    description="Verifiable attendance for court-ordered meetings.",
    lifespan=lifespan,
    # The interactive API browser is a development tool. In production it is
    # one more thing to keep an eye on, for no benefit to anyone using this.
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

# Everything the pages need, and nothing else. Scripts and styles come from
# this origin only, so an injected <script src> has nowhere to load from.
# Map tiles are the one outside source, and only images.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https://*.tile.openstreetmap.org; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
       "form-action 'self'")


for r in (auth.router, me.router, meetings.router, checkins.router, caseload.router,
          reviews.router, reports.router, admin.router):
    app.include_router(r)


@app.middleware("http")
async def security_headers(request, call_next):
    """
    The headers a county security review looks for. Set here rather than at
    the load balancer, so they hold wherever this runs.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(self), camera=(), microphone=(), payment=()")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
    # The API browser loads its own assets from a CDN, so it keeps out of this.
    if not request.url.path.startswith(("/api/", "/docs", "/redoc", "/openapi")):
        response.headers.setdefault("Content-Security-Policy", CSP)
    return response


@app.middleware("http")
async def preview_gate(request, call_next):
    """
    A shared password in front of everything except the health check, when
    MC_ACCESS_PASSWORD is set. For demo deployments only: see the note in
    config.Settings.ACCESS_PASSWORD.
    """
    if not settings.ACCESS_PASSWORD or request.url.path == "/healthz":
        return await call_next(request)
    given = ""
    header = request.headers.get("authorization", "")
    if header[:6].lower() == "basic ":
        try:
            given = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")[2]
        except Exception:                                  # noqa: BLE001  malformed header
            given = ""
    if not secrets.compare_digest(given, settings.ACCESS_PASSWORD):
        return PlainTextResponse(
            "MeetingsCount preview. Ask Joe for the password.", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MeetingsCount preview", charset="UTF-8"'})
    return await call_next(request)


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


@app.get("/report", include_in_schema=False)
def report_page():
    """The printable attendance report. Takes ?participant=<id>&days=<n>."""
    return FileResponse(os.path.join(STATIC_DIR, "report.html"))


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
