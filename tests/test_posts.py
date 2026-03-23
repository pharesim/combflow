"""Integration tests for post detail endpoint."""


async def test_get_post_detail(client, seeded_db):
    resp = await client.get("/posts/alice/test-post-one")
    assert resp.status_code == 200
    post = resp.json()
    assert post["author"] == "alice"
    assert post["permlink"] == "test-post-one"
    assert "categories" in post
    assert len(post["categories"]) >= 1
    assert "languages" in post
    assert "en" in post["languages"]
    assert post["sentiment"] == "positive"


async def test_post_detail_community_id_null(client, seeded_db):
    """Posts without community_id should return null."""
    resp = await client.get("/posts/alice/test-post-one")
    assert resp.status_code == 200
    assert resp.json()["community_id"] is None
