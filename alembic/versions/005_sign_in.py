"""Real sign-in: passwords, authenticator secrets, sessions, one-time codes.

Until now the client said who it was and the server believed it, which was
fine for a demo and is not fine for anyone's real record.

Revision ID: 005_sign_in
Revises: 004_review_history
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "005_sign_in"
down_revision = "004_review_history"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("users") as t:
        t.add_column(sa.Column("password_hash", sa.String(length=200), nullable=True))
        t.add_column(sa.Column("password_set_at", UTC, nullable=True))
        t.add_column(sa.Column("must_change_password", sa.Boolean(), nullable=False,
                               server_default=sa.false()))
        t.add_column(sa.Column("totp_secret", sa.String(length=64), nullable=True))
        t.add_column(sa.Column("totp_enabled_at", UTC, nullable=True))
        t.add_column(sa.Column("failed_logins", sa.Integer(), nullable=False,
                               server_default="0"))
        t.add_column(sa.Column("locked_until", UTC, nullable=True))
        t.add_column(sa.Column("last_login_at", UTC, nullable=True))

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("last_seen_at", UTC, nullable=False),
        sa.Column("expires_at", UTC, nullable=False),
        sa.Column("revoked_at", UTC, nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "login_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("expires_at", UTC, nullable=False),
        sa.Column("used_at", UTC, nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("issued_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("delivered_via", sa.String(length=16), nullable=False,
                  server_default="officer"),
    )
    op.create_index("ix_login_codes_user_id", "login_codes", ["user_id"])


def downgrade() -> None:
    op.drop_table("login_codes")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    with op.batch_alter_table("users") as t:
        for col in ("last_login_at", "locked_until", "failed_logins", "totp_enabled_at",
                    "totp_secret", "must_change_password", "password_set_at", "password_hash"):
            t.drop_column(col)
