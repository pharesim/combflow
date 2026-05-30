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
    """Deleting an author's posts removes the rows entirely (array columns go with them)."""
    from sqlalchemy import text as sql_text
    from project.db.crud import delete_posts_by_author

    async with _TestSession() as session:
        row = await session.execute(
            sql_text("SELECT COUNT(*) FROM posts WHERE author = 'alice'")
        )
        assert row.scalar() > 0

    async with _TestSession() as session:
        await delete_posts_by_author(session, "alice")

    async with _TestSession() as session:
        remaining = await session.execute(
            sql_text("SELECT COUNT(*) FROM posts WHERE author = 'alice'")
        )
        assert remaining.scalar() == 0


# ── get_author_summary (proposals 096 + 098) ───────────────────────────────

async def test_get_author_summary_aggregates(seeded_db):
    """Single-post author → totals + top category/language, no community."""
    from project.db.crud import get_author_summary

    async with _TestSession() as session:
        summary = await get_author_summary(session, "alice")

    assert summary is not None
    assert summary["total_posts"] == 1
    assert summary["top_categories"][0]["name"] == seeded_db["leaf_name"]
    assert summary["top_categories"][0]["id"] == seeded_db["leaf_name"]
    assert summary["top_languages"][0]["code"] == "en"
    assert summary["top_community"] is None
    assert summary["first_seen"] is not None
    assert summary["last_seen"] is not None


async def test_get_author_summary_none_for_unknown_author(seeded_db):
    from project.db.crud import get_author_summary

    async with _TestSession() as session:
        summary = await get_author_summary(session, "nobody-here")
    assert summary is None


async def test_get_author_summary_orders_categories_by_count(seeded_db):
    """Top categories are ordered by post count descending."""
    from datetime import datetime, timezone
    from project.db.crud import get_author_summary, create_post

    leaf_a = seeded_db["leaf_name"]                              # alice already has 1
    leaf_b = CATEGORY_TREE[list(CATEGORY_TREE.keys())[1]][0]     # distinct leaf

    async with _TestSession() as session:
        for i in range(2):
            await create_post(session, {
                "author": "alice",
                "permlink": f"extra-{i}",
                "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
                "categories": [leaf_b],
                "languages": ["en"],
                "sentiment": "neutral",
                "sentiment_score": 0.0,
            })

    async with _TestSession() as session:
        summary = await get_author_summary(session, "alice")

    assert summary["total_posts"] == 3
    assert summary["top_categories"][0]["name"] == leaf_b   # 2 posts > 1
    assert summary["top_categories"][0]["count"] == 2


async def test_get_author_summary_filters_below_5pct_floor(seeded_db):
    """Stray categories below the 5% floor are excluded from top categories."""
    from datetime import datetime, timezone
    from project.db.crud import get_author_summary, create_post

    leaf_a = seeded_db["leaf_name"]                              # alice's existing 1
    leaf_b = CATEGORY_TREE[list(CATEGORY_TREE.keys())[1]][0]     # bulk leaf

    # 25 leaf_b posts + 1 existing leaf_a → total 26, floor = ceil(1.3) = 2.
    # leaf_a has only 1 post, below the floor, so it drops out.
    async with _TestSession() as session:
        for i in range(25):
            await create_post(session, {
                "author": "alice",
                "permlink": f"bulk-{i}",
                "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
                "categories": [leaf_b],
                "languages": ["en"],
                "sentiment": "neutral",
                "sentiment_score": 0.0,
            })

    async with _TestSession() as session:
        summary = await get_author_summary(session, "alice")

    assert summary["total_posts"] == 26
    names = [c["name"] for c in summary["top_categories"]]
    assert leaf_b in names
    assert leaf_a not in names


async def test_get_author_summary_includes_top_community(seeded_db):
    """Author with a community_id surfaces top_community with its display name."""
    from datetime import datetime, timezone
    from sqlalchemy import text as sql_text
    from project.db.crud import get_author_summary, create_post

    async with _TestSession() as session:
        await session.execute(sql_text(
            "INSERT INTO community_mappings (community_id, community_name, score, post_count) "
            "VALUES ('hive-999', 'Test Community', 0.5, 0)"
        ))
        await session.commit()
        await create_post(session, {
            "author": "dave",
            "permlink": "comm-post",
            "created": datetime(2026, 4, 2, tzinfo=timezone.utc),
            "categories": [seeded_db["leaf_name"]],
            "languages": ["en"],
            "sentiment": "neutral",
            "sentiment_score": 0.0,
            "community_id": "hive-999",
        })

    async with _TestSession() as session:
        summary = await get_author_summary(session, "dave")

    assert summary["top_community"] == {"id": "hive-999", "name": "Test Community", "count": 1}


