"""Integration tests for the browse/discovery API."""
from datetime import datetime, timedelta, timezone

import pytest

from project.categories import CATEGORY_TREE
from project.db.crud import create_post, upsert_community_mapping


async def _create_post(db_session, **kwargs):
    """Helper to create a post directly via CRUD."""
    await create_post(db_session, kwargs)


# ── Browse endpoint ──────────────────────────────────────────────────────────

async def test_browse_returns_posts(client, seeded_db):
    resp = await client.get("/api/browse")
    assert resp.status_code == 200
    data = resp.json()
    assert "posts" in data
    assert "count" in data
    assert "total" in data
    assert data["count"] == len(data["posts"])
    assert data["count"] >= 3
    assert data["total"] >= data["count"]

    # Each post should have categories and languages attached.
    for post in data["posts"]:
        assert "categories" in post
        assert "languages" in post
        assert isinstance(post["categories"], list)
        assert isinstance(post["languages"], list)


async def test_browse_filter_by_category(client, seeded_db):
    leaf = seeded_db["leaf_name"]
    resp = await client.get(f"/api/browse?category={leaf}")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 2
    for post in posts:
        assert leaf in post["categories"]


async def test_browse_filter_by_parent_category(client, seeded_db):
    parent = seeded_db["parent_name"]
    resp = await client.get(f"/api/browse?category={parent}")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    # Posts under any child of this parent should be included.
    assert len(posts) >= 2


async def test_browse_filter_by_language(client, seeded_db):
    resp = await client.get("/api/browse?language=en")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 2
    for post in posts:
        assert "en" in post["languages"]


async def test_browse_filter_by_sentiment(client, seeded_db):
    resp = await client.get("/api/browse?sentiment=positive")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    for post in posts:
        assert post["sentiment"] == "positive"


async def test_browse_combined_filters(client, seeded_db):
    leaf = seeded_db["leaf_name"]
    resp = await client.get(f"/api/browse?category={leaf}&language=en")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    for post in posts:
        assert leaf in post["categories"]
        assert "en" in post["languages"]


async def test_browse_pagination(client, seeded_db):
    resp1 = await client.get("/api/browse?limit=2&offset=0")
    resp2 = await client.get("/api/browse?limit=2&offset=2")
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    posts1 = resp1.json()["posts"]
    posts2 = resp2.json()["posts"]
    assert len(posts1) == 2

    # No overlap between pages.
    ids1 = {p["id"] for p in posts1}
    ids2 = {p["id"] for p in posts2}
    assert ids1.isdisjoint(ids2)


async def test_browse_cursor_pagination(client, seeded_db):
    resp1 = await client.get("/api/browse?limit=2")
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["next_cursor"] is not None

    resp2 = await client.get(f"/api/browse?limit=2&cursor={data1['next_cursor']}")
    assert resp2.status_code == 200
    data2 = resp2.json()

    # No overlap.
    ids1 = {p["id"] for p in data1["posts"]}
    ids2 = {p["id"] for p in data2["posts"]}
    assert ids1.isdisjoint(ids2)


@pytest.mark.parametrize("filter_qs", [
    "sentiment=positive",
    "authors=alice",
], ids=["sentiment", "authors"])
async def test_browse_filtered_total(client, seeded_db, filter_qs):
    """total reflects the filtered count, not the global total."""
    all_total = (await client.get("/api/browse")).json()["total"]
    filtered_total = (await client.get(f"/api/browse?{filter_qs}")).json()["total"]
    assert filtered_total >= 1
    assert filtered_total < all_total


async def test_browse_cursor_total_consistent(client, seeded_db):
    """total on cursor page reflects full filtered count, not just remaining rows."""
    resp1 = await client.get("/api/browse?limit=2")
    data1 = resp1.json()
    assert data1["next_cursor"] is not None
    assert data1["total"] >= data1["count"]

    resp2 = await client.get(f"/api/browse?limit=2&cursor={data1['next_cursor']}")
    data2 = resp2.json()
    assert data2["total"] >= data2["count"]


async def test_browse_empty_db(client):
    resp = await client.get("/api/browse")
    assert resp.status_code == 200
    data = resp.json()
    assert data["posts"] == []
    assert data["count"] == 0
    assert data["total"] == 0


