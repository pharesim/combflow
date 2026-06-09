"""add partial index for the author recent-posts SEO query

``crud.get_author_recent_posts`` powers the ``/@author`` SEO page:

    SELECT permlink, created FROM posts
    WHERE author = :author AND category_ids != '{}'
    ORDER BY created DESC LIMIT 20

No existing index serves *both* the ``author`` equality and the ``created DESC``
ordering for this predicate: ``ix_posts_browse_author`` is partial on
``is_nsfw = false`` (which this query does not filter on, so the planner can't
prove it covers the rows), and ``ix_posts_author`` is ``(author)`` only. The
planner therefore scans the global ``ix_posts_created_desc`` backwards and
filters by author — for a prolific but not-recently-active author that walks
millions of rows before the LIMIT (prod EXPLAIN, 8M posts, 2026-06-01:
rongibsonchannel 6.5 s / 1.47M rows removed by filter; ackza 4.0 s). This
composite partial index lets PG seek ``author = X`` and read in ``created DESC``
order — no global scan, no sort, stop at LIMIT 20. It also serves
``get_author_summary``'s ``author = X AND category_ids <> '{}'`` aggregations.
See proposal 107.

NOT built with CONCURRENTLY — matching the rest of this migration set and the
explicit decision in proposal 079: ``CREATE INDEX CONCURRENTLY`` cannot run
inside the transaction Alembic wraps each migration in, and under the asyncpg
driver it raises ``ActiveSQLTransactionError`` which breaks the conftest
``downgrade base`` -> ``upgrade head`` cycle for the whole test suite. As with
``ix_posts_browse_author`` in 005, ``posts`` is written to only by the worker
(which has retry/backoff), so the brief SHARE lock during a one-time deploy-
window build is acceptable; reads (the API) are unaffected. An operator who
wants to avoid even that stall can build ``ix_posts_author_recent``
CONCURRENTLY out-of-band before deploy — the ``IF NOT EXISTS`` below then
no-ops.

``id DESC`` is intentionally omitted (unlike the ``ix_posts_browse_*`` family):
this query orders by ``created`` only with a simple ``LIMIT``, not keyset
pagination on ``(created, id)``, so a third index column would only enlarge the
index without serving the sort.

Revision ID: 008
Revises: 007
Create Date: 2026-06-09
"""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_author_recent "
        "ON posts (author, created DESC) "
        "WHERE category_ids <> '{}'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_posts_author_recent")
