"""
The founders' back office: tasks, the hours log, timesheets, expenses.

For the people who run ActionsCount+, which today is two of us. Nothing
here is visible to a county: every route needs the superuser role, and a
superuser is only ever made from the command line (tools/make_staff.py).

The money rules, in one place:

    an hours entry is worth its minutes at the rate in force on that day
    a timesheet freezes its hours and amount when it is submitted
    you cannot approve, send back or pay your own timesheet
    you cannot reimburse an expense you paid yourself
    owed to someone = approved but unpaid timesheets + unreimbursed expenses

Money is whole cents and time is whole minutes, end to end.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit, require
from ..db import get_db
from ..models import (
    County, Role, TeamExpense, TeamHours, TeamRate, TeamTask, TeamTaskNote, TeamTaskStatus,
    TeamTimesheet, TeamTimesheetStatus, User,
)
from ..schemas import (
    TeamDecisionIn, TeamExpenseIn, TeamExpensePatch, TeamHoursIn, TeamHoursPatch, TeamNoteIn,
    TeamPaymentIn, TeamRateIn, TeamTaskIn, TeamTaskPatch, TeamTimesheetIn,
)

router = APIRouter(prefix="/api/team", tags=["team"])
TEAM = require(Role.superuser)

DONE_SHOWN_DAYS = 60       # finished tasks drop off the list after this
HOURS_SHOWN_DAYS = 120     # the hours log shows this far back, plus anything unsubmitted


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Who is on the team, and what their time is worth
# --------------------------------------------------------------------------- #

def members(db: Session) -> list[User]:
    return db.scalars(select(User).where(User.role == Role.superuser, User.active.is_(True))
                      .order_by(User.name)).all()


def _member(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if not u or u.role != Role.superuser or not u.active:
        raise HTTPException(422, "That person is not on the team.")
    return u


def _rates(db: Session, user_id: int) -> list[TeamRate]:
    return db.scalars(select(TeamRate).where(TeamRate.user_id == user_id)
                      .order_by(TeamRate.effective_on, TeamRate.id)).all()


def rate_on(rates: list[TeamRate], day: date) -> TeamRate | None:
    """The rate in force on a day: the latest one that had started by then."""
    current = None
    for r in rates:
        if r.effective_on <= day:
            current = r
    return current


def value_of(minutes: int, cents_per_hour: int) -> int:
    return round(minutes * cents_per_hour / 60)


def balance(db: Session, member: User) -> dict:
    rates = _rates(db, member.id)
    free = db.scalars(select(TeamHours).where(TeamHours.user_id == member.id,
                                              TeamHours.timesheet_id.is_(None))).all()
    unsubmitted_cents = unpriced = 0
    for h in free:
        r = rate_on(rates, h.worked_on)
        if r is None:
            unpriced += h.minutes
        else:
            unsubmitted_cents += value_of(h.minutes, r.cents_per_hour)
    sheets = db.scalars(select(TeamTimesheet).where(TeamTimesheet.user_id == member.id)).all()
    by = {s: [t for t in sheets if t.status == s] for s in TeamTimesheetStatus}
    paid_cents = sum(t.paid_cents if t.paid_cents is not None else t.amount_cents
                     for t in by[TeamTimesheetStatus.paid])
    submitted = sum(t.amount_cents for t in by[TeamTimesheetStatus.submitted])
    approved = sum(t.amount_cents for t in by[TeamTimesheetStatus.approved])
    expenses_owed = sum(e.amount_cents for e in db.scalars(
        select(TeamExpense).where(TeamExpense.paid_by_id == member.id,
                                  TeamExpense.reimbursed_at.is_(None))))
    return {
        "unsubmitted_minutes": sum(h.minutes for h in free),
        "unsubmitted_cents": unsubmitted_cents,
        "unpriced_minutes": unpriced,
        "submitted_minutes": sum(t.minutes for t in by[TeamTimesheetStatus.submitted]),
        "submitted_cents": submitted,
        "approved_minutes": sum(t.minutes for t in by[TeamTimesheetStatus.approved]),
        "approved_cents": approved,
        "paid_minutes": sum(t.minutes for t in by[TeamTimesheetStatus.paid]),
        "paid_cents": paid_cents,
        "expenses_owed_cents": expenses_owed,
        # What the company owes this person today, and what their time has
        # come to altogether, paid or not.
        "owed_cents": approved + expenses_owed,
        "earned_cents": unsubmitted_cents + submitted + approved + paid_cents,
    }


def person_out(db: Session, u: User, me: User) -> dict:
    current = rate_on(_rates(db, u.id), date.today())
    return {"id": u.id, "name": u.name, "is_you": u.id == me.id,
            "rate": ({"cents_per_hour": current.cents_per_hour,
                      "effective_on": current.effective_on.isoformat()} if current else None),
            "balance": balance(db, u)}


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: User = Depends(TEAM)):
    tasks = db.scalars(select(TeamTask).where(TeamTask.status != TeamTaskStatus.done)).all()
    counties = db.scalars(select(County).where(County.self_serve.is_(False)).order_by(County.name)).all()
    people = [person_out(db, u, user) for u in members(db)]
    return {
        "people": people,
        "counties": [{"id": c.id, "name": c.name} for c in counties],
        "open_tasks": len(tasks),
        "my_open_tasks": sum(1 for t in tasks if t.assignee_id == user.id),
        "awaiting_me": sum(1 for t in db.scalars(select(TeamTimesheet).where(
            TeamTimesheet.status == TeamTimesheetStatus.submitted, TeamTimesheet.user_id != user.id))),
        "owed_total_cents": sum(p["balance"]["owed_cents"] for p in people),
    }


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

def task_out(t: TeamTask, names: dict[int, str], counties: dict[int, str]) -> dict:
    return {
        "id": t.id, "title": t.title, "details": t.details, "status": t.status.value,
        "assignee_id": t.assignee_id, "assignee": names.get(t.assignee_id),
        "created_by": names.get(t.created_by_id),
        "county_id": t.county_id, "county": counties.get(t.county_id),
        "due_on": t.due_on.isoformat() if t.due_on else None,
        "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat(),
        "done_at": t.done_at.isoformat() if t.done_at else None,
        "notes": [{"id": n.id, "author": names.get(n.author_id), "author_id": n.author_id,
                   "body": n.body, "at": n.created_at.isoformat()} for n in t.notes],
    }


def _names(db: Session) -> dict[int, str]:
    return {u.id: u.name for u in db.scalars(select(User).where(User.role == Role.superuser))}


def _counties(db: Session) -> dict[int, str]:
    return {c.id: c.name for c in db.scalars(select(County))}


def _check_task_links(db: Session, data: dict) -> None:
    if data.get("assignee_id") is not None:
        _member(db, data["assignee_id"])
    if data.get("county_id") is not None and not db.get(County, data["county_id"]):
        raise HTTPException(422, "No county with that id.")


@router.get("/tasks")
def list_tasks(db: Session = Depends(get_db), user: User = Depends(TEAM)):
    """Everything not done, plus what was finished recently. Soonest due first."""
    cutoff = utcnow() - timedelta(days=DONE_SHOWN_DAYS)
    rows = [t for t in db.scalars(select(TeamTask)).all()
            if t.status != TeamTaskStatus.done or (t.done_at and t.done_at >= cutoff)]
    order = {TeamTaskStatus.open: 0, TeamTaskStatus.waiting: 1, TeamTaskStatus.done: 2}
    rows.sort(key=lambda t: (order[t.status], t.due_on is None, t.due_on or date.max,
                             -(t.done_at or t.created_at).timestamp()))
    names, counties = _names(db), _counties(db)
    return [task_out(t, names, counties) for t in rows]


@router.post("/tasks", status_code=201)
def add_task(body: TeamTaskIn, request: Request, db: Session = Depends(get_db),
             user: User = Depends(TEAM)):
    data = body.model_dump()
    _check_task_links(db, data)
    t = TeamTask(title=body.title.strip(), details=(body.details or "").strip() or None,
                 assignee_id=body.assignee_id, county_id=body.county_id, due_on=body.due_on,
                 created_by_id=user.id)
    db.add(t)
    db.commit()
    audit(db, user, "team_add_task", target_type="team_task", target_id=t.id, detail=t.title,
          request=request)
    return task_out(t, _names(db), _counties(db))


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, body: TeamTaskPatch, request: Request,
                db: Session = Depends(get_db), user: User = Depends(TEAM)):
    t = db.get(TeamTask, task_id)
    if not t:
        raise HTTPException(404, "task not found")
    data = body.model_dump(exclude_unset=True)
    _check_task_links(db, data)
    changed = []
    if "title" in data and data["title"].strip() != t.title:
        t.title = data["title"].strip()
        changed.append("title")
    if "details" in data:
        t.details = (data["details"] or "").strip() or None
        changed.append("details")
    for field in ("assignee_id", "county_id", "due_on"):
        if field in data and data[field] != getattr(t, field):
            setattr(t, field, data[field])
            changed.append(field)
    if "status" in data and data["status"] != t.status:
        t.status = data["status"]
        t.done_at = utcnow() if t.status == TeamTaskStatus.done else None
        changed.append(f"status to {t.status.value}")
    if changed:
        t.updated_at = utcnow()
        db.commit()
        audit(db, user, "team_update_task", target_type="team_task", target_id=t.id,
              detail=f"{t.title}: {', '.join(changed)}", request=request)
    return task_out(t, _names(db), _counties(db))


@router.post("/tasks/{task_id}/notes", status_code=201)
def add_note(task_id: int, body: TeamNoteIn, request: Request, db: Session = Depends(get_db),
             user: User = Depends(TEAM)):
    t = db.get(TeamTask, task_id)
    if not t:
        raise HTTPException(404, "task not found")
    db.add(TeamTaskNote(task_id=t.id, author_id=user.id, body=body.body.strip()))
    t.updated_at = utcnow()
    db.commit()
    db.refresh(t)
    audit(db, user, "team_note_task", target_type="team_task", target_id=t.id, detail=t.title,
          request=request)
    return task_out(t, _names(db), _counties(db))


# --------------------------------------------------------------------------- #
# The hours log
# --------------------------------------------------------------------------- #

def hours_out(h: TeamHours, rates: list[TeamRate], sheets: dict[int, TeamTimesheet]) -> dict:
    r = rate_on(rates, h.worked_on)
    sheet = sheets.get(h.timesheet_id) if h.timesheet_id else None
    return {
        "id": h.id, "user_id": h.user_id, "worked_on": h.worked_on.isoformat(),
        "minutes": h.minutes, "note": h.note,
        "task_id": h.task_id, "task": h.task.title if h.task else None,
        "cents": value_of(h.minutes, r.cents_per_hour) if r else None,
        "timesheet_id": h.timesheet_id,
        "timesheet_status": sheet.status.value if sheet else None,
        "editable": h.timesheet_id is None,
    }


@router.get("/hours")
def list_hours(user_id: int | None = None, days: int = HOURS_SHOWN_DAYS,
               db: Session = Depends(get_db), user: User = Depends(TEAM)):
    """Someone's log, newest first: the last few months plus anything not yet on a timesheet."""
    who = _member(db, user_id) if user_id else user
    since = date.today() - timedelta(days=max(1, min(days, 3660)))
    rows = db.scalars(select(TeamHours).where(TeamHours.user_id == who.id)
                      .order_by(TeamHours.worked_on.desc(), TeamHours.id.desc())).all()
    rows = [h for h in rows if h.timesheet_id is None or h.worked_on >= since]
    sheets = {t.id: t for t in db.scalars(select(TeamTimesheet).where(TeamTimesheet.user_id == who.id))}
    rates = _rates(db, who.id)
    return [hours_out(h, rates, sheets) for h in rows]


