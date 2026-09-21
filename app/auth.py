"""
Authentication and authorization.

Two modes, chosen by MC_DEV_AUTH:

  dev  (default)  The client sends `X-MC-User: <user id>`. No password. This is
                  what the role switcher in the dashboard uses. Never in prod.
  prod            A Cognito-issued JWT in `Authorization: Bearer <token>`,
                  verified against the pool's JWKS, mapped to a User row by
                  the `sub` claim.

`require(*roles)` is the dependency every protected route uses, and
`scope_to_county()` is what keeps one county out of another's data.
"""
from __future__ import annotations

import time
from typing import Callable

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AuditLog, Role, User

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


def current_user(
    request: Request,
    db: Session = Depends(get_db),
    x_mc_user: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> User:
    if settings.DEV_AUTH:
        if not x_mc_user:
            raise HTTPException(401, "send X-MC-User (dev auth is on)")
        user = db.get(User, int(x_mc_user))
        if not user or not user.active:
            raise HTTPException(401, "unknown user")
        return user

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    claims = _cognito_claims(authorization.split(" ", 1)[1])
    user = db.scalar(select(User).where(User.cognito_sub == claims["sub"]))
    if not user or not user.active:
        raise HTTPException(401, "no active account for this identity")
    return user


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