async def test_browse_limit_bounds(client):
    resp = await client.get("/api/browse?limit=0")
    assert resp.status_code == 422
    resp = await client.get("/api/browse?limit=201")
    assert resp.status_code == 422


# ── Community filter ────────────────────────────────────────────────────────

async def test_browse_filter_by_community(client, seeded_db, db_session):
    """Posts with matching community_id are returned when filtering by community."""
    await _create_post(db_session,
        author="dave", permlink="community-post",
        categories=[seeded_db["leaf_name"]], languages=["en"],
        sentiment="positive", sentiment_score=0.5,
        community_id="hive-174578",
    )

    resp = await client.get("/api/browse?community=hive-174578")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    for post in posts:
        assert post["community_id"] == "hive-174578"


async def test_browse_community_filter_excludes_others(client, seeded_db):
    """Community filter excludes posts without that community_id."""
    resp = await client.get("/api/browse?community=hive-999999")
    assert resp.status_code == 200
    assert resp.json()["posts"] == []
    assert resp.json()["total"] == 0


async def test_browse_posts_include_community_id(client, seeded_db):
    """Browse response includes community_id and community_name fields on posts."""
    resp = await client.get("/api/browse")
    assert resp.status_code == 200
    for post in resp.json()["posts"]:
        assert "community_id" in post
        assert "community_name" in post



# ── Communities endpoint ─────────────────────────────────────────────────────

async def test_communities_endpoint_empty(client):
    resp = await client.get("/api/communities")
    assert resp.status_code == 200
    assert resp.json()["communities"] == []


async def test_communities_endpoint_with_data(client, seeded_db, db_session):
    await _create_post(db_session,
        author="dave", permlink="comm-post-1",
        categories=[seeded_db["leaf_name"]], languages=["en"],
        sentiment="positive", sentiment_score=0.5,
        community_id="hive-174578",
    )
    await _create_post(db_session,
        author="eve", permlink="comm-post-2",
        categories=[seeded_db["leaf_name"]], languages=["en"],
        sentiment="neutral", sentiment_score=0.0,
        community_id="hive-174578",
    )

    resp = await client.get("/api/communities")
    assert resp.status_code == 200
    communities = resp.json()["communities"]
    assert len(communities) >= 1
    comm = next(c for c in communities if c["id"] == "hive-174578")
    assert comm["post_count"] >= 2
    assert "name" in comm
    assert "category" in comm


# ── Suggested communities endpoint ────────────────────────────────────────────


async def test_suggested_communities_requires_category(client):
    resp = await client.get("/api/communities/suggested")
    assert resp.status_code == 422


async def test_suggested_communities_empty(client, seeded_db):
    resp = await client.get("/api/communities/suggested?category=photography")
    assert resp.status_code == 200
    assert resp.json()["suggestions"] == []


async def test_suggested_communities_returns_matches(client, seeded_db, db_session):
    await upsert_community_mapping(
        db_session, "hive-174578", "photography", "Photography Lovers", 0.55,
    )
    await upsert_community_mapping(
        db_session, "hive-196037", "food", "FoodiesUnite", 0.48,
    )

    resp = await client.get("/api/communities/suggested?category=photography")
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["id"] == "hive-174578"
    assert suggestions[0]["name"] == "Photography Lovers"
    assert suggestions[0]["category"] == "photography"


async def test_suggested_communities_multiple_categories(client, seeded_db, db_session):
    await upsert_community_mapping(
        db_session, "hive-174578", "photography", "Photography Lovers", 0.55,
    )
    await upsert_community_mapping(
        db_session, "hive-196037", "food", "FoodiesUnite", 0.48,
    )
    await upsert_community_mapping(
        db_session, "hive-111111", "crypto", "LeoFinance", 0.62,
    )

    resp = await client.get(
        "/api/communities/suggested?category=photography&category=food"
    )
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) == 2
    ids = {s["id"] for s in suggestions}
    assert "hive-174578" in ids
    assert "hive-196037" in ids
    assert "hive-111111" not in ids