def _check_task(db: Session, task_id: int | None) -> None:
    if task_id is not None and not db.get(TeamTask, task_id):
        raise HTTPException(422, "No task with that id.")


def _own_free_hours(db: Session, hours_id: int, user: User) -> TeamHours:
    h = db.get(TeamHours, hours_id)
    if not h or h.user_id != user.id:
        raise HTTPException(404, "hours entry not found")
    if h.timesheet_id is not None:
        raise HTTPException(409, "These hours are on a timesheet. Have it sent back first.")
    return h


@router.post("/hours", status_code=201)
def log_hours(body: TeamHoursIn, request: Request, db: Session = Depends(get_db),
              user: User = Depends(TEAM)):
    if body.worked_on > date.today() + timedelta(days=1):
        raise HTTPException(422, "That day has not happened yet.")
    _check_task(db, body.task_id)
    h = TeamHours(user_id=user.id, worked_on=body.worked_on, minutes=body.minutes,
                  note=body.note.strip(), task_id=body.task_id)
    db.add(h)
    db.commit()
    audit(db, user, "team_log_hours", target_type="team_hours", target_id=h.id,
          detail=f"{body.minutes} min on {body.worked_on.isoformat()}: {h.note}", request=request)
    return hours_out(h, _rates(db, user.id), {})


