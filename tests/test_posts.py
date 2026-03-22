"""Integration tests for post CRUD endpoints."""
from project.categories import CATEGORY_TREE
from tests.conftest import AUTH


async def test_create_post_happy_path(client, seeded_db):
    resp = await client.post(
        "/posts",
        json={
            "author": "dave",
            "permlink": "new-post",
            "categories": [seeded_db["leaf_name"]],
            "languages": ["en"],
            "sentiment": "positive",
            "sentiment_score": 0.8,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    # Verify it's retrievable.
    get_resp = await client.get("/posts/dave/new-post")
    assert get_resp.status_code == 200
    post = get_resp.json()
    assert post["author"] == "dave"
    assert seeded_db["leaf_name"] in post["categories"]
    assert "en" in post["languages"]


async def test_create_post_upsert(client, seeded_db):
    """Same author+permlink should update, not duplicate."""
    payload = {
        "author": "eve",
        "permlink": "upsert-test",
        "categories": [seeded_db["leaf_name"]],
        "languages": ["en"],
        "sentiment": "positive",
        "sentiment_score": 0.5,
    }
    await client.post("/posts", json=payload, headers=AUTH)

    # Update with different categories/languages.
    second_leaf = list(CATEGORY_TREE.values())[1][0]
    payload["categories"] = [second_leaf]
    payload["languages"] = ["de"]
    payload["sentiment"] = "negative"
    await client.post("/posts", json=payload, headers=AUTH)

    get_resp = await client.get("/posts/eve/upsert-test")
    assert get_resp.status_code == 200
    post = get_resp.json()
    assert post["sentiment"] == "negative"
    assert second_leaf in post["categories"]
    assert "de" in post["languages"]
    # Old values should be gone.
    assert seeded_db["leaf_name"] not in post["categories"]
    assert "en" not in post["languages"]


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


async def test_create_post_with_community_id(client, seeded_db):
    resp = await client.post(
        "/posts",
        json={
            "author": "frank",
            "permlink": "community-post",
            "categories": [seeded_db["leaf_name"]],
            "languages": ["en"],
            "sentiment": "positive",
            "sentiment_score": 0.6,
            "community_id": "hive-174578",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200

    get_resp = await client.get("/posts/frank/community-post")
    assert get_resp.status_code == 200
    post = get_resp.json()
    assert post["community_id"] == "hive-174578"


async def test_post_detail_community_id_null(client, seeded_db):
    """Posts without community_id should return null."""
    resp = await client.get("/posts/alice/test-post-one")
    assert resp.status_code == 200
    assert resp.json()["community_id"] is None
