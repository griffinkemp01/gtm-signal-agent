"""add Clay outbound-push tracking columns to companies

Decouples the outbound last-mile from Slack alerting: an account can be pushed
to Clay (lower bar) on its own cooldown without firing a Slack alert.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("last_clay_pushed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("last_clay_pushed_score", sa.Float, nullable=True))
    # Index the push timestamp — filtered on every Clay-push decision.
    op.create_index("ix_companies_last_clay_pushed_at", "companies", ["last_clay_pushed_at"])


def downgrade() -> None:
    op.drop_index("ix_companies_last_clay_pushed_at", table_name="companies")
    op.drop_column("companies", "last_clay_pushed_score")
    op.drop_column("companies", "last_clay_pushed_at")
