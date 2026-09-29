"""
Authentication and authorization.

Who is calling, in this order:

  1. A session cookie, set by signing in (staff with a password, participants
     with a one-time code). This is how the real app works.
  2. `X-MC-User: <id>` when MC_DEV_AUTH is on. Local development only: it is
     what the demo person-switcher uses, and it is off in any deployment.
  3. A Cognito JWT in `Authorization: Bearer`, if a county brings their own
     identity provider later.

Staff sessions are short and expire while idle; a participant's phone stays
signed in, because making someone re-enter a code in a basement with no
signal is how check-ins get missed.

`require(*roles)` is the dependency every protected route uses, and
`scope_to_county()` is what keeps one county out of another's data.
"""
from __future__ import annotations

import time
from typing import Callable

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import datetime, timedelta, timezone

from .config import settings
from .db import get_db
from .models import AuditLog, Role, Session as SessionRow, User
from .security import new_session_token, token_hash

_jwks_cache: dict = {"keys": None, "fetched": 0.0}


def _cognito_claims(token: str) -> dict:
    """Verify a Cognito access/ID token. Requires `pip install python-jose[cryptography]`."""
    import urllib.request, json                                   # noqa: E401
    from jose import jwt                                          # type: ignore

    if not settings.COGNITO_USER_POOL_ID:
        raise HTTPException(500, "Cognito is not configured")

    issuer = (f"https://cognito-idp.{settings.COGNITO_REGION}.amazonaws.com/"
              f"{settings.COGNITO_USER_POOL_ID}")

    if not _jwks_cache["keys"] or time.time() - _jwks_cache["fetched"] > 3600:
        with urllib.request.urlopen(f"{issuer}/.well-known/jwks.json", timeout=5) as r:
            _jwks_cache["keys"] = json.load(r)["keys"]
        _jwks_cache["fetched"] = time.time()

    header = jwt.get_unverified_header(token)
    key = next((k for k in _jwks_cache["keys"] if k["kid"] == header["kid"]), None)
    if key is None:
        raise HTTPException(401, "unknown signing key")
    try:
        return jwt.decode(token, key, algorithms=["RS256"],
                          audience=settings.COGNITO_CLIENT_ID or None, issuer=issuer)
    except Exception as exc:                                       # noqa: BLE001
        raise HTTPException(401, f"invalid token: {exc}") from exc


def session_user(request: Request, db: Session) -> User | None:
    """
    The signed-in user for this request's cookie, or None. Also enforces the
    idle timeout for staff and keeps `last_seen_at` fresh.
    """
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        return None
    row = db.scalar(select(SessionRow).where(SessionRow.token_hash == token_hash(token)))
    now = datetime.now(timezone.utc)
    if not row or row.revoked_at or row.expires_at <= now:
        return None
    user = db.get(User, row.user_id)
    if not user or not user.active:
        return None
    if user.role != Role.participant:
        idle = timedelta(minutes=settings.STAFF_IDLE_MINUTES)
        if now - row.last_seen_at > idle:
            row.revoked_at = now
            db.commit()
            return None
    # Cheap write, once a minute at most, so a busy screen is not a write storm.
    if now - row.last_seen_at > timedelta(minutes=1):
        row.last_seen_at = now
        db.commit()
    request.state.session_id = row.id
    return user


def current_user(
    request: Request,
    db: Session = Depends(get_db),
    x_mc_user: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> User:
    user = session_user(request, db)
    if user:
        return user

    if settings.DEV_AUTH and x_mc_user:
        user = db.get(User, int(x_mc_user))
        if not user or not user.active:
            raise HTTPException(401, "unknown user")
        return user

    if authorization and authorization.lower().startswith("bearer ") and settings.COGNITO_USER_POOL_ID:
        claims = _cognito_claims(authorization.split(" ", 1)[1])
        user = db.scalar(select(User).where(User.cognito_sub == claims["sub"]))
        if not user or not user.active:
            raise HTTPException(401, "no active account for this identity")
        return user

    raise HTTPException(401, "sign in to continue")


def start_session(db: Session, user: User, request: Request) -> tuple[str, datetime]:
    """A new session for a browser. Returns the cookie value and its expiry."""
    token, hashed = new_session_token()
    now = datetime.now(timezone.utc)
    life = (timedelta(days=settings.PARTICIPANT_SESSION_DAYS)
            if user.role == Role.participant else timedelta(hours=settings.STAFF_SESSION_HOURS))
    db.add(SessionRow(user_id=user.id, token_hash=hashed, created_at=now, last_seen_at=now,
                      expires_at=now + life, ip=client_ip(request),
                      user_agent=(request.headers.get("user-agent") or "")[:300]))
    user.last_login_at = now
    user.failed_logins = 0
    user.locked_until = None
    db.commit()
    return token, now + life


def end_session(db: Session, request: Request) -> None:
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        return
    row = db.scalar(select(SessionRow).where(SessionRow.token_hash == token_hash(token)))
    if row and not row.revoked_at:
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()


def end_all_sessions(db: Session, user: User) -> int:
    """Sign a person out everywhere: after a password change, or on request."""
    now = datetime.now(timezone.utc)
    rows = db.scalars(select(SessionRow).where(SessionRow.user_id == user.id,
                                               SessionRow.revoked_at.is_(None))).all()
    for row in rows:
        row.revoked_at = now
    db.commit()
    return len(rows)


def set_session_cookie(response, token: str, expires: datetime, secure: bool) -> None:
    response.set_cookie(
        settings.COOKIE_NAME, token, httponly=True, secure=secure, samesite="lax",
        path="/", max_age=int((expires - datetime.now(timezone.utc)).total_seconds()))


def clear_session_cookie(response, secure: bool) -> None:
    response.delete_cookie(settings.COOKIE_NAME, path="/", httponly=True,
                           secure=secure, samesite="lax")


def require(*roles: Role) -> Callable:
    """Route dependency: `user = Depends(require(Role.officer, Role.admin))`."""
    def dep(user: User = Depends(current_user)) -> User:
        if roles and user.role not in roles and user.role != Role.superuser:
            raise HTTPException(403, f"needs role {' or '.join(r.value for r in roles)}")
        return user
    return dep


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


def audit(db: Session, actor: User | None, action: str, *, target_type: str | None = None,
          target_id: int | None = None, detail: str | None = None,
          request: Request | None = None) -> None:
    """
    Write an audit row. Called on every staff view of a person's record, every
    review decision, and every export. Cheap; do not skip it to save a query.
    """
    db.add(AuditLog(
        county_id=actor.county_id if actor else None,
        actor_id=actor.id if actor else None,
        actor_name=actor.name if actor else None,
        action=action, target_type=target_type, target_id=target_id, detail=detail,
        ip=client_ip(request) if request else None,
    ))
    db.commit()


def scope_to_county(query, model, user: User):
    """
    Tenancy filter. A superuser sees everything; everyone else sees their own
    county only. Postgres RLS enforces the same rule underneath (migration 002)
    so a forgotten call here is not a breach on its own.
    """
    if user.role == Role.superuser:
        return query
    return query.where(model.county_id == user.county_id)
