"""Verify that Alembic migration actually created tables.

Called between 'alembic upgrade head' and 'uvicorn' in the Docker entrypoint.
If any required table is missing (migration recorded but DDL rolled back),
clears alembic_version so the next restart retries from scratch.
"""
import asyncio
import sys

from sqlalchemy import text

from project.db.session import engine

_REQUIRED_TABLES = [
    "posts",
    "categories",
    "post_category",
    "post_language",
    "category_centroids",
    "stream_cursors",
]


async def check():
    async with engine.connect() as c:
        r = await c.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(:tables)"
            ),
            {"tables": _REQUIRED_TABLES},
        )
        found = {row[0] for row in r.fetchall()}
        missing = set(_REQUIRED_TABLES) - found

        if missing:
            print(
                f"FATAL: migration missing tables: {sorted(missing)} "
                "— clearing alembic_version for retry",
                flush=True,
            )
            await c.execute(text("DELETE FROM alembic_version"))
            await c.commit()
            sys.exit(1)
        print(f"Migration verified: all {len(_REQUIRED_TABLES)} tables exist", flush=True)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(check())