@router.patch("/hours/{hours_id}")
def edit_hours(hours_id: int, body: TeamHoursPatch, request: Request,
               db: Session = Depends(get_db), user: User = Depends(TEAM)):
    h = _own_free_hours(db, hours_id, user)
    data = body.model_dump(exclude_unset=True)
    if "task_id" in data:
        _check_task(db, data["task_id"])
    if "note" in data:
        data["note"] = data["note"].strip()
    for field, value in data.items():
        setattr(h, field, value)
    h.updated_at = utcnow()
    db.commit()
    audit(db, user, "team_edit_hours", target_type="team_hours", target_id=h.id,
          detail=", ".join(data), request=request)
    return hours_out(h, _rates(db, user.id), {})


@router.delete("/hours/{hours_id}", status_code=204)
def delete_hours(hours_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(TEAM)):
    h = _own_free_hours(db, hours_id, user)
    detail = f"{h.minutes} min on {h.worked_on.isoformat()}: {h.note}"
    db.delete(h)
    db.commit()
    audit(db, user, "team_delete_hours", target_type="team_hours", target_id=hours_id,
          detail=detail, request=request)
    return None


@router.get("/rates")
def list_rates(user_id: int | None = None, db: Session = Depends(get_db),
               user: User = Depends(TEAM)):
    who = _member(db, user_id) if user_id else user
    names = _names(db)
    return [{"id": r.id, "user_id": r.user_id, "cents_per_hour": r.cents_per_hour,
             "effective_on": r.effective_on.isoformat(), "set_by": names.get(r.set_by_id),
             "created_at": r.created_at.isoformat()} for r in reversed(_rates(db, who.id))]


