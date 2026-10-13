"""Settings, all from environment variables so nothing secret lives in the repo."""
from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0") not in ("0", "false", "False", "")


def _db_url_from_parts() -> str | None:
    """Assemble a Postgres URL from the fields of an RDS-managed secret."""
    host = os.environ.get("MC_DB_HOST")
    if not host:
        return None
    from urllib.parse import quote_plus
    user = quote_plus(os.environ.get("MC_DB_USER", "postgres"))
    pw = quote_plus(os.environ.get("MC_DB_PASSWORD", ""))
    port = os.environ.get("MC_DB_PORT", "5432")
    name = os.environ.get("MC_DB_NAME", "meetingscount")
    return f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{name}"


_EPHEMERAL_KEY: bytes | None = None


class Settings:
    # sqlite for local dev, postgresql+psycopg2://... in Docker and on AWS.
    # On AWS the parts arrive as separate secrets straight from the RDS secret,
    # so there is no hand-written connection string anywhere to leak.
    DATABASE_URL: str = os.environ.get("MC_DATABASE_URL") or _db_url_from_parts() \
        or "sqlite:///./meetingscount.db"

    # Demo mode lets the simulator send a fake timestamp and lets anyone reseed.
    # MUST be off in production.
    DEMO: bool = _bool("MC_DEMO", True)

    # Dev auth lets you switch roles with a header instead of logging in.
    # MUST be off in production. When off, Cognito JWT verification is required.
    DEV_AUTH: bool = _bool("MC_DEV_AUTH", True)

    # Cognito (used when DEV_AUTH is off)
    COGNITO_REGION: str = os.environ.get("MC_COGNITO_REGION", "us-east-2")
    COGNITO_USER_POOL_ID: str = os.environ.get("MC_COGNITO_USER_POOL_ID", "")
    COGNITO_CLIENT_ID: str = os.environ.get("MC_COGNITO_CLIENT_ID", "")

    # How long raw check-in coordinates are kept before they are reduced to
    # distance + verdict. Principle 2 in the big-picture doc.
    PIN_RETENTION_DAYS: int = int(os.environ.get("MC_PIN_RETENTION_DAYS", "30"))

    # The wording of the participant consent screen. Bump it when the text
    # changes in a way people should agree to again.
    CONSENT_VERSION: str = "2026-10"

    # One shared password in front of a preview deployment, so a demo on a
    # public URL is not a public demo. This is a door lock, not a sign-in:
    # everyone who gets through is still whoever the role switcher says they
    # are. Never a substitute for real accounts, and never in front of real
    # participant data.
    ACCESS_PASSWORD: str = os.environ.get("MC_ACCESS_PASSWORD", "")

    # Keys one-time codes are signed with. Set MC_SECRET_KEY in any
    # deployment: without it a restart invalidates codes people are holding.
    SECRET_KEY: str = os.environ.get("MC_SECRET_KEY", "")

    # How long a signed-in session lasts. Staff sessions are short and time
    # out while idle, which is what a county security review expects. A
    # participant's phone stays signed in, because making someone re-enter a
    # code in a church basement with no signal is how check-ins get missed.
    STAFF_SESSION_HOURS: int = int(os.environ.get("MC_STAFF_SESSION_HOURS", "12"))
    STAFF_IDLE_MINUTES: int = int(os.environ.get("MC_STAFF_IDLE_MINUTES", "30"))
    PARTICIPANT_SESSION_DAYS: int = int(os.environ.get("MC_PARTICIPANT_SESSION_DAYS", "30"))
    LOGIN_CODE_MINUTES: int = int(os.environ.get("MC_LOGIN_CODE_MINUTES", "60"))

    COOKIE_NAME: str = "mc_session"

    # Text messages go through Twilio. Unset means nothing is sent: sign-in
    # codes are read out by an officer, and in demo mode a confirmation link
    # shows on screen instead of going to anyone's phone. MC_TWILIO_FROM is
    # the number to send from, or a Messaging Service SID (MG...).
    TWILIO_ACCOUNT_SID: str = os.environ.get("MC_TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.environ.get("MC_TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM: str = os.environ.get("MC_TWILIO_FROM", "")

    # Demo mode lists its accounts on the sign-in screen, so anyone who opens
    # it can sign in as a participant and type in a number to be texted. With
    # Twilio set up in demo mode, texts go only to the numbers listed here,
    # comma separated: yours, and whoever is in the room. Every other number
    # gets the demo treatment, its code or link on screen. Ignored outside
    # demo mode.
    SMS_ALLOW: str = os.environ.get("MC_SMS_ALLOW", "")

    # Where links in texts point, with no trailing slash. A confirmation link
    # has to open on somebody else's phone, so it needs the public address.
    # Empty means links are built from the request that caused them, which
    # is right for local development and wrong behind a proxy.
    PUBLIC_URL: str = os.environ.get("MC_PUBLIC_URL", "").rstrip("/")

    # How long a confirmation link stays usable.
    CONFIRM_LINK_DAYS: int = int(os.environ.get("MC_CONFIRM_LINK_DAYS", "7"))

    # Self-serve profiles, paid through Stripe. Someone whose county is not
    # on MeetingsCount creates their own profile at /join, proves the phone
    # is theirs with a code, and pays for a subscription on Stripe's hosted
    # checkout page. No card number ever reaches this app. MC_STRIPE_PRICE_ID
    # is the recurring price to sell (price_...), made in the Stripe
    # dashboard, so the amount lives there and not in code.
    SELF_SIGNUP: bool = _bool("MC_SELF_SIGNUP", True)
    # MC_PAYMENTS=0 switches Stripe off without unsetting the secrets, so a
    # site can drop back to free demo profiles and come back later with one
    # line in fly.toml. With payments off nobody is ever asked to pay and no
    # profile is blocked for not having paid.
    PAYMENTS: bool = _bool("MC_PAYMENTS", True)
    STRIPE_SECRET_KEY: str = os.environ.get("MC_STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.environ.get("MC_STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICE_ID: str = os.environ.get("MC_STRIPE_PRICE_ID", "")
    STRIPE_TRIAL_DAYS: int = int(os.environ.get("MC_STRIPE_TRIAL_DAYS", "0"))

    @property
    def stripe_configured(self) -> bool:
        return self.PAYMENTS and bool(self.STRIPE_SECRET_KEY and self.STRIPE_PRICE_ID)

    @property
    def sms_configured(self) -> bool:
        return bool(self.TWILIO_ACCOUNT_SID and self.TWILIO_AUTH_TOKEN and self.TWILIO_FROM)

    @property
    def secret_key_bytes(self) -> bytes:
        if self.SECRET_KEY:
            return self.SECRET_KEY.encode()
        # Dev and tests: a key for this process only.
        global _EPHEMERAL_KEY
        if _EPHEMERAL_KEY is None:
            import secrets as _secrets
            _EPHEMERAL_KEY = _secrets.token_bytes(32)
        return _EPHEMERAL_KEY

    @property
    def is_postgres(self) -> bool:
        return self.DATABASE_URL.startswith("postgresql")


settings = Settings()
