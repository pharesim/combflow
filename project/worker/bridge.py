"""Async bridge for running DB operations from synchronous worker threads."""
import asyncio
import logging
import threading

from ..categories import CATEGORY_TREE
from ..db import crud
from ..db.session import WorkerSessionLocal as AsyncSessionLocal, worker_engine as engine
from ..hafsql import shutdown as hafsql_shutdown

logger = logging.getLogger(__name__)


class _DB:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()

    def run(self, coro):
        with self._lock:
            return self._loop.run_until_complete(coro)

    def close(self):
        hafsql_shutdown()
        with self._lock:
            try:
                self._loop.run_until_complete(engine.dispose())
            finally:
                self._loop.close()


def _seed_categories(db: _DB) -> None:
    async def _seed():
        async with AsyncSessionLocal() as session:
            await crud.seed_category_tree(session, CATEGORY_TREE)
    try:
        db.run(_seed())
    except Exception as exc:
        logger.warning("Could not seed category tree: %s", exc)


def _save_post(db, data: dict) -> None:
    async def _do():
        async with AsyncSessionLocal() as session:
            await crud.create_post(session, data)
    db.run(_do())


def _get_cursor(db, key: str) -> int | None:
    async def _do():
        async with AsyncSessionLocal() as session:
            return await crud.get_cursor(session, key)
    return db.run(_do())


def _set_cursor(db, key: str, block_num: int) -> None:
    async def _do():
        async with AsyncSessionLocal() as session:
            await crud.set_cursor(session, key, block_num)
    db.run(_do())


def _get_distinct_authors(db) -> list[str]:
    async def _do():
        async with AsyncSessionLocal() as session:
            return await crud.get_distinct_authors(session)
    return db.run(_do())


def _delete_posts_by_author(db, author: str) -> int:
    async def _do():
        async with AsyncSessionLocal() as session:
            return await crud.delete_posts_by_author(session, author)
    return db.run(_do())


def _existing_author_permlinks(db, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    async def _do():
        async with AsyncSessionLocal() as session:
            return await crud.existing_author_permlinks(session, pairs)
    return db.run(_do())