@router.post("/rates", status_code=201)
def set_rate(body: TeamRateIn, request: Request, db: Session = Depends(get_db),
             user: User = Depends(TEAM)):
    """
    A new rate from a date. Either founder can set either rate; the audit
    log says who did. Hours already on a timesheet keep the amount they
    were submitted at.
    """
    who = _member(db, body.user_id)
    r = TeamRate(user_id=who.id, cents_per_hour=body.cents_per_hour,
                 effective_on=body.effective_on, set_by_id=user.id)
    db.add(r)
    db.commit()
    audit(db, user, "team_set_rate", target_type="user", target_id=who.id,
          detail=f"{who.name}: {body.cents_per_hour / 100:.2f}/h from {body.effective_on.isoformat()}",
          request=request)
    return {"id": r.id, "user_id": r.user_id, "cents_per_hour": r.cents_per_hour,
            "effective_on": r.effective_on.isoformat()}


# --------------------------------------------------------------------------- #
# Timesheets
# --------------------------------------------------------------------------- #

def timesheet_out(t: TeamTimesheet, names: dict[int, str], *, entries: bool = False) -> dict:
    out = {
        "id": t.id, "user_id": t.user_id, "person": names.get(t.user_id),
        "starts_on": t.starts_on.isoformat(), "ends_on": t.ends_on.isoformat(),
        "status": t.status.value, "minutes": t.minutes, "amount_cents": t.amount_cents,
        "note": t.note, "submitted_at": t.submitted_at.isoformat(),
        "decided_by": names.get(t.decided_by_id),
        "decided_at": t.decided_at.isoformat() if t.decided_at else None,
        "decision_note": t.decision_note,
        "paid_by": names.get(t.paid_by_id),
        "paid_on": t.paid_on.isoformat() if t.paid_on else None,
        "paid_cents": t.paid_cents, "paid_note": t.paid_note,
        "entries": len(t.hours),
    }
    if entries:
        out["hours"] = [{"id": h.id, "worked_on": h.worked_on.isoformat(), "minutes": h.minutes,
                         "note": h.note, "task": h.task.title if h.task else None} for h in t.hours]
    return out


@router.get("/timesheets")
def list_timesheets(db: Session = Depends(get_db), user: User = Depends(TEAM)):
    rows = db.scalars(select(TeamTimesheet).order_by(TeamTimesheet.submitted_at.desc())).all()
    names = _names(db)
    return [timesheet_out(t, names) for t in rows]


@router.get("/timesheets/{sheet_id}")
def timesheet_detail(sheet_id: int, db: Session = Depends(get_db), user: User = Depends(TEAM)):
    t = db.get(TeamTimesheet, sheet_id)
    if not t:
        raise HTTPException(404, "timesheet not found")
    return timesheet_out(t, _names(db), entries=True)


