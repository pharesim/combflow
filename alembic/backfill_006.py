"""Backfill category_ids / language_codes and drop junction tables.

Runs outside Alembic's transaction so each batch commits independently.
Safe to re-run: skips if junction tables are already gone.

Called from Docker entrypoint after ``alembic upgrade head``.
"""

import os
import sys

import psycopg2

BATCH = 50_000


def get_sync_url() -> str:
    """Convert asyncpg DATABASE_URL to psycopg2 format."""
    url = os.environ["DATABASE_URL"]
    return url.replace("+asyncpg", "")


def table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def backfill(conn, col: str, junction: str, agg_expr: str) -> None:
    cur = conn.cursor()
    cur.execute(f"SELECT MIN(post_id), MAX(post_id) FROM {junction}")
    lo, hi = cur.fetchone()
    if lo is None:
        print(f"  {junction}: empty, nothing to backfill", flush=True)
        return

    total = 0
    while lo <= hi:
        batch_hi = lo + BATCH - 1
        cur.execute(
            f"UPDATE posts SET {col} = sub.vals "
            f"FROM ("
            f"  SELECT post_id, {agg_expr} AS vals "
            f"  FROM {junction} "
            f"  WHERE post_id BETWEEN %s AND %s "
            f"  GROUP BY post_id"
            f") sub "
            f"WHERE posts.id = sub.post_id",
            (lo, batch_hi),
        )
        total += cur.rowcount
        conn.commit()
        print(f"  {col}: {total} rows backfilled (batch up to {batch_hi})", flush=True)
        lo = batch_hi + 1
    cur.close()
    print(f"  {col}: done — {total} rows total", flush=True)


def main() -> None:
    conn = psycopg2.connect(get_sync_url())
    conn.autocommit = False  # we commit explicitly per batch

    cur = conn.cursor()

    if not table_exists(cur, "post_category") and not table_exists(cur, "post_language"):
        print("backfill_006: junction tables already dropped, nothing to do", flush=True)
        cur.close()
        conn.close()
        return

    print("backfill_006: starting backfill...", flush=True)

    if table_exists(cur, "post_category"):
        backfill(conn, "category_ids", "post_category", "array_agg(category_id)")

    if table_exists(cur, "post_language"):
        backfill(conn, "language_codes", "post_language", "array_agg(language)")

    # GIN indexes
    print("backfill_006: creating GIN indexes...", flush=True)
    conn.autocommit = True  # CREATE INDEX can't run in a transaction
    cur = conn.cursor()
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_category_ids "
        "ON posts USING gin(category_ids)"
    )
    print("  ix_posts_category_ids created", flush=True)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_posts_language_codes "
        "ON posts USING gin(language_codes)"
    )
    print("  ix_posts_language_codes created", flush=True)

    # Drop junction tables
    cur.execute("DROP TABLE IF EXISTS post_category CASCADE")
    cur.execute("DROP TABLE IF EXISTS post_language CASCADE")
    print("backfill_006: junction tables dropped — done", flush=True)

    cur.close()
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"backfill_006 FATAL: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
