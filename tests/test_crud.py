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
    # Children sharing a name with their parent are not inserted as separate rows
    overlaps = sum(1 for p, kids in CATEGORY_TREE.items() if p in kids)
    expected_parents = len(CATEGORY_TREE)
    expected_children = sum(len(v) for v in CATEGORY_TREE.values()) - overlaps
    async with _TestSession() as session:
        parents = await session.execute(
            text("SELECT COUNT(*) FROM categories WHERE parent_id IS NULL")
        )
        parent_count = parents.scalar()
        children = await session.execute(
            text("SELECT COUNT(*) FROM categories WHERE parent_id IS NOT NULL")
        )
        child_count = children.scalar()
    assert parent_count == expected_parents
    assert child_count == expected_children


async def test_seed_category_tree_idempotent(seeded_db):
    """Running seed twice doesn't duplicate categories."""
    from project.db.crud import seed_category_tree
    from sqlalchemy import text

    async with _TestSession() as session:
        await seed_category_tree(session, CATEGORY_TREE)

    overlaps = sum(1 for p, kids in CATEGORY_TREE.items() if p in kids)
    async with _TestSession() as session:
        rows = await session.execute(text("SELECT COUNT(*) FROM categories"))
        count = rows.scalar()
    expected = len(CATEGORY_TREE) + sum(len(v) for v in CATEGORY_TREE.values()) - overlaps
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


# ── existing_author_permlinks ─────────────────────────────────────────────

async def test_existing_author_permlinks_empty_pairs(seeded_db):
    from project.db.crud import existing_author_permlinks

    async with _TestSession() as session:
        result = await existing_author_permlinks(session, [])
    assert result == set()


async def test_existing_author_permlinks_finds_existing(seeded_db):
    from project.db.crud import existing_author_permlinks

    async with _TestSession() as session:
        result = await existing_author_permlinks(session, [
            ("alice", "test-post-one"),
            ("bob", "test-post-two"),
            ("nobody", "nonexistent"),
        ])
    assert ("alice", "test-post-one") in result
    assert ("bob", "test-post-two") in result
    assert ("nobody", "nonexistent") not in result


async def test_existing_author_permlinks_all_missing(seeded_db):
    from project.db.crud import existing_author_permlinks

    async with _TestSession() as session:
        result = await existing_author_permlinks(session, [
            ("x", "y"), ("a", "b"),
        ])
    assert result == set()


# ── create_post update path ───────────────────────────────────────────────

async def test_create_post_upsert_updates_existing(seeded_db):
    """Re-inserting same author/permlink updates sentiment and categories."""
    from project.db.crud import create_post, get_post_by_permlink

    second_leaf = CATEGORY_TREE[list(CATEGORY_TREE.keys())[1]][0]

    async with _TestSession() as session:
        await create_post(session, {
            "author": "alice",
            "permlink": "test-post-one",
            "sentiment": "negative",
            "sentiment_score": -0.9,
            "categories": [second_leaf],
            "languages": ["fr"],
            "community_id": "hive-123456",
            "primary_language": "fr",
            "is_nsfw": True,
        })

    async with _TestSession() as session:
        post = await get_post_by_permlink(session, "alice", "test-post-one")
    assert post is not None
    assert post["sentiment"] == "negative"
    assert post["sentiment_score"] == -0.9
    assert post["categories"] == [second_leaf]
    assert post["languages"] == ["fr"]
    assert post["community_id"] == "hive-123456"
    assert post["primary_language"] == "fr"
    assert post["is_nsfw"] is True


# ── Centroids roundtrip ──────────────────────────────────────────────────

async def test_save_and_get_centroids(setup_db):
    from project.db.crud import save_centroids, get_centroids

    centroids = {
        "photography": [0.1] * 384,
        "food": [0.2] * 384,
    }
    metadata = {
        "posts_labeled": 100,
        "llm_model": "test-model",
        "embedding_model": "all-MiniLM-L6-v2",
    }

    async with _TestSession() as session:
        await save_centroids(session, centroids, metadata)

    async with _TestSession() as session:
        loaded = await get_centroids(session)

    assert set(loaded.keys()) == {"photography", "food"}
    assert len(loaded["photography"]) == 384
    assert abs(loaded["photography"][0] - 0.1) < 1e-5


async def test_save_centroids_upsert(setup_db):
    """Saving centroids twice overwrites the first set."""
    from project.db.crud import save_centroids, get_centroids

    async with _TestSession() as session:
        await save_centroids(session, {"photography": [0.1] * 384}, {"posts_labeled": 10})
    async with _TestSession() as session:
        await save_centroids(session, {"photography": [0.9] * 384}, {"posts_labeled": 20})

    async with _TestSession() as session:
        loaded = await get_centroids(session)
    assert abs(loaded["photography"][0] - 0.9) < 1e-5


# ── Stream cursors ────────────────────────────────────────────────────────

async def test_cursor_set_and_get(setup_db):
    from project.db.crud import get_cursor, set_cursor

    async with _TestSession() as session:
        result = await get_cursor(session, "test_key")
    assert result is None

    async with _TestSession() as session:
        await set_cursor(session, "test_key", 12345)

    async with _TestSession() as session:
        result = await get_cursor(session, "test_key")
    assert result == 12345


async def test_cursor_upsert(setup_db):
    from project.db.crud import get_cursor, set_cursor

    async with _TestSession() as session:
        await set_cursor(session, "test_key", 100)
    async with _TestSession() as session:
        await set_cursor(session, "test_key", 200)

    async with _TestSession() as session:
        result = await get_cursor(session, "test_key")
    assert result == 200


# ── get_distinct_authors ──────────────────────────────────────────────────

async def test_get_distinct_authors(seeded_db):
    from project.db.crud import get_distinct_authors

    async with _TestSession() as session:
        authors = await get_distinct_authors(session)
    assert set(authors) == {"alice", "bob", "carol"}


async def test_get_distinct_authors_empty(setup_db):
    from project.db.crud import get_distinct_authors

    async with _TestSession() as session:
        authors = await get_distinct_authors(session)
    assert authors == []


# ── delete_posts_by_author ────────────────────────────────────────────────

async def test_delete_posts_by_author(seeded_db):
    from project.db.crud import delete_posts_by_author, get_post_by_permlink

    async with _TestSession() as session:
        count = await delete_posts_by_author(session, "alice")
    assert count == 1

    async with _TestSession() as session:
        post = await get_post_by_permlink(session, "alice", "test-post-one")
    assert post is None


async def test_delete_posts_by_author_nonexistent(setup_db):
    from project.db.crud import delete_posts_by_author

    async with _TestSession() as session:
        count = await delete_posts_by_author(session, "nobody")
    assert count == 0


async def test_delete_posts_by_author_cascades_associations(seeded_db):
    """Deleting an author's posts also removes post_category and post_language rows."""
    from sqlalchemy import text as sql_text
    from project.db.crud import delete_posts_by_author

    # Get alice's post ID before deletion.
    async with _TestSession() as session:
        row = await session.execute(
            sql_text("SELECT id FROM posts WHERE author = 'alice'")
        )
        alice_id = row.scalar()
        assert alice_id is not None

    async with _TestSession() as session:
        await delete_posts_by_author(session, "alice")

    async with _TestSession() as session:
        cats = await session.execute(
            sql_text("SELECT COUNT(*) FROM post_category WHERE post_id = :pid"),
            {"pid": alice_id},
        )
        assert cats.scalar() == 0
        langs = await session.execute(
            sql_text("SELECT COUNT(*) FROM post_language WHERE post_id = :pid"),
            {"pid": alice_id},
        )
        assert langs.scalar() == 0
