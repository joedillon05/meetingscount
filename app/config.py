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
    CONSENT_VERSION: str = "2026-09"

    @property
    def is_postgres(self) -> bool:
        return self.DATABASE_URL.startswith("postgresql")


settings = Settings()
