"""Reviews become a history: more than one per check-in, latest wins.

Asking a participant for more information, then deciding after they reply,
or correcting a mistaken decision, each adds a row. Nothing is overwritten.

Revision ID: 004_review_history
Revises: 003_consent
"""
from alembic import op

revision = "004_review_history"
down_revision = "003_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reviews") as t:
        t.drop_index("ix_reviews_checkin_id")
        t.create_index("ix_reviews_checkin_id", ["checkin_id"], unique=False)


def downgrade() -> None:
    # Only safe while every check-in still has at most one review.
    with op.batch_alter_table("reviews") as t:
        t.drop_index("ix_reviews_checkin_id")
        t.create_index("ix_reviews_checkin_id", ["checkin_id"], unique=True)
