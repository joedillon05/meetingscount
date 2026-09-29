"""Request and response shapes. Pydantic does the validation FastAPI documents."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import MeetingCategory, MeetingSource, RequirementPeriod, ReviewDecision, Role


class MeetingIn(BaseModel):
    name: str
    category: MeetingCategory = MeetingCategory.public
    source: MeetingSource = MeetingSource.staff
    address: Optional[str] = None
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    starts_at: datetime
    duration_min: int = Field(default=60, ge=5, le=1440)
    radius_ft: float = Field(default=500, ge=50, le=5280)
    time_window_min: int = Field(default=60, ge=5, le=480)


class CheckInIn(BaseModel):
    meeting_id: int
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: Optional[float] = None
    # Demo/simulator only, ignored unless MC_DEMO is on. The server clock wins.
    at: Optional[datetime] = None
    # Simulator only: pretend the request came from this IP.
    sim_ip: Optional[str] = None


class CheckOutIn(BaseModel):
    lat: Optional[float] = None
    lng: Optional[float] = None


class RequirementIn(BaseModel):
    participant_id: int
    category: Optional[MeetingCategory] = None
    count_required: int = Field(default=3, ge=1, le=50)
    period: RequirementPeriod = RequirementPeriod.week
    starts_on: datetime
    ends_on: Optional[datetime] = None
    order_reference: Optional[str] = None


class ReviewIn(BaseModel):
    decision: ReviewDecision
    note: Optional[str] = Field(default=None, max_length=2000)


class BulkReviewIn(BaseModel):
    checkin_ids: list[int] = Field(min_length=1, max_length=200)
    decision: ReviewDecision
    note: Optional[str] = Field(default=None, max_length=2000)


class DisputeIn(BaseModel):
    message: str = Field(min_length=3, max_length=2000)


class ParticipantIn(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    program_id: Optional[int] = None
    officer_id: Optional[int] = None
    case_number: Optional[str] = None


class ParticipantPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=32)
    case_number: Optional[str] = Field(default=None, max_length=64)
    program_id: Optional[int] = None
    officer_id: Optional[int] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    ends_on: Optional[datetime] = None
    active: Optional[bool] = None


class RequirementPatch(BaseModel):
    category: Optional[MeetingCategory] = None
    count_required: Optional[int] = Field(default=None, ge=1, le=50)
    period: Optional[RequirementPeriod] = None
    starts_on: Optional[datetime] = None
    ends_on: Optional[datetime] = None
    order_reference: Optional[str] = Field(default=None, max_length=120)


class StaffIn(BaseModel):
    name: str = Field(max_length=120)
    email: str = Field(max_length=200)
    role: Role = Role.officer


class StaffPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    role: Optional[Role] = None
    active: Optional[bool] = None


class ConsentIn(BaseModel):
    version: str


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    code: Optional[str] = Field(default=None, max_length=10)      # authenticator app


class CodeRequestIn(BaseModel):
    phone: str = Field(max_length=32)


class CodeVerifyIn(BaseModel):
    phone: str = Field(max_length=32)
    code: str = Field(max_length=10)


class PasswordIn(BaseModel):
    current_password: Optional[str] = Field(default=None, max_length=200)
    new_password: str = Field(default="", max_length=200)


class TotpIn(BaseModel):
    code: str = Field(max_length=10)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    role: Role
    county_id: Optional[int] = None
    email: Optional[str] = None


def checkin_out(c: Any, meeting: Any = None, *, include_pin: bool = False) -> dict:
    """
    Serialize a check-in. `include_pin` is False by default: coordinates are
    only sent to staff who are reviewing that specific check-in, and to the
    participant themselves. Principle 3 in the plan.
    """
    d = {
        "id": c.id, "meeting_id": c.meeting_id, "user_id": c.user_id,
        "checked_in_at": c.checked_in_at.isoformat(),
        "checked_out_at": c.checked_out_at.isoformat() if c.checked_out_at else None,
        "status": c.status.value, "confidence": c.confidence,
        "distance_ft": round(c.distance_ft, 1),
        "accuracy_m": c.accuracy_m,
        "reasons": c.reasons,
        "pin_purged": c.pin_purged_at is not None,
    }
    if include_pin:
        d |= {"lat": c.lat, "lng": c.lng, "ip": c.ip}
    if meeting is not None:
        d["meeting"] = {"id": meeting.id, "name": meeting.name,
                        "category": meeting.category.value, "source": meeting.source.value,
                        "address": meeting.address,
                        "lat": meeting.lat, "lng": meeting.lng, "radius_ft": meeting.radius_ft,
                        "starts_at": meeting.starts_at.isoformat(),
                        "ends_at": meeting.ends_at.isoformat(),
                        "duration_min": meeting.duration_min,
                        "time_window_min": meeting.time_window_min,
                        "counts": meeting.counts_toward_requirement}
    return d
