"""A probation end date at sign-up, sharing a log by email, support groups.

Revision ID: 010_end_dates_and_email
Revises: 009_log_shares
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "010_end_dates_and_email"
down_revision = "009_log_shares"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    # The join form asks when probation ends, so the requirement can carry it.
    with op.batch_alter_table("signups") as t:
        t.add_column(sa.Column("ends_on", UTC, nullable=True))

    # A log can go to an email address as well as a phone, so the phone may
    # now be empty. One of the two is always set; the app checks.
    with op.batch_alter_table("log_shares") as t:
        t.add_column(sa.Column("email", sa.String(length=200), nullable=True))
        t.alter_column("phone", existing_type=sa.String(length=32), nullable=True)

    op.create_table(
        "email_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("county_id", sa.Integer(),
                  sa.ForeignKey("counties.id", name="fk_email_log_county_id"), nullable=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_email_log_user_id"), nullable=True),
        sa.Column("to_email", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_id", sa.String(length=120), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
    )
    op.create_index("ix_email_log_county_id", "email_log", ["county_id"])
    op.create_index("ix_email_log_user_id", "email_log", ["user_id"])
    op.create_index("ix_email_log_to_email", "email_log", ["to_email"])

    # A fifth kind of meeting. SQLite stores the enum as text and needs
    # nothing; Postgres has a real enum type to extend.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE meetingcategory ADD VALUE IF NOT EXISTS 'support_group'")


def downgrade() -> None:
    for ix in ("ix_email_log_to_email", "ix_email_log_user_id", "ix_email_log_county_id"):
        op.drop_index(ix, table_name="email_log")
    op.drop_table("email_log")
    with op.batch_alter_table("log_shares") as t:
        t.drop_column("email")
        t.alter_column("phone", existing_type=sa.String(length=32), nullable=False)
    with op.batch_alter_table("signups") as t:
        t.drop_column("ends_on")