@router.post("/timesheets", status_code=201)
def submit_timesheet(body: TeamTimesheetIn, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(TEAM)):
    """
    Gather your unsubmitted hours in a date range into one timesheet, priced
    at the rate in force on each day. Nothing is pulled in from outside the
    range, so a stray entry from last quarter waits for its own sheet.
    """
    if body.ends_on < body.starts_on:
        raise HTTPException(422, "The timesheet ends before it starts.")
    hours = db.scalars(select(TeamHours).where(
        TeamHours.user_id == user.id, TeamHours.timesheet_id.is_(None),
        TeamHours.worked_on >= body.starts_on, TeamHours.worked_on <= body.ends_on)
        .order_by(TeamHours.worked_on)).all()
    if not hours:
        raise HTTPException(422, "No unsubmitted hours in that range.")
    rates = _rates(db, user.id)
    amount = 0
    for h in hours:
        r = rate_on(rates, h.worked_on)
        if r is None:
            raise HTTPException(422, f"No hourly rate is set for {h.worked_on.isoformat()}. "
                                     "Set a rate on the Money tab first.")
        amount += value_of(h.minutes, r.cents_per_hour)
    t = TeamTimesheet(user_id=user.id, starts_on=body.starts_on, ends_on=body.ends_on,
                      minutes=sum(h.minutes for h in hours), amount_cents=amount,
                      note=(body.note or "").strip() or None)
    db.add(t)
    db.flush()
    for h in hours:
        h.timesheet_id = t.id
    db.commit()
    db.refresh(t)
    audit(db, user, "team_submit_timesheet", target_type="team_timesheet", target_id=t.id,
          detail=f"{t.minutes} min, {amount / 100:.2f}, {t.starts_on.isoformat()} to {t.ends_on.isoformat()}",
          request=request)
    return timesheet_out(t, _names(db), entries=True)


def _someone_elses(db: Session, sheet_id: int, user: User, *allowed: TeamTimesheetStatus) -> TeamTimesheet:
    t = db.get(TeamTimesheet, sheet_id)
    if not t:
        raise HTTPException(404, "timesheet not found")
    if t.user_id == user.id:
        raise HTTPException(403, "Not your own timesheet. The other founder does this.")
    if t.status not in allowed:
        raise HTTPException(409, f"This timesheet is {t.status.value}.")
    return t


@router.post("/timesheets/{sheet_id}/approve")
def approve_timesheet(sheet_id: int, body: TeamDecisionIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(TEAM)):
    t = _someone_elses(db, sheet_id, user, TeamTimesheetStatus.submitted)
    t.status = TeamTimesheetStatus.approved
    t.decided_by_id, t.decided_at = user.id, utcnow()
    t.decision_note = (body.note or "").strip() or None
    db.commit()
    audit(db, user, "team_approve_timesheet", target_type="team_timesheet", target_id=t.id,
          detail=f"{_names(db).get(t.user_id)}: {t.amount_cents / 100:.2f}", request=request)
    return timesheet_out(t, _names(db))


@router.post("/timesheets/{sheet_id}/return")
def return_timesheet(sheet_id: int, body: TeamDecisionIn, request: Request,
                     db: Session = Depends(get_db), user: User = Depends(TEAM)):
    """Send it back. Its hours are free to edit and resubmit; the sheet stays as a record."""
    if not (body.note or "").strip():
        raise HTTPException(422, "Say why, so they know what to change.")
    t = _someone_elses(db, sheet_id, user, TeamTimesheetStatus.submitted, TeamTimesheetStatus.approved)
    t.status = TeamTimesheetStatus.returned
    t.decided_by_id, t.decided_at = user.id, utcnow()
    t.decision_note = body.note.strip()
    for h in list(t.hours):
        h.timesheet_id = None
    db.commit()
    audit(db, user, "team_return_timesheet", target_type="team_timesheet", target_id=t.id,
          detail=t.decision_note, request=request)
    return timesheet_out(t, _names(db))


@router.post("/timesheets/{sheet_id}/pay")
def pay_timesheet(sheet_id: int, body: TeamPaymentIn, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(TEAM)):
    """Record that the money went. The app moves no money itself."""
    t = _someone_elses(db, sheet_id, user, TeamTimesheetStatus.approved)
    t.status = TeamTimesheetStatus.paid
    t.paid_by_id = user.id
    t.paid_on = body.paid_on or date.today()
    t.paid_cents = body.paid_cents if body.paid_cents is not None else t.amount_cents
    t.paid_note = (body.note or "").strip() or None
    db.commit()
    audit(db, user, "team_pay_timesheet", target_type="team_timesheet", target_id=t.id,
          detail=f"{_names(db).get(t.user_id)}: {t.paid_cents / 100:.2f}"
                 + (f" ({t.paid_note})" if t.paid_note else ""), request=request)
    return timesheet_out(t, _names(db))


