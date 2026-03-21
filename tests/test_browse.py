"""Integration tests for the browse/discovery API."""
import pytest

from project.categories import CATEGORY_TREE


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


async def test_browse_filtered_total(client, seeded_db):
    """total reflects the filtered count, not the global total."""
    all_resp = await client.get("/api/browse")
    all_total = all_resp.json()["total"]

    filtered_resp = await client.get("/api/browse?sentiment=positive")
    filtered_total = filtered_resp.json()["total"]

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

async def test_browse_filter_by_community(client, seeded_db):
    """Posts with matching community_id are returned when filtering by community."""
    from tests.conftest import AUTH
    # Create a post with a community_id.
    await client.post("/posts", json={
        "author": "dave",
        "permlink": "community-post",
        "categories": [seeded_db["leaf_name"]],
        "languages": ["en"],
        "sentiment": "positive",
        "sentiment_score": 0.5,
        "community_id": "hive-174578",
    }, headers=AUTH)

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


async def test_browse_posts_include_title(client, seeded_db):
    """Browse response includes title field on posts."""
    resp = await client.get("/api/browse")
    assert resp.status_code == 200
    for post in resp.json()["posts"]:
        assert "title" in post


# ── Communities endpoint ─────────────────────────────────────────────────────

async def test_communities_endpoint_empty(client):
    resp = await client.get("/api/communities")
    assert resp.status_code == 200
    assert resp.json()["communities"] == []


async def test_communities_endpoint_with_data(client, seeded_db):
    from tests.conftest import AUTH
    await client.post("/posts", json={
        "author": "dave",
        "permlink": "comm-post-1",
        "categories": [seeded_db["leaf_name"]],
        "languages": ["en"],
        "sentiment": "positive",
        "sentiment_score": 0.5,
        "community_id": "hive-174578",
    }, headers=AUTH)
    await client.post("/posts", json={
        "author": "eve",
        "permlink": "comm-post-2",
        "categories": [seeded_db["leaf_name"]],
        "languages": ["en"],
        "sentiment": "neutral",
        "sentiment_score": 0.0,
        "community_id": "hive-174578",
    }, headers=AUTH)

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
    from project.db.crud import upsert_community_mapping
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
    from project.db.crud import upsert_community_mapping
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
    from tests.conftest import AUTH
    from project.db.crud import upsert_community_mapping
    leaf = seeded_db["leaf_name"]

    await upsert_community_mapping(
        db_session, "hive-174578", leaf, "Photography Lovers", 0.55,
    )
    # Create posts with this community.
    await client.post("/posts", json={
        "author": "dave", "permlink": "sugg-1",
        "categories": [leaf], "languages": ["en"],
        "sentiment": "positive", "sentiment_score": 0.5,
        "community_id": "hive-174578",
    }, headers=AUTH)
    await client.post("/posts", json={
        "author": "eve", "permlink": "sugg-2",
        "categories": [leaf], "languages": ["en"],
        "sentiment": "neutral", "sentiment_score": 0.0,
        "community_id": "hive-174578",
    }, headers=AUTH)

    resp = await client.get(f"/api/communities/suggested?category={leaf}")
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert len(suggestions) >= 1
    match = next(s for s in suggestions if s["id"] == "hive-174578")
    assert match["post_count"] >= 2


async def test_suggested_communities_no_null_category(client, seeded_db, db_session):
    """Communities without a category match should not appear in suggestions."""
    from project.db.crud import upsert_community_mapping
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
    from project.db.crud import upsert_community_mapping
    await upsert_community_mapping(db_session, "hive-174578", "photography", "Photo", 0.55)

    r1 = await client.get("/api/communities/suggested?category=photography")
    r2 = await client.get("/api/communities/suggested?category=photography")
    assert r1.status_code == 200
    assert r1.json() == r2.json()


async def test_suggested_communities_sorted_category_key(client, seeded_db, db_session):
    """Different category order should use same cache key."""
    from project.db.crud import upsert_community_mapping
    await upsert_community_mapping(db_session, "hive-111", "food", "Foodies", 0.50)
    await upsert_community_mapping(db_session, "hive-222", "photography", "Photo", 0.55)

    r1 = await client.get("/api/communities/suggested?category=food&category=photography")
    r2 = await client.get("/api/communities/suggested?category=photography&category=food")
    assert r1.status_code == 200
    assert r1.json() == r2.json()


# ── Browse pagination edge cases ────────────────────────────────────────────

async def test_browse_malformed_cursor(client, seeded_db):
    """Malformed cursor should fall back to offset pagination, not crash."""
    resp = await client.get("/api/browse?cursor=not_a_valid_cursor")
    assert resp.status_code == 200
    assert "posts" in resp.json()


async def test_browse_cursor_wrong_format(client, seeded_db):
    """Cursor missing underscore separator should fall back gracefully."""
    resp = await client.get("/api/browse?cursor=12345678")
    assert resp.status_code == 200
    assert "posts" in resp.json()


# ── Community edge cases ────────────────────────────────────────────────────

async def test_communities_zero_posts_not_shown(client):
    """Communities with no posts should not appear in /api/communities."""
    resp = await client.get("/api/communities")
    assert resp.status_code == 200
    # Empty DB has no communities.
    assert resp.json()["communities"] == []


async def test_browse_community_name_coalesce(client, seeded_db):
    """Post with community_id but no mapping should use fallback name."""
    from tests.conftest import AUTH
    leaf = seeded_db["leaf_name"]
    await client.post("/posts", json={
        "author": "test", "permlink": "no-mapping",
        "categories": [leaf], "languages": ["en"],
        "sentiment": "neutral", "sentiment_score": 0.0,
        "community_id": "hive-999999",
    }, headers=AUTH)
    resp = await client.get("/api/browse?community=hive-999999")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) >= 1
    # community_name should be None (no mapping exists).
    assert posts[0]["community_name"] is None
