"""add partial indexes for browse query performance

With 4M+ posts, filtered browse COUNT queries hit sequential scans on
posts and junction tables.  Partial indexes on ``is_nsfw = false``
(99.9 % of queries) let the planner use index scans instead, cutting
COUNT times from 1-3 s to sub-second.

Revision ID: 005
Revises: 004
Create Date: 2026-04-01
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Main browse index: covers the default sort (created DESC) with
    # keyset pagination (id DESC) for the ~100 % of queries that
    # exclude NSFW.  Replaces the full-table ix_posts_created_desc for
    # filtered queries.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_browse_default "
        "ON posts (created DESC, id DESC) "
        "WHERE is_nsfw = false"
    )

    # Sentiment filter — second most common after category.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_browse_sentiment "
        "ON posts (sentiment, created DESC, id DESC) "
        "WHERE is_nsfw = false"
    )

    # Community filter — already has a single-column index, but the
    # composite lets PG satisfy ORDER BY without a separate sort.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_browse_community "
        "ON posts (community_id, created DESC, id DESC) "
        "WHERE is_nsfw = false"
    )

    # Author filter (Following / profile pages).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_browse_author "
        "ON posts (author, created DESC, id DESC) "
        "WHERE is_nsfw = false"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_posts_browse_author")
    op.execute("DROP INDEX IF EXISTS ix_posts_browse_community")
    op.execute("DROP INDEX IF EXISTS ix_posts_browse_sentiment")
    op.execute("DROP INDEX IF EXISTS ix_posts_browse_default")
