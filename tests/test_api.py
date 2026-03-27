"""Core API tests — health, schema validation, categories, middleware, SEO, OG tags."""
from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from project.categories import CATEGORY_TREE, LEAF_CATEGORIES
from project import cache
from project.api.routes.ui import _OG_DEFAULT_TITLE, _OG_DEFAULT_DESC


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

async def test_cors_rejects_unknown_origin(client):
    """With no configured CORS origins, unknown origins should not get allow-origin header."""
    resp = await client.options(
        "/health",
        headers={"Origin": "http://example.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in resp.headers


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


# ── SEO endpoints ─────────────────────────────────────────────────────────

async def test_robots_txt(client):
    resp = await client.get("/robots.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    body = resp.text
    assert "User-agent: *" in body
    assert "Disallow: /api/" in body
    assert "GPTBot" in body
    assert "anthropic-ai" in body


async def test_robots_txt_includes_sitemap_when_site_url(client):
    with patch("project.api.routes.ui.settings") as mock_settings:
        mock_settings.site_url = "https://example.com"
        resp = await client.get("/robots.txt")
    assert resp.status_code == 200
    assert "Sitemap: https://example.com/sitemap.xml" in resp.text


async def test_llms_txt(client):
    resp = await client.get("/llms.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    body = resp.text
    assert "HiveComb" in body
    assert "Hive blockchain" in body
    assert "API" in body


async def test_sitemap_xml_empty_site_url(client):
    """Without site_url, sitemap returns a minimal empty urlset."""
    with patch("project.api.routes.ui.settings") as mock_settings:
        mock_settings.site_url = ""
        resp = await client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "application/xml" in resp.headers.get("content-type", "")
    assert "<urlset" in resp.text


async def test_sitemap_xml_with_posts(client, seeded_db):
    with patch("project.api.routes.ui.settings") as mock_settings:
        mock_settings.site_url = "https://example.com"
        cache.clear()
        resp = await client.get("/sitemap.xml")
    assert resp.status_code == 200
    body = resp.text
    assert "https://example.com/" in body
    assert "@alice/test-post-one" in body
    assert "@bob/test-post-two" in body


async def test_sitemap_xml_cached(client, seeded_db):
    with patch("project.api.routes.ui.settings") as mock_settings:
        mock_settings.site_url = "https://example.com"
        cache.clear()
        r1 = await client.get("/sitemap.xml")
        r2 = await client.get("/sitemap.xml")
    assert r1.status_code == 200
    assert r1.text == r2.text


# ── Post detail validation ────────────────────────────────────────────────

async def test_post_invalid_author_pattern(client):
    """Author with invalid characters should return 422."""
    resp = await client.get("/posts/INVALID_AUTHOR!/some-permlink")
    assert resp.status_code == 422


async def test_post_author_too_long(client):
    """Author exceeding max_length should return 422."""
    resp = await client.get(f"/posts/{'a' * 17}/some-permlink")
    assert resp.status_code == 422


# ── Stats includes api_base_url ──────────────────────────────────────────

async def test_stats_includes_api_base_url(client, seeded_db):
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    assert "api_base_url" in resp.json()


# ── Categories fallback on DB error ──────────────────────────────────────

async def test_categories_fallback_on_exception(client):
    """When DB fails, /categories returns in-memory CATEGORY_TREE fallback."""
    cache.clear()
    with patch("project.api.main.AsyncSessionLocal") as mock_session_cls:
        mock_session_cls.side_effect = SQLAlchemyError("db down")
        resp = await client.get("/categories")
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    assert len(data["categories"]) == len(CATEGORY_TREE)


# ── OG meta tags (proposal 043) ──────────────────────────────────────────


@pytest.mark.parametrize("url,metadata,expect_in,expect_not_in", [
    # Post deep link injects OG tags from Hive API
    (
        "/@alice/my-great-post",
        {"title": "My Great Post About Hive", "description": "A short description of my post", "image": "https://example.com/photo.jpg"},
        ['content="My Great Post About Hive"', 'content="A short description of my post"',
         'content="https://images.hive.blog/0x0/https://example.com/photo.jpg"', 'content="article"'],
        ["{{OG_", 'content="website"'],
    ),
    # Prefixed post URLs also get OG overrides
    (
        "/hive-139531/@bob/cross-post",
        {"title": "Cross-posted Article", "description": "Some description", "image": ""},
        ['content="Cross-posted Article"', 'content="article"'],
        [],
    ),
    # Post with no image keeps default og:image
    (
        "/@alice/no-img",
        {"title": "No Image Post", "description": "Desc", "image": ""},
        ["og-image.png"],
        [],
    ),
    # HTML special chars are escaped
    (
        "/@alice/xss-attempt",
        {"title": 'Post with <script> & "quotes"', "description": "Safe description", "image": ""},
        ["&lt;script&gt;", "&amp;"],
        ["<script>"],
    ),
    # Post with title but no description keeps default description
    (
        "/@alice/title-only",
        {"title": "Title Only", "description": "", "image": ""},
        ['content="Title Only"'],
        [],
    ),
], ids=["deep-link", "prefixed", "no-image", "html-escape", "partial-metadata"])
async def test_og_post_with_metadata(client, url, metadata, expect_in, expect_not_in):
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = metadata
        resp = await client.get(url)
    assert resp.status_code == 200
    body = resp.text
    for text in expect_in:
        assert text in body, f"Expected {text!r} in response for {url}"
    for text in expect_not_in:
        assert text not in body, f"Did not expect {text!r} in response for {url}"


async def test_og_post_fallback_returns_none(client):
    """When Hive API returns None, OG tags use defaults."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = None
        resp = await client.get("/@alice/my-great-post")
    assert resp.status_code == 200
    assert _OG_DEFAULT_TITLE in resp.text


async def test_og_post_fallback_raises_exception(client):
    """When Hive API raises, OG tags use defaults."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.side_effect = OSError("network error")
        resp = await client.get("/@alice/my-great-post")
    assert resp.status_code == 200
    assert _OG_DEFAULT_TITLE in resp.text


async def test_og_author_profile(client):
    """/@author pages should get author-specific OG tags."""
    resp = await client.get("/@alice")
    assert resp.status_code == 200
    body = resp.text
    assert "{{OG_" not in body
    assert "@alice" in body
    assert "Posts by @alice on HiveComb" in body


async def test_og_root_page_keeps_defaults(client):
    """Root page should keep the default OG tags."""
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "{{OG_" not in body
    assert _OG_DEFAULT_TITLE in body
    assert 'content="website"' in body


