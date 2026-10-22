"""
Keeping the promise on the consent screen: exact locations are deleted after
MC_PIN_RETENTION_DAYS.

The purge nulls the coordinates and the IP address on check-ins older than
the window. The distance in feet, the verdict and the reasons stay, so the
compliance record is whole but the location history is gone.

It runs on its own: once when the app starts and then every few hours while
it is awake (app/main.py), and a county admin can also run it by hand from
the dashboard. Either way it writes an audit row saying how many.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import audit
from .config import settings
from .db import SessionLocal
from .models import CheckIn, User

log = logging.getLogger("meetingscount.retention")

EVERY_HOURS = 6


def cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=settings.PIN_RETENTION_DAYS)


def purge_pins(db: Session, *, by: User | None = None, now: datetime | None = None,
               detail: str = "on schedule") -> dict:
    """Drop raw coordinates older than the window. Returns how many and the cutoff."""
    now = now or datetime.now(timezone.utc)
    limit = cutoff(now)
    rows = db.scalars(select(CheckIn).where(
        CheckIn.checked_in_at < limit, CheckIn.pin_purged_at.is_(None),
        CheckIn.lat.is_not(None))).all()
    for c in rows:
        c.lat = c.lng = c.checkout_lat = c.checkout_lng = None
        c.ip = None
        c.pin_purged_at = now
    db.commit()
    if rows or by is not None:
        audit(db, by, "run_retention",
              detail=f"purged {len(rows)} pins older than {settings.PIN_RETENTION_DAYS}d, {detail}")
    return {"purged": len(rows), "cutoff": limit.isoformat()}


def run_once() -> dict | None:
    """One pass with its own session. Logs and swallows errors: a failed purge
    must never take the app down with it."""
    db = SessionLocal()
    try:
        out = purge_pins(db)
        if out["purged"]:
            log.info("retention: purged %d pins older than %d days",
                     out["purged"], settings.PIN_RETENTION_DAYS)
        return out
    except Exception:                                        # noqa: BLE001
        log.exception("retention: the purge failed; will try again later")
        db.rollback()
        return None
    finally:
        db.close()


async def loop(every_hours: float = EVERY_HOURS) -> None:
    """Run at start, then every `every_hours` for as long as the app is awake."""
    while True:
        await asyncio.to_thread(run_once)
        await asyncio.sleep(every_hours * 3600)