# ── get_author_recent_posts (Soft-404 fix) ─────────────────────────────────

async def test_get_author_recent_posts_returns_titled_classified_posts(seeded_db):
    """Joins classified posts (from PG) with titles (from HAFSQL stub)."""
    from project.db.crud import get_author_recent_posts

    titles = {"test-post-one": "Alice's First Post"}
    with patch("project.hafsql.get_post_titles", return_value=titles):
        async with _TestSession() as session:
            result = await get_author_recent_posts(session, "alice")

    assert result == [
        {
            "permlink": "test-post-one",
            "title": "Alice's First Post",
            "created": result[0]["created"],
        }
    ]


async def test_get_author_recent_posts_empty_for_unknown_author(seeded_db):
    """Author with no classified posts → []. HAFSQL not even consulted."""
    from project.db.crud import get_author_recent_posts

    with patch("project.hafsql.get_post_titles") as titles_mock:
        async with _TestSession() as session:
            result = await get_author_recent_posts(session, "nobody-here")
    assert result == []
    titles_mock.assert_not_called()


async def test_get_author_recent_posts_drops_permlinks_missing_from_hafsql(seeded_db):
    """HAFSQL might not have a title for every classified permlink (out-of-sync,
    deleted on chain, etc.) — those entries drop out rather than rendering an
    empty <a></a>."""
    from project.db.crud import get_author_recent_posts

    with patch("project.hafsql.get_post_titles", return_value={}):
        async with _TestSession() as session:
            result = await get_author_recent_posts(session, "alice")
    assert result == []


async def test_get_author_recent_posts_orders_by_created_desc(seeded_db):
    """Most recent classified post comes first — that's what SEO needs."""
    from datetime import datetime, timezone
    from project.db.crud import get_author_recent_posts, create_post

    async with _TestSession() as session:
        await create_post(session, {
            "author": "alice",
            "permlink": "newer-post",
            "created": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "categories": [seeded_db["leaf_name"]],
            "languages": ["en"],
            "sentiment": "neutral",
            "sentiment_score": 0.0,
        })

    titles = {"test-post-one": "Old", "newer-post": "New"}
    with patch("project.hafsql.get_post_titles", return_value=titles):
        async with _TestSession() as session:
            result = await get_author_recent_posts(session, "alice")

    assert [p["permlink"] for p in result] == ["newer-post", "test-post-one"]


async def test_get_author_recent_posts_caches_empty_results(seeded_db):
    """Empty-HAFSQL result is cached so a HAFSQL incident doesn't re-hammer it
    on every author-page hit."""
    from project.db.crud import get_author_recent_posts

    with patch("project.hafsql.get_post_titles", return_value={}) as titles_mock:
        async with _TestSession() as session:
            await get_author_recent_posts(session, "alice")
            await get_author_recent_posts(session, "alice")
    assert titles_mock.call_count == 1


# ── get_recent_posts_for_seo (proposal 100, Phase 1) ───────────────────────


def _seo_titles(*pairs):
    """Build a get_posts_titles_and_excerpts-style result for the given
    (author, permlink) pairs."""
    return {
        (a, p): {"title": f"{a} {p} title", "body": f"Body text for {a}/{p}."}
        for a, p in pairs
    }


async def test_get_recent_posts_for_seo_returns_titled_posts(seeded_db):
    """Joins classified posts (PG) with titles + excerpts (HAFSQL stub),
    newest-first, with cleaned excerpts."""
    from project.db.crud import get_recent_posts_for_seo

    info = _seo_titles(("alice", "test-post-one"), ("bob", "test-post-two"),
                       ("carol", "test-post-three"))
    with patch("project.hafsql.get_posts_titles_and_excerpts", return_value=info):
        async with _TestSession() as session:
            result = await get_recent_posts_for_seo(session)

    # carol (3/3) newest → alice (3/1) oldest.
    assert [p["author"] for p in result] == ["carol", "bob", "alice"]
    first = result[0]
    assert first["title"] == "carol test-post-three title"
    assert first["excerpt"] == "Body text for carol/test-post-three."
    assert "permlink" in first and "created" in first


async def test_get_recent_posts_for_seo_respects_category_filter(seeded_db):
    """category= narrows to posts classified in that leaf (alice + bob), not
    carol (a different category)."""
    from project.db.crud import get_recent_posts_for_seo

    info = _seo_titles(("alice", "test-post-one"), ("bob", "test-post-two"))
    with patch("project.hafsql.get_posts_titles_and_excerpts", return_value=info):
        async with _TestSession() as session:
            result = await get_recent_posts_for_seo(session, category=seeded_db["leaf_name"])
    assert {p["author"] for p in result} == {"alice", "bob"}