async def test_suggested_communities_includes_post_count(client, seeded_db, db_session):
    from project.db.crud import upsert_community_mapping
    leaf = seeded_db["leaf_name"]

    await upsert_community_mapping(
        db_session, "hive-174578", leaf, "Photography Lovers", 0.55,
    )
    await _create_post(db_session,
        author="dave", permlink="sugg-1",
        categories=[leaf], languages=["en"],
        sentiment="positive", sentiment_score=0.5,
        community_id="hive-174578",
    )
    await _create_post(db_session,
        author="eve", permlink="sugg-2",
        categories=[leaf], languages=["en"],
        sentiment="neutral", sentiment_score=0.0,
        community_id="hive-174578",
    )

    resp = await client.get(f"/api/communities/suggested?category={leaf}")
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) >= 1
    match = next(s for s in suggestions if s["id"] == "hive-174578")
    assert match["post_count"] >= 2


async def test_suggested_communities_no_null_category(client, seeded_db, db_session):
    """Communities without a category match should not appear in suggestions."""
    await upsert_community_mapping(
        db_session, "hive-999999", None, "Unmapped Community", 0.15,
    )
    resp = await client.get("/api/communities/suggested?category=photography")
    assert resp.status_code == 200
    assert resp.json()["suggestions"] == []


# ── Languages endpoint ───────────────────────────────────────────────────────

async def test_languages_endpoint(client, seeded_db):
    resp = await client.get("/api/languages")
    assert resp.status_code == 200
    data = resp.json()
    assert "languages" in data
    lang_codes = [l["language"] for l in data["languages"]]
    assert "en" in lang_codes


# ── Stats endpoint ───────────────────────────────────────────────────────────

async def test_stats_endpoint(client, seeded_db):
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_posts"] >= 3
    assert data["languages"] >= 2


# ── Cache TTL verification ──────────────────────────────────────────────────

async def test_communities_endpoint_cached(client, seeded_db):
    """Second call to /api/communities should hit cache."""
    r1 = await client.get("/api/communities")
    r2 = await client.get("/api/communities")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()


async def test_suggested_communities_cached(client, seeded_db, db_session):
    await upsert_community_mapping(db_session, "hive-174578", "photography", "Photo", 0.55)

    r1 = await client.get("/api/communities/suggested?category=photography")
    r2 = await client.get("/api/communities/suggested?category=photography")
    assert r1.status_code == 200
    assert r1.json() == r2.json()


async def test_suggested_communities_sorted_category_key(client, seeded_db, db_session):
    """Different category order should use same cache key."""
    await upsert_community_mapping(db_session, "hive-111", "food", "Foodies", 0.50)
    await upsert_community_mapping(db_session, "hive-222", "photography", "Photo", 0.55)

    r1 = await client.get("/api/communities/suggested?category=food&category=photography")
    r2 = await client.get("/api/communities/suggested?category=photography&category=food")
    assert r1.status_code == 200
    assert r1.json() == r2.json()


# ── Browse pagination edge cases ────────────────────────────────────────────

@pytest.mark.parametrize("cursor", [
    "not_a_valid_cursor",
    "12345678",  # missing underscore separator
], ids=["malformed", "wrong-format"])
async def test_browse_malformed_cursor_fallback(client, seeded_db, cursor):
    """Malformed cursors should fall back gracefully, not crash."""
    resp = await client.get(f"/api/browse?cursor={cursor}")
    assert resp.status_code == 200
    assert "posts" in resp.json()


# ── Community edge cases ────────────────────────────────────────────────────

async def test_browse_community_name_coalesce(client, seeded_db, db_session):
    """Post with community_id but no mapping should use fallback name."""
    leaf = seeded_db["leaf_name"]
    await _create_post(db_session,
        author="test", permlink="no-mapping",
        categories=[leaf], languages=["en"],
        sentiment="neutral", sentiment_score=0.0,
        community_id="hive-999999",
    )
    resp = await client.get("/api/browse?community=hive-999999")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    # community_name should be None (no mapping exists).
    assert posts[0]["community_name"] is None


# ── Multi-community filter ─────────────────────────────────────────────────

async def test_browse_filter_by_communities(client, seeded_db, db_session):
    """The communities param filters posts to multiple community IDs."""
    leaf = seeded_db["leaf_name"]
    for cid, author, perm in [
        ("hive-111111", "alice", "mc-1"),
        ("hive-222222", "bob", "mc-2"),
        ("hive-333333", "carol", "mc-3"),
    ]:
        await _create_post(db_session,
            author=author, permlink=perm,
            categories=[leaf], languages=["en"],
            sentiment="neutral", sentiment_score=0.0,
            community_id=cid,
        )

    resp = await client.get(
        "/api/browse?communities=hive-111111&communities=hive-222222"
    )
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 2
    returned_communities = {p["community_id"] for p in posts}
    assert returned_communities <= {"hive-111111", "hive-222222"}


