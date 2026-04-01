"""denormalize category_ids and language_codes onto posts, drop junction tables

Adds ``category_ids int[]`` and ``language_codes text[]`` columns to posts,
backfills from the junction tables, adds GIN indexes, then drops
``post_category`` and ``post_language``.  All browse queries become
single-table scans — estimated 5-10× speedup on filtered COUNTs.

Revision ID: 006
Revises: 005
Create Date: 2026-04-01
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add array columns (instant — PG stores DEFAULT in pg_attribute).
    op.execute("ALTER TABLE posts ADD COLUMN category_ids int[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE posts ADD COLUMN language_codes text[] NOT NULL DEFAULT '{}'")

    # 2. Backfill from junction tables.
    op.execute(
        "UPDATE posts SET category_ids = COALESCE(sub.ids, '{}') "
        "FROM ("
        "  SELECT post_id, array_agg(category_id) AS ids "
        "  FROM post_category GROUP BY post_id"
        ") sub "
        "WHERE posts.id = sub.post_id"
    )
    op.execute(
        "UPDATE posts SET language_codes = COALESCE(sub.codes, '{}') "
        "FROM ("
        "  SELECT post_id, array_agg(language) AS codes "
        "  FROM post_language GROUP BY post_id"
        ") sub "
        "WHERE posts.id = sub.post_id"
    )

    # 3. GIN indexes for array overlap (&&) queries.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_category_ids "
        "ON posts USING gin(category_ids)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_language_codes "
        "ON posts USING gin(language_codes)"
    )

    # 4. Drop junction tables (CASCADE removes indexes, FKs, constraints).
    op.execute("DROP TABLE IF EXISTS post_category CASCADE")
    op.execute("DROP TABLE IF EXISTS post_language CASCADE")


def downgrade() -> None:
    # Recreate junction tables.
    op.execute(
        "CREATE TABLE post_category ("
        "  post_id int NOT NULL REFERENCES posts(id) ON DELETE CASCADE, "
        "  category_id int NOT NULL REFERENCES categories(id), "
        "  CONSTRAINT uq_post_category UNIQUE (post_id, category_id)"
        ")"
    )
    op.execute(
        "CREATE TABLE post_language ("
        "  post_id int REFERENCES posts(id), "
        "  language varchar(10) NOT NULL, "
        "  CONSTRAINT uq_post_language UNIQUE (post_id, language)"
        ")"
    )

    # Repopulate from array columns.
    op.execute(
        "INSERT INTO post_category (post_id, category_id) "
        "SELECT id, unnest(category_ids) FROM posts "
        "WHERE category_ids != '{}'"
    )
    op.execute(
        "INSERT INTO post_language (post_id, language) "
        "SELECT id, unnest(language_codes) FROM posts "
        "WHERE language_codes != '{}'"
    )

    # Recreate indexes that existed before.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_post_category_post_id_category_id "
        "ON post_category (post_id, category_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_post_category_category_id "
        "ON post_category (category_id)"
    )

    # Drop array columns and GIN indexes.
    op.execute("DROP INDEX IF EXISTS ix_posts_language_codes")
    op.execute("DROP INDEX IF EXISTS ix_posts_category_ids")
    op.execute("ALTER TABLE posts DROP COLUMN language_codes")
    op.execute("ALTER TABLE posts DROP COLUMN category_ids")
