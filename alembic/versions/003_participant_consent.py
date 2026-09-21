"""Participant consent, and an index for "has this person checked in here already".

Revision ID: 003_consent
Revises: 002_rls
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "003_consent"
down_revision = "002_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as t:
        t.add_column(sa.Column("consented_at", app.models.UTCDateTime(timezone=True), nullable=True))
        t.add_column(sa.Column("consent_version", sa.String(length=16), nullable=True))
    op.create_index("ix_checkins_user_meeting", "checkins", ["user_id", "meeting_id"])


def downgrade() -> None:
    op.drop_index("ix_checkins_user_meeting", table_name="checkins")
    with op.batch_alter_table("users") as t:
        t.drop_column("consent_version")
        t.drop_column("consented_at")
