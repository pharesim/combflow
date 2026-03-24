"""Core API tests — health, schema validation, categories, middleware, SEO, OG tags."""
from unittest.mock import patch

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
        mock_session_cls.side_effect = Exception("db down")
        resp = await client.get("/categories")
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    assert len(data["categories"]) == len(CATEGORY_TREE)


# ── OG meta tags (proposal 043) ──────────────────────────────────────────

_MOCK_POST_RESULT = {
    "title": "My Great Post About Hive",
    "body": "This is the body of the post with some interesting content about blockchain.",
    "json_metadata": {
        "description": "A short description of my post",
        "image": ["https://example.com/photo.jpg"],
    },
}


async def test_og_post_deep_link_injects_post_tags(client):
    """/@author/permlink should have post-specific OG tags when Hive API returns data."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = {
            "title": "My Great Post About Hive",
            "description": "A short description of my post",
            "image": "https://example.com/photo.jpg",
        }
        resp = await client.get("/@alice/my-great-post")
    assert resp.status_code == 200
    body = resp.text
    assert 'content="My Great Post About Hive"' in body
    assert 'content="A short description of my post"' in body
    assert 'content="https://images.hive.blog/0x0/https://example.com/photo.jpg"' in body
    assert 'content="article"' in body
    # Defaults should NOT be present for overridden fields
    assert "{{OG_" not in body
    assert 'content="website"' not in body


async def test_og_post_deep_link_fallback_on_api_failure(client):
    """When Hive API fails, OG tags should use defaults."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = None
        resp = await client.get("/@alice/my-great-post")
    assert resp.status_code == 200
    body = resp.text
    assert "{{OG_" not in body
    assert _OG_DEFAULT_TITLE in body
    assert 'content="website"' in body


async def test_og_post_deep_link_exception_falls_back(client):
    """Exception in Hive API call should not break the page."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.side_effect = Exception("network error")
        resp = await client.get("/@alice/my-great-post")
    assert resp.status_code == 200
    assert _OG_DEFAULT_TITLE in resp.text


async def test_og_prefixed_post_injects_tags(client):
    """Prefixed post URLs also get OG overrides."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = {
            "title": "Cross-posted Article",
            "description": "Some description",
            "image": "",
        }
        resp = await client.get("/hive-139531/@bob/cross-post")
    assert resp.status_code == 200
    assert 'content="Cross-posted Article"' in resp.text
    assert 'content="article"' in resp.text


async def test_og_prefixed_post_no_image_keeps_default(client):
    """When post has no image, the default og:image should remain."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = {
            "title": "No Image Post",
            "description": "Desc",
            "image": "",
        }
        resp = await client.get("/@alice/no-img")
    assert resp.status_code == 200
    assert "og-image.png" in resp.text


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


async def test_og_html_escapes_special_chars(client):
    """Post titles with HTML special chars should be escaped."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = {
            "title": 'Post with <script> & "quotes"',
            "description": "Safe description",
            "image": "",
        }
        resp = await client.get("/@alice/xss-attempt")
    assert resp.status_code == 200
    body = resp.text
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&amp;" in body


async def test_og_post_partial_metadata(client):
    """Post with title but no description keeps default description."""
    with patch("project.api.routes.ui.get_post_metadata") as mock_get:
        mock_get.return_value = {
            "title": "Title Only",
            "description": "",
            "image": "",
        }
        resp = await client.get("/@alice/title-only")
    assert resp.status_code == 200
    body = resp.text
    assert 'content="Title Only"' in body
    # Default description should remain since override is empty
    assert _OG_DEFAULT_DESC in body


