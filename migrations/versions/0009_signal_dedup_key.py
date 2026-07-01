"""dedup signals on a stable key instead of the volatile source_url

News source_urls are Google-redirect links that change on every fetch, so
keying uniqueness on source_url re-inserted the same story as a new row each
run and inflated cumulative scores. This adds a stable `dedup_key` (title+date
hash for news, source_url for everything else), collapses the duplicate rows
already in the table, and moves the uq_signal_dedup constraint onto it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# survivor = lowest id per (company_id, signal_type, dedup_key)
_RANKED = """
WITH ranked AS (
    SELECT id, first_value(id) OVER (
               PARTITION BY company_id, signal_type, dedup_key ORDER BY id
           ) AS keep_id
    FROM signals
)
"""


def upgrade() -> None:
    op.add_column("signals", sa.Column("dedup_key", sa.String(length=1024), nullable=True))

    # Backfill: news keys on its stable title+date hash (stored in raw_payload
    # as rss_item_hash); everything else keys on its stable source_url.
    op.execute(
        """
        UPDATE signals SET dedup_key = CASE
            WHEN source = 'news'
                THEN 'news:' || COALESCE(NULLIF(raw_payload->>'rss_item_hash', ''), source_url)
            ELSE source_url
        END
        """
    )

    # Collapse existing duplicates. Repoint any alerts off the rows we're about
    # to delete (FK alerts.triggering_signal_id -> signals.id), then delete.
    op.execute(
        _RANKED
        + """
        UPDATE alerts a SET triggering_signal_id = r.keep_id
        FROM ranked r
        WHERE a.triggering_signal_id = r.id AND r.id <> r.keep_id
        """
    )
    op.execute(
        _RANKED
        + """
        DELETE FROM signals s USING ranked r
        WHERE s.id = r.id AND r.id <> r.keep_id
        """
    )

    # Swap the uniqueness constraint onto the stable key.
    op.drop_constraint("uq_signal_dedup", "signals", type_="unique")
    op.create_unique_constraint(
        "uq_signal_dedup", "signals", ["company_id", "signal_type", "dedup_key"]
    )
    op.alter_column("signals", "dedup_key", nullable=False)


def downgrade() -> None:
    op.drop_constraint("uq_signal_dedup", "signals", type_="unique")
    op.create_unique_constraint(
        "uq_signal_dedup", "signals", ["company_id", "signal_type", "source_url"]
    )
    op.drop_column("signals", "dedup_key")
