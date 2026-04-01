"""denormalize category_ids and language_codes onto posts, drop junction tables

Adds ``category_ids int[]`` and ``language_codes text[]`` columns to posts,
backfills from the junction tables in PL/pgSQL batches of 50k rows (reduces
per-statement memory pressure on 7M+ row table), adds GIN indexes, then
drops ``post_category`` and ``post_language``.

Resumable: IF NOT EXISTS / IF EXISTS guards allow re-run after partial
failure (e.g. container killed mid-migration — alembic_version not yet
updated, so next startup retries).

Requires start_period >= 3600s in docker-compose health check.

Revision ID: 006
Revises: 005
Create Date: 2026-04-01
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

BATCH = 50_000


def upgrade() -> None:
    # 1. Add array columns (instant — PG stores DEFAULT in pg_attribute).
    op.execute(
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS category_ids int[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS language_codes text[] NOT NULL DEFAULT '{}'"
    )

    # 2. Backfill from junction tables in batched PL/pgSQL loops.
    #    Each UPDATE touches at most BATCH rows, reducing memory/WAL pressure.
    #    RAISE NOTICE provides progress in Docker logs.
    op.execute(f"""
DO $$
DECLARE
    _lo   bigint;
    _hi   bigint;
    _cur  bigint;
    _rows bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'post_category') THEN
        SELECT MIN(post_id), MAX(post_id) INTO _lo, _hi FROM post_category;
        IF _lo IS NOT NULL THEN
            _cur := _lo;
            WHILE _cur <= _hi LOOP
                UPDATE posts SET category_ids = sub.vals
                FROM (
                    SELECT post_id, array_agg(category_id) AS vals
                    FROM post_category
                    WHERE post_id BETWEEN _cur AND _cur + {BATCH} - 1
                    GROUP BY post_id
                ) sub
                WHERE posts.id = sub.post_id;
                GET DIAGNOSTICS _rows = ROW_COUNT;
                RAISE NOTICE 'category_ids batch % .. %: % rows', _cur, _cur + {BATCH} - 1, _rows;
                _cur := _cur + {BATCH};
            END LOOP;
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'post_language') THEN
        SELECT MIN(post_id), MAX(post_id) INTO _lo, _hi FROM post_language;
        IF _lo IS NOT NULL THEN
            _cur := _lo;
            WHILE _cur <= _hi LOOP
                UPDATE posts SET language_codes = sub.vals
                FROM (
                    SELECT post_id, array_agg(language) AS vals
                    FROM post_language
                    WHERE post_id BETWEEN _cur AND _cur + {BATCH} - 1
                    GROUP BY post_id
                ) sub
                WHERE posts.id = sub.post_id;
                GET DIAGNOSTICS _rows = ROW_COUNT;
                RAISE NOTICE 'language_codes batch % .. %: % rows', _cur, _cur + {BATCH} - 1, _rows;
                _cur := _cur + {BATCH};
            END LOOP;
        END IF;
    END IF;
END $$;
""")

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
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_post_category_post_id_category_id "
        "ON post_category (post_id, category_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_post_category_category_id "
        "ON post_category (category_id)"
    )
    op.execute("DROP INDEX IF EXISTS ix_posts_language_codes")
    op.execute("DROP INDEX IF EXISTS ix_posts_category_ids")
    op.execute("ALTER TABLE posts DROP COLUMN language_codes")
    op.execute("ALTER TABLE posts DROP COLUMN category_ids")
