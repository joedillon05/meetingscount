"""Confirmation by text: saved people, asks and answers, the text log, opt-outs.

A participant asks a sponsor or chair to confirm a check-in. That person gets
one text with a link, and their answer is kept on the check-in.

Revision ID: 006_text_confirmations
Revises: 005_sign_in
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "006_text_confirmations"
down_revision = "005_sign_in"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "confirmers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("county_id", sa.Integer(), sa.ForeignKey("counties.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("relationship", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("removed_at", UTC, nullable=True),
    )
    op.create_index("ix_confirmers_county_id", "confirmers", ["county_id"])
    op.create_index("ix_confirmers_user_id", "confirmers", ["user_id"])

    op.create_table(
        "confirmations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("county_id", sa.Integer(), sa.ForeignKey("counties.id"), nullable=True),
        sa.Column("checkin_id", sa.Integer(), sa.ForeignKey("checkins.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("confirmer_id", sa.Integer(), sa.ForeignKey("confirmers.id"), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("relationship", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("asked_at", UTC, nullable=False),
        sa.Column("expires_at", UTC, nullable=False),
        sa.Column("status", sa.Enum("asked", "confirmed", "declined", name="confirmationstatus"),
                  nullable=False, server_default="asked"),
        sa.Column("answered_at", UTC, nullable=True),
        sa.Column("answered_via", sa.String(length=16), nullable=True),
        sa.Column("answer_note", sa.String(length=400), nullable=True),
        sa.Column("answer_ip", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_confirmations_county_id", "confirmations", ["county_id"])
    op.create_index("ix_confirmations_checkin_id", "confirmations", ["checkin_id"])
    op.create_index("ix_confirmations_user_id", "confirmations", ["user_id"])
    op.create_index("ix_confirmations_token_hash", "confirmations", ["token_hash"], unique=True)

    op.create_table(
        "sms_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("county_id", sa.Integer(), sa.ForeignKey("counties.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("to_phone", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("body", sa.String(length=600), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_sid", sa.String(length=64), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
    )
    op.create_index("ix_sms_log_county_id", "sms_log", ["county_id"])
    op.create_index("ix_sms_log_user_id", "sms_log", ["user_id"])
    op.create_index("ix_sms_log_to_phone", "sms_log", ["to_phone"])
    op.create_index("ix_sms_log_provider_sid", "sms_log", ["provider_sid"])

    op.create_table(
        "sms_opt_outs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(length=32), nullable=False, unique=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="reply"),
        sa.Column("created_at", UTC, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sms_opt_outs")
    for ix in ("ix_sms_log_provider_sid", "ix_sms_log_to_phone", "ix_sms_log_user_id",
               "ix_sms_log_county_id"):
        op.drop_index(ix, table_name="sms_log")
    op.drop_table("sms_log")
    for ix in ("ix_confirmations_token_hash", "ix_confirmations_user_id",
               "ix_confirmations_checkin_id", "ix_confirmations_county_id"):
        op.drop_index(ix, table_name="confirmations")
    op.drop_table("confirmations")
    op.drop_index("ix_confirmers_user_id", table_name="confirmers")
    op.drop_index("ix_confirmers_county_id", table_name="confirmers")
    op.drop_table("confirmers")
