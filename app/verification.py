"""
Check-in verification engine.

The idea: a check-in is judged by a list of independent *criteria*. Each one
looks at the same context (where the user says they are, when, how accurate the
GPS fix is, what network they're on, and the meeting they're checking into) and
returns a small result: pass/fail, a 0..1 score, a weight, and a human-readable
reason.

The engine combines those into a confidence score (0-100) and a status:

    confirmed  - every hard criterion passed and confidence is high
    flagged    - nothing hard failed, but something looks off (weak GPS,
                 possible VPN, borderline distance). Needs a human look.
    rejected   - a hard criterion failed (too far away, wrong time).

Why this shape: later features from the roadmap (VPN detection, urban radius
adjustment, late arrival / early checkout, secondary confirmation) each become
one more Criterion class. Nothing else has to change, and the reasons list gives
the admin (or a court official) an audit trail for every decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from .geo import feet_to_meters, haversine_m, meters_to_feet

DEFAULT_RADIUS_FT = 500
DEFAULT_TIME_WINDOW_MIN = 60


@dataclass
class MeetingSpec:
    id: int
    name: str
    lat: float
    lng: float
    starts_at: datetime                 # timezone-aware UTC
    duration_min: int = 60
    radius_ft: float = DEFAULT_RADIUS_FT   # adjustable per meeting (urban areas)
    time_window_min: int = DEFAULT_TIME_WINDOW_MIN
    category: str = "public"           # public | private | support_group | community_service | therapy


@dataclass
class CheckInContext:
    meeting: MeetingSpec
    lat: float
    lng: float
    at: datetime                        # timezone-aware UTC
    accuracy_m: Optional[float] = None  # from navigator.geolocation; None if unknown
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    # Free-form bag for signals future criteria may need (device id, phone, etc.)
    extra: dict = field(default_factory=dict)

    @property
    def distance_m(self) -> float:
        return haversine_m(self.lat, self.lng, self.meeting.lat, self.meeting.lng)


@dataclass
class CriterionResult:
    name: str
    passed: bool
    score: float          # 0..1 contribution to confidence
    weight: float         # relative importance
    hard: bool            # if True and failed -> check-in is rejected outright
    reason: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """What gets stored on the check-in row and shown to a reviewer."""
        return {"name": self.name, "passed": self.passed, "score": round(self.score, 3),
                "weight": self.weight, "hard": self.hard, "reason": self.reason,
                "data": self.data}


class Criterion(Protocol):
    name: str
    weight: float
    hard: bool

    def evaluate(self, ctx: CheckInContext) -> CriterionResult: ...


# --------------------------------------------------------------------------- #
# Criteria
# --------------------------------------------------------------------------- #

class DistanceCriterion:
    """User must be inside the meeting's radius. Score decays with distance."""
    name = "distance"
    weight = 3.0
    hard = True

    def evaluate(self, ctx: CheckInContext) -> CriterionResult:
        radius_m = feet_to_meters(ctx.meeting.radius_ft)
        d = ctx.distance_m
        passed = d <= radius_m
        # Full score inside half the radius, tapering to 0.5 at the edge.
        score = 1.0 if d <= radius_m / 2 else max(0.0, 1.0 - 0.5 * (d / radius_m))
        if not passed:
            score = 0.0
        return CriterionResult(
            self.name, passed, score, self.weight, self.hard,
            f"{meters_to_feet(d):.0f} ft from meeting (limit {ctx.meeting.radius_ft:.0f} ft)",
            {"distance_ft": round(meters_to_feet(d), 1), "radius_ft": ctx.meeting.radius_ft},
        )


class TimeWindowCriterion:
    """Check-in must be within N minutes of the meeting start (before or after)."""
    name = "time_window"
    weight = 2.0
    hard = True

    def evaluate(self, ctx: CheckInContext) -> CriterionResult:
        window = timedelta(minutes=ctx.meeting.time_window_min)
        delta = ctx.at - ctx.meeting.starts_at
        passed = abs(delta) <= window
        minutes = delta.total_seconds() / 60
        # Late arrivals cost a little; early is fine.
        score = 1.0 if minutes <= 0 else max(0.0, 1.0 - 0.5 * (minutes / ctx.meeting.time_window_min))
        if not passed:
            score = 0.0
        when = "early" if minutes < 0 else "late"
        return CriterionResult(
            self.name, passed, score, self.weight, self.hard,
            f"{abs(minutes):.0f} min {when} (window ±{ctx.meeting.time_window_min} min)",
            {"minutes_from_start": round(minutes, 1)},
        )