# --------------------------------------------------------------------------- #
# Expenses
# --------------------------------------------------------------------------- #

def expense_out(e: TeamExpense, names: dict[int, str]) -> dict:
    return {
        "id": e.id, "spent_on": e.spent_on.isoformat(), "vendor": e.vendor,
        "amount_cents": e.amount_cents, "note": e.note,
        "paid_by_id": e.paid_by_id, "paid_by": names.get(e.paid_by_id),
        "created_by": names.get(e.created_by_id), "created_at": e.created_at.isoformat(),
        "reimbursed_at": e.reimbursed_at.isoformat() if e.reimbursed_at else None,
        "reimbursed_by": names.get(e.reimbursed_by_id), "reimbursed_note": e.reimbursed_note,
        "owed": e.owed,
    }


@router.get("/expenses")
def list_expenses(db: Session = Depends(get_db), user: User = Depends(TEAM)):
    rows = db.scalars(select(TeamExpense).order_by(TeamExpense.spent_on.desc(),
                                                   TeamExpense.id.desc())).all()
    names = _names(db)
    return [expense_out(e, names) for e in rows]


def _check_payer(db: Session, paid_by_id: int | None) -> None:
    if paid_by_id is not None:
        _member(db, paid_by_id)


@router.post("/expenses", status_code=201)
def add_expense(body: TeamExpenseIn, request: Request, db: Session = Depends(get_db),
                user: User = Depends(TEAM)):
    _check_payer(db, body.paid_by_id)
    e = TeamExpense(spent_on=body.spent_on, vendor=body.vendor.strip(), amount_cents=body.amount_cents,
                    note=(body.note or "").strip() or None, paid_by_id=body.paid_by_id,
                    created_by_id=user.id)
    db.add(e)
    db.commit()
    audit(db, user, "team_add_expense", target_type="team_expense", target_id=e.id,
          detail=f"{e.vendor} {e.amount_cents / 100:.2f}", request=request)
    return expense_out(e, _names(db))


def _open_expense(db: Session, expense_id: int) -> TeamExpense:
    e = db.get(TeamExpense, expense_id)
    if not e:
        raise HTTPException(404, "expense not found")
    if e.reimbursed_at:
        raise HTTPException(409, "This expense has been reimbursed and is part of the record now.")
    return e


@router.patch("/expenses/{expense_id}")
def edit_expense(expense_id: int, body: TeamExpensePatch, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(TEAM)):
    e = _open_expense(db, expense_id)
    data = body.model_dump(exclude_unset=True)
    if "paid_by_id" in data:
        _check_payer(db, data["paid_by_id"])
    for field in ("vendor", "note"):
        if field in data and data[field] is not None:
            data[field] = data[field].strip() or None
    for field, value in data.items():
        setattr(e, field, value)
    db.commit()
    audit(db, user, "team_edit_expense", target_type="team_expense", target_id=e.id,
          detail=", ".join(data), request=request)
    return expense_out(e, _names(db))


@router.delete("/expenses/{expense_id}", status_code=204)
def delete_expense(expense_id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(TEAM)):
    e = _open_expense(db, expense_id)
    detail = f"{e.vendor} {e.amount_cents / 100:.2f} on {e.spent_on.isoformat()}"
    db.delete(e)
    db.commit()
    audit(db, user, "team_delete_expense", target_type="team_expense", target_id=expense_id,
          detail=detail, request=request)
    return None


@router.post("/expenses/{expense_id}/reimburse")
def reimburse_expense(expense_id: int, body: TeamDecisionIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(TEAM)):
    e = _open_expense(db, expense_id)
    if e.paid_by_id is None:
        raise HTTPException(409, "The business paid this. There is nothing to reimburse.")
    if e.paid_by_id == user.id:
        raise HTTPException(403, "You paid this one. The other founder marks it reimbursed.")
    e.reimbursed_at, e.reimbursed_by_id = utcnow(), user.id
    e.reimbursed_note = (body.note or "").strip() or None
    db.commit()
    audit(db, user, "team_reimburse_expense", target_type="team_expense", target_id=e.id,
          detail=f"{_names(db).get(e.paid_by_id)}: {e.vendor} {e.amount_cents / 100:.2f}",
          request=request)
    return expense_out(e, _names(db))
