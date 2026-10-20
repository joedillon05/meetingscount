"""
Where each check-in stands with a human.

A check-in the engine could not settle (flagged or rejected) waits for an
officer. Reviews are append-only: changing a decision adds a row, nothing is
overwritten, and the latest row is the one that counts. Every screen and the
compliance maths read review state from here, so they cannot disagree.

    open      needs a decision. Never reviewed, or the officer asked for more
              information and the participant has since replied.
    waiting   the officer asked for more information; the participant has not
              answered yet. Counts as pending, never as a miss.
    decided   approved or denied.
    none      nothing to review (a confirmed check-in nobody has touched).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CheckIn, CheckInStatus, Dispute, Review, ReviewDecision

NEEDS_REVIEW = (CheckInStatus.flagged, CheckInStatus.rejected)


@dataclass
class ReviewState:
    state: str                                   # open | waiting | decided | none
    history: list[Review] = field(default_factory=list)
    notes: list[Dispute] = field(default_factory=list)
    replied: bool = False                        # a participant note after "more info"

    @property
    def latest(self) -> Review | None:
        return self.history[-1] if self.history else None

    @property
    def decision(self) -> ReviewDecision | None:
        return self.latest.decision if self.latest else None


def states_for(db: Session, checkins: list[CheckIn]) -> dict[int, ReviewState]:
    """Review state for many check-ins in two queries."""
    ids = [c.id for c in checkins] or [-1]
    history: dict[int, list[Review]] = {}
    for r in db.scalars(select(Review).where(Review.checkin_id.in_(ids))
                        .order_by(Review.created_at, Review.id)):
        history.setdefault(r.checkin_id, []).append(r)
    notes: dict[int, list[Dispute]] = {}
    for d in db.scalars(select(Dispute).where(Dispute.checkin_id.in_(ids))
                        .order_by(Dispute.created_at, Dispute.id)):
        notes.setdefault(d.checkin_id, []).append(d)

    out = {}
    for c in checkins:
        h, n = history.get(c.id, []), notes.get(c.id, [])
        latest = h[-1] if h else None
        replied = False
        if latest is None:
            state = "open" if c.status in NEEDS_REVIEW else "none"
        elif latest.decision == ReviewDecision.more_info:
            replied = any(d.created_at > latest.created_at for d in n)
            state = "open" if replied else "waiting"
        else:
            state = "decided"
        out[c.id] = ReviewState(state, h, n, replied)
    return out


def latest_decisions(db: Session, checkin_ids: list[int]) -> dict[int, ReviewDecision]:
    """The decision that counts for each check-in: the most recent one."""
    out: dict[int, ReviewDecision] = {}
    for r in db.scalars(select(Review).where(Review.checkin_id.in_(checkin_ids or [-1]))
                        .order_by(Review.created_at, Review.id)):
        out[r.checkin_id] = r.decision
    return out


def review_out(r: Review) -> dict:
    return {"id": r.id, "decision": r.decision.value, "note": r.note,
            "by": r.reviewer.name if r.reviewer else None, "at": r.created_at.isoformat()}


def note_out(d: Dispute) -> dict:
    return {"message": d.message, "created_at": d.created_at.isoformat()}


def note_required(decision: ReviewDecision, st: ReviewState, status: CheckInStatus) -> str | None:
    """
    Why a note is needed, or None. The participant reads every note, and a
    judge may, so anything that hurts or reverses a decision must say why.
    """
    if decision == ReviewDecision.denied:
        return "Say why it does not count. The participant will see this note."
    if decision == ReviewDecision.more_info:
        return "Write the question for the participant. They will see it and can reply."
    if st.decision in (ReviewDecision.approved, ReviewDecision.denied):
        return "Say why the decision is changing. The earlier decision stays in the record."
    if status == CheckInStatus.confirmed:
        return "This check-in was already confirmed. Say why you are reviewing it."
    return None


def reviewable(db: Session, user, *, mine_only: bool = False, ids: list[int] | None = None):
    """
    Check-ins a staff member may review: flagged, rejected, or already
    reviewed, in their county. Rows are
    (CheckIn, Meeting, User, Participant | None).
    """
    from sqlalchemy import or_

    from .auth import scope_to_county
    from .models import Meeting, MeetingSource, Participant, User

    q = (select(CheckIn, Meeting, User, Participant)
         .join(Meeting, Meeting.id == CheckIn.meeting_id)
         .join(User, User.id == CheckIn.user_id)
         .outerjoin(Participant, Participant.user_id == CheckIn.user_id)
         .where(or_(CheckIn.status.in_(NEEDS_REVIEW),
                    CheckIn.id.in_(select(Review.checkin_id)))))
    q = scope_to_county(q, CheckIn, user)
    if mine_only:
        q = q.where(Participant.officer_id == user.id)
    if ids is not None:
        q = q.where(CheckIn.id.in_(ids or [-1]))
    return db.execute(q).all()


def open_counts(db: Session, user) -> dict[int, int]:
    """Items needing a decision, per participant user id. Drives badges and the caseload."""
    rows = reviewable(db, user)
    states = states_for(db, [c for c, *_ in rows])
    out: dict[int, int] = {}
    for c, *_ in rows:
        if states[c.id].state == "open":
            out[c.user_id] = out.get(c.user_id, 0) + 1
    return out
