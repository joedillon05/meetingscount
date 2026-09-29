"""
Password hashing, session tokens, one-time codes, authenticator codes.

Standard library only: scrypt for passwords (built into hashlib), HMAC-SHA1
for RFC 6238 authenticator codes, and `secrets` for anything random. Nothing
here invents cryptography; it wires up the pieces Python already ships.

Every comparison against a stored secret goes through compare_digest, so a
timing measurement cannot walk a token character by character.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict

from .config import settings

# scrypt cost. 16 MB of memory per hash, about 60 ms on a small server, which
# is slow for an attacker with a stolen database and unnoticeable at sign-in.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD = 12


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """False for an unset password, and the same work either way."""
    if not stored:
        # Hash anyway, so "no account" and "wrong password" take the same time.
        hashlib.scrypt(password.encode(), salt=b"x" * 16, n=SCRYPT_N, r=SCRYPT_R,
                       p=SCRYPT_P, dklen=32)
        return False
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r),
                            p=int(p), dklen=len(_unb64(want)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_b64(dk), want)


def password_problem(password: str, *, email: str | None = None) -> str | None:
    """Why this password will not do, in words the person can act on."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters. Longer beats complicated."
    if email and password.strip().lower() == email.strip().lower():
        return "That is your email address. Pick something else."
    if password.lower() in COMMON_PASSWORDS:
        return "That password turns up in breach lists. Pick another."
    return None


# The handful that show up over and over. A real deployment checks against a
# breach list (k-anonymity API or a local dump); this is the cheap version.
COMMON_PASSWORDS = {
    "password1234", "passw0rd1234", "123456789012", "qwertyuiop12",
    "meetingscount", "letmein12345", "welcome12345", "iloveyou1234",
}


# --------------------------------------------------------------------------- #
# Tokens and one-time codes
# --------------------------------------------------------------------------- #

def new_session_token() -> tuple[str, str]:
    """(token for the cookie, hash to store). The raw token is never saved."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_login_code() -> str:
    """Six digits. Short enough to read down a phone line, and rate limited."""
    return f"{secrets.randbelow(1_000_000):06d}"


def code_hash(user_id: int, code: str) -> str:
    """
    Keyed, so a stolen database alone does not let someone grind six digits
    offline. Ties the code to one account as well.
    """
    return hmac.new(settings.secret_key_bytes, f"{user_id}:{code}".encode(),
                    hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Authenticator app codes (RFC 6238)
# --------------------------------------------------------------------------- #

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((at if at is not None else time.time()) // step)
    mac = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def totp_verify(secret: str | None, code: str, at: float | None = None, window: int = 1) -> bool:
    """One step either side, for clocks that drift and for slow typing."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    now = at if at is not None else time.time()
    return any(hmac.compare_digest(totp_code(secret, now + offset * 30), code)
               for offset in range(-window, window + 1))


def totp_uri(secret: str, account: str, issuer: str = "MeetingsCount") -> str:
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}"
            f"?secret={secret}&issuer={quote(issuer)}&digits=6&period=30")


def grouped(secret: str, size: int = 4) -> str:
    """The secret in blocks, for typing into an authenticator app by hand."""
    return " ".join(secret[i:i + size] for i in range(0, len(secret), size))


# --------------------------------------------------------------------------- #
# Phone numbers
# --------------------------------------------------------------------------- #

def normalize_phone(raw: str | None) -> str | None:
    """
    To +1XXXXXXXXXX for US numbers, so "(734) 555-0101" and "734-555-0101"
    are the same person at sign-in. Anything else keeps its digits and a
    leading +, which is enough for a pilot in one county.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
# In memory, so it holds for one process. That is the whole pilot today, and
# it is the difference between someone grinding six digits in an afternoon and
# not. A second machine means moving this to the database or to Redis.

_HITS: dict[str, list[float]] = defaultdict(list)


def too_many(key: str, limit: int, per_seconds: int) -> bool:
    """True when this key has already had `limit` tries in the window."""
    now = time.time()
    hits = [t for t in _HITS[key] if now - t < per_seconds]
    _HITS[key] = hits
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


def clear_limit(key: str) -> None:
    _HITS.pop(key, None)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
