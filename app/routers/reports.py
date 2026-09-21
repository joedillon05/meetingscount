"""
Compliance reports. This is the artifact a probation officer attaches to a
file, so it is deliberately conservative in its wording: it reports what
MeetingsCount confirmed, never that someone "did not attend".

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

from .. import compliance
from ..auth import audit, require
from ..db import get_db
from ..models import CheckIn, County, Meeting, MeetingSource, Participant, Role, User
from ..reviewing import latest_decisions

router = APIRouter(prefix="/api/reports", tags=["reports"])
STAFF = (Role.officer, Role.admin)


def _build(db: Session, p: Participant, tz: str, days: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(CheckIn, Meeting).join(Meeting, Meeting.id == CheckIn.meeting_id)
        .where(CheckIn.user_id == p.user_id, CheckIn.checked_in_at >= since,
               Meeting.source != MeetingSource.personal)
        .order_by(CheckIn.checked_in_at.asc())
    ).all()
    decisions = {k: v.value for k, v in latest_decisions(db, [c.id for c, _ in rows]).items()}

    lines = []
    for c, m in rows:
        lines.append({
            "date": c.checked_in_at.astimezone(timezone.utc).isoformat(),
            "meeting": m.name, "category": m.category.value,
            "counts_toward_requirement": m.counts_toward_requirement,
            "result": c.status.value, "confidence": c.confidence,
            "distance_ft": round(c.distance_ft, 1),
            "checked_out": c.checked_out_at is not None,
            "review": decisions.get(c.id),
        })

    body = {
        "participant": p.user.name, "case_number": p.case_number,
        "period_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": compliance.summarize(db, p, tz),
        "requirements": [
            {"category": r.category.value if r.category else "any",
             "count_required": r.count_required, "period": r.period.value,
             "order_reference": r.order_reference,
             "history": [h.as_dict() for h in compliance.history(db, r, tz, periods=12)]}
            for r in p.requirements
        ],
        "check_ins": lines,
        "note": ("MeetingsCount reports confirmed attendance only. An entry that is not "
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
    report = _build(db, p, tz, days)
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
