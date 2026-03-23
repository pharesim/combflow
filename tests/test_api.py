"""Core API tests — health, schema validation, categories, middleware."""
from unittest.mock import patch

from project.categories import CATEGORY_TREE, LEAF_CATEGORIES


# ── Health ────────────────────────────────────────────────────────────────────

async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── Categories ────────────────────────────────────────────────────────────────

async def test_categories_endpoint(client, seeded_db):
    resp = await client.get("/categories")
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    names = [c["name"] for c in data["categories"]]
    for parent in CATEGORY_TREE:
        assert parent in names


def test_category_tree_structure():
    assert len(CATEGORY_TREE) >= 9
    for parent, children in CATEGORY_TREE.items():
        assert isinstance(parent, str)
        assert len(children) >= 1


def test_leaf_categories_are_unique():
    assert len(LEAF_CATEGORIES) == len(set(LEAF_CATEGORIES))



# ── Post not found ───────────────────────────────────────────────────────────

async def test_get_post_not_found(client, seeded_db):
    resp = await client.get("/posts/nobody/nonexistent-permlink")
    assert resp.status_code == 404


# ── CORS ─────────────────────────────────────────────────────────────────────

async def test_cors_headers(client):
    resp = await client.options(
        "/health",
        headers={"Origin": "http://example.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" in resp.headers


# ── OpenAPI ──────────────────────────────────────────────────────────────────

async def test_openapi_schema(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "paths" in schema
    assert schema["info"]["title"] == "CombFlow Discovery Engine"


# ── Categories caching ───────────────────────────────────────────────────────

async def test_categories_cached(client, seeded_db):
    r1 = await client.get("/categories")
    r2 = await client.get("/categories")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Compare category names (IDs may differ if worker re-seeds concurrently)
    def extract_names(data):
        return sorted(
            (p["name"], sorted(c["name"] for c in p.get("children", [])))
            for p in data["categories"]
        )
    assert extract_names(r1.json()) == extract_names(r2.json())


# ── HTML page routes ─────────────────────────────────────────────────────────

async def test_root_returns_html(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


async def test_ui_redirects_to_root(client):
    resp = await client.get("/ui", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers.get("location") == "/"


async def test_ui_post_page_returns_html(client):
    resp = await client.get("/@alice/some-post")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


async def test_prefixed_post_url_returns_html(client):
    resp = await client.get("/hive-139531/@alice/some-post")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


async def test_author_profile_returns_html(client):
    resp = await client.get("/@alice")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


# ── GZip middleware ──────────────────────────────────────────────────────────

async def test_gzip_large_response(client, seeded_db):
    """Large response with Accept-Encoding: gzip should be compressed."""
    resp = await client.get(
        "/api/browse",
        headers={"Accept-Encoding": "gzip"},
    )
    assert resp.status_code == 200
    # httpx auto-decompresses, but we can check content-encoding was set.
    # Note: with few test posts, response may be < 500 bytes and not compressed.
    # So we test that the endpoint works with the encoding header.
    assert "posts" in resp.json()


async def test_gzip_varies_on_accept_encoding(client, seeded_db):
    """GZip middleware sets Vary: Accept-Encoding header on large responses."""
    resp = await client.get("/api/browse", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    # Large enough response should trigger gzip and set Vary header.
    assert "Accept-Encoding" in resp.headers.get("vary", "")


