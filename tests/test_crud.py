"""Tests for CRUD utility functions — retry decorator, category tree, seed idempotency."""
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from tests.conftest import _TestSession
from project.categories import CATEGORY_TREE


# ── retry_transient decorator ────────────────────────────────────────────────

class TestRetryTransient:
    @pytest.fixture(autouse=True)
    def _import(self):
        from project.db.crud import retry_transient
        self.retry = retry_transient

    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        call_count = 0

        @self.retry
        async def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await fn()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        call_count = 0

        @self.retry
        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                exc = OperationalError("", {}, Exception())
                exc.connection_invalidated = True
                raise exc
            return "recovered"

        with patch("project.db.crud.asyncio.sleep", new_callable=AsyncMock):
            result = await fn()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_raises_immediately(self):
        call_count = 0

        @self.retry
        async def fn():
            nonlocal call_count
            call_count += 1
            exc = OperationalError("", {}, Exception())
            exc.connection_invalidated = False
            orig = MagicMock()
            orig.pgcode = "42P01"  # undefined_table — not retryable
            exc.orig = orig
            raise exc

        with pytest.raises(OperationalError):
            await fn()
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises(self):
        call_count = 0

        @self.retry
        async def fn():
            nonlocal call_count
            call_count += 1
            exc = OperationalError("", {}, Exception())
            exc.connection_invalidated = True
            raise exc

        with patch("project.db.crud.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(OperationalError):
                await fn()
        assert call_count == 3  # _RETRY_MAX = 3

    @pytest.mark.asyncio
    async def test_backoff_timing(self):
        """Verify sleep durations double: 0.5, 1.0, 2.0."""
        sleep_calls = []

        async def mock_sleep(duration):
            sleep_calls.append(duration)

        @self.retry
        async def fn():
            exc = OperationalError("", {}, Exception())
            exc.connection_invalidated = True
            raise exc

        with patch("project.db.crud.asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(OperationalError):
                await fn()
        assert sleep_calls == [0.5, 1.0, 2.0]

    @pytest.mark.asyncio
    async def test_retries_on_deadlock_pgcode(self):
        """pgcode 40P01 (deadlock) should trigger retry."""
        call_count = 0

        @self.retry
        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                exc = OperationalError("", {}, Exception())
                exc.connection_invalidated = False
                orig = MagicMock()
                orig.pgcode = "40P01"
                exc.orig = orig
                raise exc
            return "ok"

        with patch("project.db.crud.asyncio.sleep", new_callable=AsyncMock):
            result = await fn()
        assert result == "ok"
        assert call_count == 2


# ── seed_category_tree ──────────────────────────────────────────────────────

async def test_seed_category_tree_creates_correct_counts(seeded_db):
    from sqlalchemy import text
    async with _TestSession() as session:
        parents = await session.execute(
            text("SELECT COUNT(*) FROM categories WHERE parent_id IS NULL")
        )
        parent_count = parents.scalar()
        children = await session.execute(
            text("SELECT COUNT(*) FROM categories WHERE parent_id IS NOT NULL")
        )
        child_count = children.scalar()
    assert parent_count == len(CATEGORY_TREE)
    assert child_count == sum(len(v) for v in CATEGORY_TREE.values())


async def test_seed_category_tree_idempotent(seeded_db):
    """Running seed twice doesn't duplicate categories."""
    from project.db.crud import seed_category_tree
    from sqlalchemy import text

    async with _TestSession() as session:
        await seed_category_tree(session, CATEGORY_TREE)

    async with _TestSession() as session:
        rows = await session.execute(text("SELECT COUNT(*) FROM categories"))
        count = rows.scalar()
    expected = len(CATEGORY_TREE) + sum(len(v) for v in CATEGORY_TREE.values())
    assert count == expected


# ── get_category_tree ────────────────────────────────────────────────────────

async def test_get_category_tree_structure(seeded_db):
    from project.db.crud import get_category_tree

    async with _TestSession() as session:
        tree = await get_category_tree(session)
    assert len(tree) == len(CATEGORY_TREE)
    for node in tree:
        assert "name" in node
        assert "children" in node
        assert isinstance(node["children"], list)


async def test_get_category_tree_empty_db(setup_db):
    from project.db.crud import get_category_tree

    async with _TestSession() as session:
        tree = await get_category_tree(session)
    assert tree == []
