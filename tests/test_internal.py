"""Tests for internal API endpoints and CRUD functions (proposals 018 + 019)."""
import pytest

from tests.conftest import AUTH, _TestSession


# ── Centroid upload & reload (POST /internal/centroids) ──────────────────────

SAMPLE_CENTROIDS = {
    "crypto": [0.1] * 384,
    "programming": [0.2] * 384,
}
CENTROID_META = {
    "similarity_threshold": 0.45,
    "llm_model": "test-model",
    "embedding_model": "test-embed",
    "posts_labeled": 50,
}


async def test_upload_centroids_saves_and_reloads(client, seeded_db):
    """Upload centroids, verify response shape, verify they persist and reload into app.state."""
    resp = await client.post(
        "/internal/centroids",
        json={"centroids": SAMPLE_CENTROIDS, "metadata": CENTROID_META},
        headers=AUTH,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved"] == 2
    assert data["active"] >= 2
    assert data["threshold"] == 0.45


async def test_upload_centroids_upserts(client, seeded_db):
    """Upload, then re-upload with different vectors — verify overwrite, not duplicate."""
    await client.post(
        "/internal/centroids",
        json={"centroids": SAMPLE_CENTROIDS, "metadata": CENTROID_META},
        headers=AUTH,
    )
    # Re-upload with different vectors.
    updated = {"crypto": [0.9] * 384, "programming": [0.8] * 384}
    resp = await client.post(
        "/internal/centroids",
        json={"centroids": updated, "metadata": CENTROID_META},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["saved"] == 2
    assert resp.json()["active"] == 2


# ── Stream cursors (GET/PUT /internal/stream-cursor/{key}) ───────────────────

async def test_cursor_not_found(client, seeded_db):
    """GET missing key returns 404."""
    resp = await client.get("/internal/stream-cursor/nonexistent", headers=AUTH)
    assert resp.status_code == 404


async def test_cursor_set_and_get(client, seeded_db):
    """PUT then GET — verify round-trip."""
    put_resp = await client.put(
        "/internal/stream-cursor/live",
        json={"block_num": 95000000},
        headers=AUTH,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["block_num"] == 95000000

    get_resp = await client.get("/internal/stream-cursor/live", headers=AUTH)
    assert get_resp.status_code == 200
    assert get_resp.json()["block_num"] == 95000000


async def test_cursor_upsert(client, seeded_db):
    """PUT twice with different block_num — verify update, not insert."""
    await client.put(
        "/internal/stream-cursor/backfill",
        json={"block_num": 1000},
        headers=AUTH,
    )
    await client.put(
        "/internal/stream-cursor/backfill",
        json={"block_num": 2000},
        headers=AUTH,
    )
    get_resp = await client.get("/internal/stream-cursor/backfill", headers=AUTH)
    assert get_resp.status_code == 200
    assert get_resp.json()["block_num"] == 2000


# ── CRUD unit tests ──────────────────────────────────────────────────────────

async def test_existing_author_permlinks_empty(seeded_db):
    """Empty input returns empty set without hitting DB."""
    from project.db.crud import existing_author_permlinks

    async with _TestSession() as session:
        result = await existing_author_permlinks(session, [])
    assert result == set()


async def test_existing_author_permlinks_returns_matches(seeded_db):
    """Verify dedup check returns existing (author, permlink) pairs."""
    from project.db.crud import existing_author_permlinks

    async with _TestSession() as session:
        result = await existing_author_permlinks(session, [
            ("alice", "test-post-one"),   # exists
            ("bob", "test-post-two"),     # exists
            ("nobody", "fake-post"),      # does not exist
        ])
    assert ("alice", "test-post-one") in result
    assert ("bob", "test-post-two") in result
    assert ("nobody", "fake-post") not in result
    assert len(result) == 2


async def test_centroids_round_trip(seeded_db):
    """save_centroids then get_centroids — vectors match within float tolerance."""
    from project.db.crud import save_centroids, get_centroids

    vecs = {"testcat": [0.123456] * 384}
    meta = {"posts_labeled": 10, "llm_model": "m", "embedding_model": "e"}

    async with _TestSession() as session:
        await save_centroids(session, vecs, meta)

    async with _TestSession() as session:
        loaded = await get_centroids(session)

    assert "testcat" in loaded
    assert len(loaded["testcat"]) == 384
    assert abs(loaded["testcat"][0] - 0.123456) < 1e-4


async def test_cursor_missing_returns_none(seeded_db):
    """get_cursor for unknown key returns None."""
    from project.db.crud import get_cursor

    async with _TestSession() as session:
        result = await get_cursor(session, "nonexistent-key")
    assert result is None


async def test_cursor_upsert_semantics(seeded_db):
    """set_cursor twice, get_cursor returns latest value."""
    from project.db.crud import get_cursor, set_cursor

    async with _TestSession() as session:
        await set_cursor(session, "test-key", 100)
    async with _TestSession() as session:
        await set_cursor(session, "test-key", 200)
    async with _TestSession() as session:
        result = await get_cursor(session, "test-key")
    assert result == 200


# ── Community mapping CRUD ──────────────────────────────────────────────────

async def test_upsert_community_mapping(seeded_db):
    from project.db.crud import upsert_community_mapping

    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-174578", "photography", "Photo Lovers", 0.55)

    # Verify via raw SQL.
    from sqlalchemy import text
    async with _TestSession() as session:
        row = await session.execute(
            text("SELECT * FROM community_mappings WHERE community_id = :cid"),
            {"cid": "hive-174578"},
        )
        r = row.mappings().first()
    assert r is not None
    assert r["category_slug"] == "photography"
    assert r["community_name"] == "Photo Lovers"
    assert r["score"] == pytest.approx(0.55)


async def test_upsert_community_mapping_updates(seeded_db):
    """Second upsert should update, not duplicate."""
    from project.db.crud import upsert_community_mapping

    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-100000", "crypto", "OldName", 0.40)
    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-100000", "finance", "NewName", 0.60)

    from sqlalchemy import text
    async with _TestSession() as session:
        rows = await session.execute(
            text("SELECT * FROM community_mappings WHERE community_id = :cid"),
            {"cid": "hive-100000"},
        )
        all_rows = rows.mappings().all()
    assert len(all_rows) == 1
    assert all_rows[0]["category_slug"] == "finance"
    assert all_rows[0]["community_name"] == "NewName"


async def test_get_suggested_communities_empty_categories(seeded_db):
    from project.db.crud import get_suggested_communities

    async with _TestSession() as session:
        result = await get_suggested_communities(session, [])
    assert result == []


async def test_get_suggested_communities_filters_by_category(seeded_db):
    from project.db.crud import upsert_community_mapping, get_suggested_communities

    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-111", "photography", "Photogs", 0.50)
    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-222", "crypto", "CoinTalk", 0.45)

    async with _TestSession() as session:
        result = await get_suggested_communities(session, ["photography"])
    assert len(result) == 1
    assert result[0]["id"] == "hive-111"


async def test_get_suggested_communities_excludes_null_category(seeded_db):
    from project.db.crud import upsert_community_mapping, get_suggested_communities

    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-333", None, "Unmapped", 0.10)

    async with _TestSession() as session:
        result = await get_suggested_communities(session, ["photography"])
    assert result == []


# ── Browse edge cases ────────────────────────────────────────────────────────

async def test_browse_posts_empty_db(seeded_db):
    """browse_posts with no matching filters returns empty."""
    from project.db.crud import browse_posts

    async with _TestSession() as session:
        result = await browse_posts(session, categories=["nonexistent-category-xyz"])
    assert result["posts"] == []
    assert result["total"] == 0


async def test_browse_posts_with_community_name(seeded_db):
    """browse_posts includes community_name from community_mappings."""
    from project.db.crud import create_post, upsert_community_mapping, browse_posts

    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-444", "photography", "Photogs", 0.50)

    from project.categories import CATEGORY_TREE
    leaf = CATEGORY_TREE[list(CATEGORY_TREE.keys())[0]][0]
    async with _TestSession() as session:
        await create_post(session, {
            "author": "testuser", "permlink": "comm-name-test",
            "categories": [leaf], "languages": ["en"],
            "sentiment": "positive", "sentiment_score": 0.5,
            "community_id": "hive-444",
        })

    async with _TestSession() as session:
        result = await browse_posts(session, community="hive-444")
    assert len(result["posts"]) >= 1
    assert result["posts"][0]["community_name"] == "Photogs"


async def test_get_post_by_permlink_community_name(seeded_db):
    """get_post_by_permlink includes community_name."""
    from project.db.crud import create_post, upsert_community_mapping, get_post_by_permlink

    async with _TestSession() as session:
        await upsert_community_mapping(session, "hive-555", "food", "Foodies", 0.48)

    from project.categories import CATEGORY_TREE
    leaf = CATEGORY_TREE[list(CATEGORY_TREE.keys())[0]][0]
    async with _TestSession() as session:
        await create_post(session, {
            "author": "testuser2", "permlink": "detail-comm-name",
            "categories": [leaf], "languages": ["en"],
            "sentiment": "neutral", "sentiment_score": 0.0,
            "community_id": "hive-555",
        })

    async with _TestSession() as session:
        post = await get_post_by_permlink(session, "testuser2", "detail-comm-name")
    assert post is not None
    assert post["community_name"] == "Foodies"


async def test_get_post_by_permlink_no_community(seeded_db):
    """Posts without community_id have community_name = None."""
    from project.db.crud import get_post_by_permlink

    async with _TestSession() as session:
        post = await get_post_by_permlink(session, "alice", "test-post-one")
    assert post is not None
    assert post["community_id"] is None
    assert post["community_name"] is None