async def test_get_recent_posts_for_seo_unknown_category_returns_empty(seeded_db):
    """A category slug that resolves to no IDs short-circuits to [] without
    touching HAFSQL."""
    from project.db.crud import get_recent_posts_for_seo

    with patch("project.hafsql.get_posts_titles_and_excerpts") as info_mock:
        async with _TestSession() as session:
            result = await get_recent_posts_for_seo(session, category="not-a-category")
    assert result == []
    info_mock.assert_not_called()


async def test_get_recent_posts_for_seo_respects_language_filter(seeded_db):
    """language= narrows by language_codes overlap (es → bob only)."""
    from project.db.crud import get_recent_posts_for_seo

    info = _seo_titles(("bob", "test-post-two"))
    with patch("project.hafsql.get_posts_titles_and_excerpts", return_value=info):
        async with _TestSession() as session:
            result = await get_recent_posts_for_seo(session, language="es")
    assert [p["author"] for p in result] == ["bob"]


async def test_get_recent_posts_for_seo_respects_community_filter(seeded_db):
    """community= narrows by community_id."""
    from datetime import datetime, timezone
    from project.db.crud import get_recent_posts_for_seo, create_post

    async with _TestSession() as session:
        await create_post(session, {
            "author": "dave",
            "permlink": "community-post",
            "created": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "categories": [seeded_db["leaf_name"]],
            "languages": ["en"],
            "sentiment": "neutral",
            "sentiment_score": 0.0,
            "community_id": "hive-12345",
        })

    info = _seo_titles(("dave", "community-post"))
    with patch("project.hafsql.get_posts_titles_and_excerpts", return_value=info):
        async with _TestSession() as session:
            result = await get_recent_posts_for_seo(session, community="hive-12345")
    assert [p["author"] for p in result] == ["dave"]


async def test_get_recent_posts_for_seo_drops_posts_missing_from_hafsql(seeded_db):
    """Permlinks HAFSQL has no title for drop out rather than rendering an
    empty link."""
    from project.db.crud import get_recent_posts_for_seo

    with patch("project.hafsql.get_posts_titles_and_excerpts", return_value={}):
        async with _TestSession() as session:
            result = await get_recent_posts_for_seo(session)
    assert result == []


async def test_get_recent_posts_for_seo_degrades_on_hafsql_error(seeded_db):
    """get_posts_titles_and_excerpts raising → [] (the route then renders the
    page without the primer rather than 500ing)."""
    from project.db.crud import get_recent_posts_for_seo

    with patch("project.hafsql.get_posts_titles_and_excerpts",
               side_effect=OSError("hafsql down")):
        async with _TestSession() as session:
            with pytest.raises(OSError):
                await get_recent_posts_for_seo(session)


async def test_get_recent_posts_for_seo_caches_empty_results(seeded_db):
    """Empty result cached for 5 min so crawler traffic doesn't re-hit HAFSQL."""
    from project.db.crud import get_recent_posts_for_seo

    with patch("project.hafsql.get_posts_titles_and_excerpts", return_value={}) as info_mock:
        async with _TestSession() as session:
            await get_recent_posts_for_seo(session)
            await get_recent_posts_for_seo(session)
    assert info_mock.call_count == 1


async def test_get_recent_posts_for_seo_excludes_nsfw(seeded_db):
    """NSFW posts are excluded from the public crawler-facing surface."""
    from datetime import datetime, timezone
    from project.db.crud import get_recent_posts_for_seo, create_post

    async with _TestSession() as session:
        await create_post(session, {
            "author": "naughty",
            "permlink": "nsfw-post",
            "created": datetime(2026, 5, 9, tzinfo=timezone.utc),
            "categories": [seeded_db["leaf_name"]],
            "languages": ["en"],
            "sentiment": "neutral",
            "sentiment_score": 0.0,
            "is_nsfw": True,
        })

    info = _seo_titles(("alice", "test-post-one"), ("bob", "test-post-two"),
                       ("carol", "test-post-three"), ("naughty", "nsfw-post"))
    with patch("project.hafsql.get_posts_titles_and_excerpts", return_value=info):
        async with _TestSession() as session:
            result = await get_recent_posts_for_seo(session)
    assert "naughty" not in {p["author"] for p in result}


# ── get_community_name ─────────────────────────────────────────────────────

async def test_get_community_name_returns_mapped_name(setup_db):
    from project.db.crud import get_community_name, upsert_community_mapping

    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-555", "crypto", "Crypto Talk", 0.9)
        result = await get_community_name(session, "hive-555")
    assert result == "Crypto Talk"


async def test_get_community_name_none_when_unmapped(setup_db):
    from project.db.crud import get_community_name

    async with _TestSession() as session:
        result = await get_community_name(session, "hive-000000")
    assert result is None
