"""add post_count to community_mappings

Denormalizes post counts onto community_mappings so
get_available_communities avoids a GROUP BY over the full posts table.
Backfills from current data.

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
        "ADD COLUMN post_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "UPDATE community_mappings cm "
        "SET post_count = COALESCE(("
        "  SELECT COUNT(*) FROM posts p WHERE p.community_id = cm.community_id"
        "), 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE community_mappings DROP COLUMN post_count")
