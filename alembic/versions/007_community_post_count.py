"""add post_count to community_mappings

Denormalizes post counts onto community_mappings so
get_available_communities avoids a GROUP BY over the full posts table.
Backfills from current data using a single GROUP BY (not a correlated
subquery, which would scan 7M+ posts per community).

Revision ID: 007
Revises: 006
Create Date: 2026-04-01
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE community_mappings "
        "ADD COLUMN IF NOT EXISTS post_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "UPDATE community_mappings cm "
        "SET post_count = sub.cnt "
        "FROM ("
        "  SELECT community_id, COUNT(*) AS cnt "
        "  FROM posts "
        "  WHERE community_id IS NOT NULL "
        "  GROUP BY community_id"
        ") sub "
        "WHERE cm.community_id = sub.community_id"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE community_mappings DROP COLUMN post_count")