async def test_browse_communities_overrides_community(client, seeded_db, db_session):
    """When both community and communities are provided, communities wins."""
    leaf = seeded_db["leaf_name"]
    await _create_post(db_session,
        author="dave", permlink="override-1",
        categories=[leaf], languages=["en"],
        sentiment="neutral", sentiment_score=0.0,
        community_id="hive-444444",
    )

    # community=hive-444444 should be ignored when communities is set
    resp = await client.get(
        "/api/browse?community=hive-444444&communities=hive-999999"
    )
    assert resp.status_code == 200
    assert resp.json()["posts"] == []
    assert resp.json()["total"] == 0


async def test_browse_communities_empty_list(client, seeded_db):
    """Omitting communities should return all posts (no filter)."""
    resp = await client.get("/api/browse")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 3


# ── Authors filter ─────────────────────────────────────────────────────────

async def test_browse_filter_by_authors(client, seeded_db):
    """The authors param filters posts to specific authors."""
    resp = await client.get("/api/browse?authors=alice")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    for post in posts:
        assert post["author"] == "alice"


async def test_browse_filter_by_multiple_authors(client, seeded_db):
    """Multiple authors param returns posts from all specified authors."""
    resp = await client.get("/api/browse?authors=alice&authors=bob")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 2
    returned_authors = {p["author"] for p in posts}
    assert returned_authors <= {"alice", "bob"}


async def test_browse_authors_excludes_others(client, seeded_db):
    """Authors filter excludes posts from non-matching authors."""
    resp = await client.get("/api/browse?authors=alice")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    for post in posts:
        assert post["author"] != "bob"
        assert post["author"] != "carol"


async def test_browse_authors_no_match(client, seeded_db):
    """Authors filter with unknown author returns empty."""
    resp = await client.get("/api/browse?authors=nobody")
    assert resp.status_code == 200
    assert resp.json()["posts"] == []
    assert resp.json()["total"] == 0


async def test_browse_authors_combined_with_category(client, seeded_db):
    """Authors filter works together with category filter."""
    leaf = seeded_db["leaf_name"]
    resp = await client.get(f"/api/browse?authors=alice&category={leaf}")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    for post in posts:
        assert post["author"] == "alice"
        assert leaf in post["categories"]


# ── Filter list truncation (proposal 033) ─────────────────────────────────

async def test_browse_truncates_long_filter_lists(client, seeded_db):
    """Lists exceeding the max length are silently truncated, not rejected."""
    # Build query strings exceeding each limit.
    cats = "&".join(f"category=cat{i}" for i in range(60))       # limit 50
    langs = "&".join(f"language=lang{i}" for i in range(110))    # limit 100
    comms = "&".join(f"communities=hive-{i}" for i in range(210))  # limit 200
    authors = "&".join(f"authors=user{i}" for i in range(3100))  # limit 3000

    # All four combined — should succeed (200), not 422 or 500.
    qs = f"{cats}&{langs}&{comms}&{authors}"
    resp = await client.get(f"/api/browse?{qs}")
    assert resp.status_code == 200
    data = resp.json()
    assert "posts" in data
    # No matching data expected, but the request must not error.
    assert isinstance(data["posts"], list)


# ── max_age filter ─────────────────────────────────────────────────────────

async def test_browse_max_age_filters_old_posts(client, seeded_db, db_session):
    """max_age=1h should exclude posts older than 1 hour."""
    leaf = seeded_db["leaf_name"]
    now = datetime.now(timezone.utc)
    await _create_post(db_session,
        author="recent", permlink="recent-post",
        categories=[leaf], languages=["en"],
        sentiment="neutral", sentiment_score=0.0,
        created=now - timedelta(minutes=30),
    )
    resp = await client.get("/api/browse?max_age=1h")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    # Only the recent post should appear; seeded posts are from March 2026.
    assert len(posts) >= 1
    authors = {p["author"] for p in posts}
    assert "recent" in authors
    # Seeded posts (days old) should be excluded.
    assert "alice" not in authors
    assert "bob" not in authors


