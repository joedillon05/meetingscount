"""
Compliance reports. This is the artifact a probation officer attaches to a
file, so it is deliberately conservative in its wording: it reports what
ActionsCount+ confirmed, never that someone "did not attend".

Each report carries a SHA-256 of its own content so a printed copy can be
checked against the record later.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import compliance, confirming
from ..auth import audit, require
from ..db import get_db
from ..models import CheckIn, County, Meeting, MeetingSource, Participant, Program, Role, User
from ..reviewing import states_for

router = APIRouter(prefix="/api/reports", tags=["reports"])
STAFF = (Role.officer, Role.admin)


OUTCOME = {compliance.COUNTS: "counted", compliance.PENDING: "pending",
           compliance.NOT_COUNTED: "not counted"}

DECISION_WORDS = {"approved": "counted after review", "denied": "not counted after review",
                  "more_info": "officer asked the participant for more information"}


def _build(db: Session, p: Participant, tz: str, days: int, *, by: User | None = None) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(CheckIn, Meeting).join(Meeting, Meeting.id == CheckIn.meeting_id)
        .where(CheckIn.user_id == p.user_id, CheckIn.checked_in_at >= since)
        .order_by(CheckIn.checked_in_at.asc())
    ).all()
    states = states_for(db, [c for c, _ in rows])
    asks = confirming.for_checkins(db, [c for c, _ in rows])

    lines = []
    for c, m in rows:
        st = states[c.id]
        review = st.latest
        vouched = confirming.best(asks.get(c.id, []))
        lines.append({
            "date": c.checked_in_at.astimezone(timezone.utc).isoformat(),
            "meeting": m.name, "address": m.address, "category": m.category.value,
            "counts_toward_requirement": m.counts_toward_requirement,
            # Not from the list: the participant added it where they were.
            "added_by_participant": m.added_by_participant,
            "recorded_at": f"{m.lat:.4f}, {m.lng:.4f}" if m.added_by_participant else None,
            "result": c.status.value, "confidence": c.confidence,
            "distance_ft": round(c.distance_ft, 1),
            "checked_out": c.checked_out_at is not None,
            "checked_out_at": c.checked_out_at.isoformat() if c.checked_out_at else None,
            "review": review.decision.value if review else None,
            "reviewed_by": review.reviewer.name if review and review.reviewer else None,
            "reviewed_at": review.created_at.isoformat() if review else None,
            "review_note": review.note if review else None,
            "review_words": DECISION_WORDS.get(review.decision.value) if review else None,
            # Someone the participant asked, and what they said, if anything.
            "confirmation": confirming.confirmation_out(vouched) if vouched else None,
            "confirmation_words": confirming.words(vouched, tz),
            # What this check-in did for the requirement, in one word.
            "outcome": OUTCOME[compliance.outcome(c, review.decision if review else None)],
        })

    county = db.get(County, p.county_id)
    program = db.get(Program, p.program_id) if p.program_id else None
    requirements = []
    for r in p.requirements:
        history = [h.as_dict() for h in compliance.history(db, r, tz, periods=max(2, days // 7))]
        full = [h for h in history if not h["partial"]]
        met = sum(1 for h in full if h["status"] == "met")
        requirements.append({
            "category": r.category.value if r.category else "any",
            "count_required": r.count_required, "period": r.period.value,
            "order_reference": r.order_reference,
            "starts_on": r.starts_on.isoformat(),
            "ends_on": r.ends_on.isoformat() if r.ends_on else None,
            "history": history,
            "periods_met": met, "periods_covered": len(full),
            "partial_periods": len(history) - len(full),
        })

    body = {
        "participant": p.user.name, "case_number": p.case_number,
        "county": county.name if county else None,
        "timezone": tz,
        "program": program.name if program else None,
        "officer": p.officer.name if p.officer else None,
        "enrolled_on": p.enrolled_on.isoformat(),
        "period_days": days,
        "period_from": since.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": by.name if by else None,
        "summary": compliance.summarize(db, p, tz),
        "requirements": requirements,
        "check_ins": lines,
        "note": ("ActionsCount+ reports confirmed attendance only. An entry that is not "
                 "confirmed means this system could not verify it, which is not by itself "
                 "evidence that the participant was absent."),
    }
    body["content_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    return body


@router.get("/participant/{participant_id}")
def participant_report(participant_id: int, request: Request, days: int = 90,
                       db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    p = db.get(Participant, participant_id)
    if not p or (user.role != Role.superuser and p.county_id != user.county_id):
        raise HTTPException(404, "participant not found")
    tz = (db.get(County, user.county_id).timezone if user.county_id else "America/Detroit")
    report = _build(db, p, tz, days, by=user)
    audit(db, user, "export_report", target_type="participant", target_id=p.id,
          detail=f"{days}d hash={report['content_hash'][:12]}", request=request)
    return report


@router.get("/participant/{participant_id}.csv", response_class=PlainTextResponse)
def participant_report_csv(participant_id: int, request: Request, days: int = 90,
                           db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    import csv, io                                                  # noqa: E401
    data = participant_report(participant_id, request, days, db, user)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "meeting", "category", "counts", "result", "confidence",
                "distance_ft", "checked_out", "review"])
    for line in data["check_ins"]:
        w.writerow([line["date"], line["meeting"], line["category"],
                    line["counts_toward_requirement"], line["result"], line["confidence"],
                    line["distance_ft"], line["checked_out"], line["review"] or ""])
    w.writerow([])
    w.writerow(["content_hash", data["content_hash"]])
    return buf.getvalue()