class GpsAccuracyCriterion:
    """
    Soft check. A fix with a huge accuracy circle (dense city, indoors) is not
    proof the user is far away, but it's also not proof they're close. We keep
    the check-in and lower confidence so it shows up as 'flagged' if bad enough.
    """
    name = "gps_accuracy"
    weight = 1.5
    hard = False

    def __init__(self, good_m: float = 30.0, poor_m: float = 150.0):
        self.good_m, self.poor_m = good_m, poor_m

    def evaluate(self, ctx: CheckInContext) -> CriterionResult:
        acc = ctx.accuracy_m
        if acc is None:
            return CriterionResult(self.name, False, 0.3, self.weight, self.hard,
                                   "no GPS accuracy reported", {"accuracy_m": None})
        if acc <= self.good_m:
            score = 1.0
        elif acc >= self.poor_m:
            score = 0.0
        else:
            score = 1.0 - (acc - self.good_m) / (self.poor_m - self.good_m)
        # Extra: if the accuracy circle is bigger than the geofence, the fix
        # can't really place them inside it.
        radius_m = feet_to_meters(ctx.meeting.radius_ft)
        if acc > radius_m:
            score = min(score, 0.2)
        return CriterionResult(
            self.name, score >= 0.5, score, self.weight, self.hard,
            f"GPS accuracy ±{acc:.0f} m", {"accuracy_m": acc},
        )


class NetworkCriterion:
    """
    Placeholder for VPN / proxy / datacenter-IP detection.

    Wire an IP-intelligence provider (ipinfo, IPQualityScore, MaxMind, etc.)
    into `lookup`. Until then it reports 'unknown' and stays neutral so it
    doesn't swing results. It's soft on purpose: a VPN alone shouldn't reject
    someone, but it should flag the check-in for review.
    """
    name = "network"
    weight = 1.5
    hard = False

    def __init__(self, lookup=None):
        self.lookup = lookup  # callable(ip) -> {"vpn": bool, "proxy": bool, ...}

    def evaluate(self, ctx: CheckInContext) -> CriterionResult:
        if not ctx.ip or self.lookup is None:
            return CriterionResult(self.name, True, 0.6, self.weight, self.hard,
                                   "network check not configured", {"ip": ctx.ip})
        info = self.lookup(ctx.ip) or {}
        suspicious = bool(info.get("vpn") or info.get("proxy") or info.get("hosting"))
        return CriterionResult(
            self.name, not suspicious, 0.0 if suspicious else 1.0, self.weight, self.hard,
            "VPN/proxy detected" if suspicious else "network looks normal",
            {"ip": ctx.ip, **info},
        )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

@dataclass
class Verdict:
    status: str                     # confirmed | flagged | rejected
    confidence: int                 # 0..100
    results: list[CriterionResult]
    distance_ft: float

    @property
    def reasons(self) -> list[str]:
        return [f"{r.name}: {r.reason}" for r in self.results]


DEFAULT_CRITERIA: list[Criterion] = [
    DistanceCriterion(),
    TimeWindowCriterion(),
    GpsAccuracyCriterion(),
    NetworkCriterion(),
]


def verify(ctx: CheckInContext, criteria: Optional[list[Criterion]] = None,
           confirm_threshold: int = 70) -> Verdict:
    criteria = criteria or DEFAULT_CRITERIA
    results = [c.evaluate(ctx) for c in criteria]

    total_w = sum(r.weight for r in results) or 1.0
    confidence = round(100 * sum(r.score * r.weight for r in results) / total_w)

    if any(r.hard and not r.passed for r in results):
        status = "rejected"
    elif confidence >= confirm_threshold and all(r.passed for r in results):
        status = "confirmed"
    else:
        status = "flagged"

    return Verdict(status, confidence, results, round(meters_to_feet(ctx.distance_m), 1))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
