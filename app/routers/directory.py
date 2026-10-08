"""
The meeting directory, as the phone and the dashboard see it, and the town a
participant chooses to look around.

The area is a town from the directory, stored as that town's centre. A
participant can pick it from a list, or let the phone read its location once
and pick the nearest town. Either way what is kept is the town, not where the
phone was.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import directory
from ..auth import audit, current_user, require
from ..db import get_db
from ..models import Role, User
from ..schemas import AreaIn

router = APIRouter(prefix="/api", tags=["directory"])
STAFF = (Role.officer, Role.admin)


@router.get("/directory")
def search(q: str | None = None, day: int | None = None, lat: float | None = None,
           lng: float | None = None, within_mi: float = directory.MAX_WITHIN_MI,
           limit: int = 60, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Search by name, town or zip, one weekday, near a point. Everyone signed in may look."""
    if day is not None and not 0 <= day <= 6:
        raise HTTPException(422, "day is 0 for Monday to 6 for Sunday")
    near = (lat, lng) if lat is not None and lng is not None else (
        (user.area_lat, user.area_lng) if user.role == Role.participant and user.area_lat is not None
        else None)
    rows = directory.search(db, q, weekday=day, near=near, within_mi=within_mi,
                            county_id=user.county_id, limit=max(1, min(limit, 200)))
    return [directory.series_out(s, d) for s, d in rows]


@router.get("/directory/places")
def places(q: str | None = None, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    return directory.places(db, q, county_id=user.county_id)


@router.get("/directory/stats")
def stats(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    return directory.stats(db, county_id=user.county_id)


@router.post("/my/area")
def set_area(body: AreaIn, request: Request, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    """
    Choose where to look for meetings. With a label, the point is a town the
    participant picked. Without one, the point is wherever the phone was and
    the nearest town in the directory is what gets kept.
    """
    label, lat, lng = body.label, body.lat, body.lng
    if not label:
        place = directory.nearest_place(db, lat, lng, county_id=user.county_id)
        if place is None:
            raise HTTPException(404, "There are no meetings in the directory yet.")
        label, lat, lng = place["label"], place["lat"], place["lng"]
    user.area_label, user.area_lat, user.area_lng = label[:80], lat, lng
    db.commit()
    audit(db, user, "set_area", target_type="user", target_id=user.id, detail=label, request=request)
    return {"label": user.area_label, "lat": user.area_lat, "lng": user.area_lng}


@router.delete("/my/area", status_code=204)
def clear_area(db: Session = Depends(get_db), user: User = Depends(current_user)):
    user.area_label = user.area_lat = user.area_lng = None
    db.commit()
