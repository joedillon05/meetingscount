"""The meeting directory: recurring meetings, and where a participant looks for them.

Revision ID: 007_meeting_directory
Revises: 006_text_confirmations
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "007_meeting_directory"
down_revision = "006_text_confirmations"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "meeting_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("county_id", sa.Integer(), sa.ForeignKey("counties.id"), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.Enum("public", "private", "community_service", "therapy",
                                      name="meetingcategory"), nullable=False,
                  server_default="public"),
        sa.Column("source_name", sa.String(length=60), nullable=False),
        sa.Column("source_url", sa.String(length=300), nullable=True),
        sa.Column("external_key", sa.String(length=120), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("timezone", sa.String(length=64), nullable=False,
                  server_default="America/Detroit"),
        sa.Column("street", sa.String(length=120), nullable=True),
        sa.Column("city", sa.String(length=80), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=False, server_default="MI"),
        sa.Column("zip", sa.String(length=10), nullable=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("radius_ft", sa.Float(), nullable=False, server_default="500"),
        sa.Column("time_window_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("imported_at", UTC, nullable=False),
        sa.Column("last_seen_at", UTC, nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_meeting_series_county_id", "meeting_series", ["county_id"])
    op.create_index("ix_meeting_series_source_name", "meeting_series", ["source_name"])
    op.create_index("ix_meeting_series_external_key", "meeting_series", ["external_key"], unique=True)
    op.create_index("ix_meeting_series_city", "meeting_series", ["city"])
    op.create_index("ix_meeting_series_latlng", "meeting_series", ["lat", "lng"])

    with op.batch_alter_table("meetings") as t:
        t.add_column(sa.Column("series_id", sa.Integer(), sa.ForeignKey("meeting_series.id"),
                               nullable=True))
    op.create_index("ix_meetings_series_id", "meetings", ["series_id"])
    op.create_index("ux_meetings_series_start", "meetings", ["series_id", "starts_at"], unique=True)

    with op.batch_alter_table("users") as t:
        t.add_column(sa.Column("area_label", sa.String(length=80), nullable=True))
        t.add_column(sa.Column("area_lat", sa.Float(), nullable=True))
        t.add_column(sa.Column("area_lng", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as t:
        for col in ("area_lng", "area_lat", "area_label"):
            t.drop_column(col)
    op.drop_index("ux_meetings_series_start", table_name="meetings")
    op.drop_index("ix_meetings_series_id", table_name="meetings")
    with op.batch_alter_table("meetings") as t:
        t.drop_column("series_id")
    for ix in ("ix_meeting_series_latlng", "ix_meeting_series_city", "ix_meeting_series_external_key",
               "ix_meeting_series_source_name", "ix_meeting_series_county_id"):
        op.drop_index(ix, table_name="meeting_series")
    op.drop_table("meeting_series")