async def test_browse_max_age_days(client, seeded_db, db_session):
    """max_age=1d should filter to last 24 hours."""
    leaf = seeded_db["leaf_name"]
    now = datetime.now(timezone.utc)
    await _create_post(db_session,
        author="yesterday", permlink="yesterday-post",
        categories=[leaf], languages=["en"],
        sentiment="neutral", sentiment_score=0.0,
        created=now - timedelta(hours=12),
    )
    resp = await client.get("/api/browse?max_age=1d")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    authors = {p["author"] for p in posts}
    assert "yesterday" in authors


async def test_browse_max_age_invalid_format(client, seeded_db):
    """Invalid max_age format should return 422."""
    resp = await client.get("/api/browse?max_age=abc")
    assert resp.status_code == 422


# ── sort parameter ─────────────────────────────────────────────────────────

async def test_browse_sort_oldest(client, seeded_db):
    """sort=oldest should return posts in ascending order."""
    resp = await client.get("/api/browse?sort=oldest")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 2
    dates = [p["created"] for p in posts]
    assert dates == sorted(dates)


async def test_browse_sort_newest_default(client, seeded_db):
    """Default sort (newest) should return posts in descending order."""
    resp = await client.get("/api/browse")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 2
    dates = [p["created"] for p in posts]
    assert dates == sorted(dates, reverse=True)


async def test_browse_sort_invalid(client, seeded_db):
    """Invalid sort value should return 422."""
    resp = await client.get("/api/browse?sort=random")
    assert resp.status_code == 422


async def test_browse_sort_oldest_cursor_pagination(client, seeded_db, db_session):
    """Cursor pagination should work correctly with sort=oldest."""
    leaf = seeded_db["leaf_name"]
    now = datetime.now(timezone.utc)
    # Add more posts so we have enough for pagination.
    for i in range(3):
        await _create_post(db_session,
            author=f"user{i}", permlink=f"sort-page-{i}",
            categories=[leaf], languages=["en"],
            sentiment="neutral", sentiment_score=0.0,
            created=now - timedelta(hours=i),
        )

    resp1 = await client.get("/api/browse?sort=oldest&limit=2")
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["next_cursor"] is not None

    resp2 = await client.get(f"/api/browse?sort=oldest&limit=2&cursor={data1['next_cursor']}")
    assert resp2.status_code == 200
    data2 = resp2.json()

    # No overlap between pages.
    ids1 = {p["id"] for p in data1["posts"]}
    ids2 = {p["id"] for p in data2["posts"]}
    assert ids1.isdisjoint(ids2)

    # Page 2 posts should be newer than page 1 posts (ascending order).
    if data2["posts"]:
        assert data1["posts"][-1]["created"] <= data2["posts"][0]["created"]


# ── POST /api/browse ──────────────────────────────────────────────────────


async def test_post_browse_returns_posts(client, seeded_db):
    """POST /api/browse returns same shape as GET."""
    resp = await client.post("/api/browse", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "posts" in data
    assert "count" in data
    assert "total" in data
    assert "next_cursor" in data
    assert data["count"] == len(data["posts"])
    assert data["count"] >= 3


async def test_post_browse_filter_by_authors(client, seeded_db):
    """POST /api/browse filters by authors in JSON body."""
    resp = await client.post("/api/browse", json={"authors": ["alice"]})
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    for post in posts:
        assert post["author"] == "alice"


async def test_post_browse_filter_by_category(client, seeded_db):
    """POST /api/browse filters by category in JSON body."""
    leaf = seeded_db["leaf_name"]
    resp = await client.post("/api/browse", json={"category": [leaf]})
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    for post in posts:
        assert leaf in post["categories"]


async def test_post_browse_validates_max_age(client, seeded_db):
    """POST /api/browse validates max_age pattern."""
    resp = await client.post("/api/browse", json={"max_age": "abc"})
    assert resp.status_code == 422


async def test_post_browse_validates_sort(client, seeded_db):
    """POST /api/browse validates sort pattern."""
    resp = await client.post("/api/browse", json={"sort": "random"})
    assert resp.status_code == 422
