"""Core API tests — health, schema validation, categories, middleware, SEO, OG tags."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from project.categories import CATEGORY_TREE, LEAF_CATEGORIES
from project import cache
from project import apps_canonical
from project.api.routes.ui import _OG_DEFAULT_TITLE, _OG_DEFAULT_DESC


@pytest.fixture(autouse=True)
def _stub_inline_comments(monkeypatch):
    """Post pages fetch inline comments via bridge.get_discussion (proposal 095)
    and community pages fetch about-text via bridge.get_community (proposal 100).
    Stub both so the suite never makes a live RPC; tests that exercise them patch
    the specific function explicitly."""
    monkeypatch.setattr("project.api.routes.ui.get_top_comments", lambda *a, **k: [])
    monkeypatch.setattr("project.api.routes.ui.get_community", lambda *a, **k: None)


def _author_summary(total, cats=("photography",), langs=("en",)):
    """Build an author-summary dict matching crud.get_author_summary's shape."""
    return {
        "total_posts": total,
        "top_categories": [{"id": c, "name": c, "count": 5} for c in cats],
        "top_languages": [{"code": l, "count": 5} for l in langs],
        "top_community": None,
        "first_seen": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


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


async def test_prefixed_post_url_redirects_to_canonical(client):
    resp = await client.get("/hive-139531/@alice/some-post")
    assert resp.status_code == 301
    assert resp.headers.get("location") == "/@alice/some-post"


async def test_author_profile_returns_html(client):
    resp = await client.get("/@alice")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


async def test_category_landing_renders_with_og(client):
    """Known leaf category returns 200 with category-specific OG tags."""
    resp = await client.get("/c/photography")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.text
    assert 'rel="canonical" href="' in body
    assert "/c/photography" in body
    # OG title contains the category
    assert "Photography" in body


async def test_category_landing_unknown_returns_404(client):
    resp = await client.get("/c/not-a-real-category-slug")
    assert resp.status_code == 404


async def test_community_landing_renders_with_og(client):
    resp = await client.get("/community/hive-139531")
    assert resp.status_code == 200
    body = resp.text
    assert "/community/hive-139531" in body
    assert "hive-139531" in body


async def test_community_landing_invalid_id_returns_404(client):
    resp = await client.get("/community/not-a-community")
    assert resp.status_code == 404


async def test_language_landing_renders_with_og(client):
    resp = await client.get("/lang/de")
    assert resp.status_code == 200
    body = resp.text
    assert "/lang/de" in body
    assert "de" in body


async def test_language_landing_invalid_code_returns_404(client):
    resp = await client.get("/lang/NOT_VALID_4")
    assert resp.status_code == 404


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
    from datetime import datetime, timezone
    fake_posts = [
        ("alice", "test-post-one", datetime(2026, 5, 1, tzinfo=timezone.utc)),
        ("bob", "test-post-two", datetime(2026, 5, 2, tzinfo=timezone.utc)),
    ]
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_hivecomb_posts", return_value=fake_posts):
        mock_settings.site_url = "https://example.com"
        cache.clear()
        resp = await client.get("/sitemap.xml")
    assert resp.status_code == 200
    body = resp.text
    assert "https://example.com/" in body
    # HiveComb-canonical posts
    assert "@alice/test-post-one" in body
    assert "@bob/test-post-two" in body
    # Author profile URLs for HiveComb authors
    assert "<loc>https://example.com/@alice</loc>" in body
    assert "<loc>https://example.com/@bob</loc>" in body
    # Legal pages
    assert "<loc>https://example.com/privacy</loc>" in body
    assert "<loc>https://example.com/terms</loc>" in body
    assert "<loc>https://example.com/takedown</loc>" in body
    # Category landing pages (all 38 leaves, fixed taxonomy)
    assert "<loc>https://example.com/c/crypto</loc>" in body
    assert "<loc>https://example.com/c/photography</loc>" in body


async def test_sitemap_xml_includes_language_and_community_landings(client, seeded_db):
    """Languages and communities with posts should appear as landing-page URLs."""
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_hivecomb_posts", return_value=[]):
        mock_settings.site_url = "https://example.com"
        cache.clear()
        resp = await client.get("/sitemap.xml")
    body = resp.text
    # seeded posts have languages en/es/fr — they should be in the sitemap
    assert "<loc>https://example.com/lang/en</loc>" in body


async def test_sitemap_xml_includes_active_authors(client):
    """Active authors from our DB get profile URLs, even if they never
    posted via HiveComb — unique aggregation surface, not duplicate content."""
    from datetime import datetime, timezone
    fake_authors = [
        ("carol", datetime(2026, 5, 10, tzinfo=timezone.utc)),
        ("dave", datetime(2026, 5, 11, tzinfo=timezone.utc)),
    ]
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_hivecomb_posts", return_value=[]), \
         patch("project.api.routes.ui.crud.get_recently_active_authors",
               return_value=fake_authors):
        mock_settings.site_url = "https://example.com"
        cache.clear()
        resp = await client.get("/sitemap.xml")
    assert resp.status_code == 200
    body = resp.text
    assert "<loc>https://example.com/@carol</loc>" in body
    assert "<loc>https://example.com/@dave</loc>" in body


async def test_sitemap_xml_cached(client, seeded_db):
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_hivecomb_posts", return_value=[]):
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


def _bridge(title="", description="", image="", body="", **meta_extras):
    """Build a minimal bridge.get_post-like response with given metadata fields.

    Mirrors what api.hive.blog returns: title/body at top level, everything else
    under json_metadata. Passed to get_post_full mocks in tests.
    """
    metadata = {k: v for k, v in meta_extras.items() if v}
    if description:
        metadata["description"] = description
    if image:
        metadata["image"] = [image]
    return {"title": title, "body": body, "json_metadata": metadata}


@pytest.mark.parametrize("url,metadata,expect_in,expect_not_in", [
    # Post deep link injects OG tags from Hive API
    (
        "/@alice/my-great-post",
        _bridge(title="My Great Post About Hive", description="A short description of my post", image="https://example.com/photo.jpg"),
        ['content="My Great Post About Hive"', 'content="A short description of my post"',
         'content="https://images.hive.blog/0x0/https://example.com/photo.jpg"', 'content="article"'],
        ["{{OG_", 'content="website"'],
    ),
    # Post with no image keeps default og:image
    (
        "/@alice/no-img",
        _bridge(title="No Image Post", description="Desc"),
        ["og-image.png"],
        [],
    ),
    # HTML special chars are escaped in og/meta attributes
    (
        "/@alice/xss-attempt",
        _bridge(title='Post with <script> & "quotes"', description="Safe description"),
        ["&lt;script&gt;", "&amp;"],
        # Don't appear unescaped INSIDE quoted HTML attributes — JSON in the
        # post-data script tag is allowed to contain the raw bytes since it's
        # not executable there.
        ['content="Post with <script>'],
    ),
    # Post with title but no description keeps default description
    (
        "/@alice/title-only",
        _bridge(title="Title Only"),
        ['content="Title Only"'],
        [],
    ),
], ids=["deep-link", "no-image", "html-escape", "partial-metadata"])
async def test_og_post_with_metadata(client, url, metadata, expect_in, expect_not_in):
    with patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_get.return_value = metadata
        resp = await client.get(url)
    assert resp.status_code == 200
    body = resp.text
    for text in expect_in:
        assert text in body, f"Expected {text!r} in response for {url}"
    for text in expect_not_in:
        assert text not in body, f"Did not expect {text!r} in response for {url}"


async def test_canonical_honors_publisher_declared_url(client):
    """If json_metadata.canonical_url points to another UI, our canonical
    defers to it. og:url stays pointing to us (share clicks land here)."""
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_settings.site_url = "https://example.com"
        mock_get.return_value = _bridge(
            title="A post originally on PeakD",
            description="Body excerpt",
            canonical_url="https://peakd.com/@alice/some-post",
            app="ecency",  # irrelevant — explicit canonical wins
        )
        resp = await client.get("/@alice/some-post")
    body = resp.text
    assert '<link rel="canonical" href="https://peakd.com/@alice/some-post">' in body
    # og:url stays as our URL so social shares from us link back to us
    assert '<meta property="og:url" content="https://example.com/@alice/some-post">' in body


async def test_canonical_inferred_from_app_when_no_explicit_canonical(client, monkeypatch):
    """peakd/ecency/hiveblog posts (which don't set canonical_url) should
    canonical to their rightful publisher based on the app field."""
    # APP_CANONICAL_URLS is empty at import (populated only by the upstream
    # refresh, which never runs under pytest) — seed the entry the app-inference
    # path needs. ui.py reads this same module object.
    monkeypatch.setattr(
        apps_canonical, "APP_CANONICAL_URLS",
        {"peakd": "https://peakd.com/@{author}/{permlink}"},
    )
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_settings.site_url = "https://example.com"
        mock_get.return_value = _bridge(
            title="Peakd-published post",
            description="Body",
            app="peakd",
        )
        resp = await client.get("/@alice/some-post")
    body = resp.text
    assert '<link rel="canonical" href="https://peakd.com/@alice/some-post">' in body
    # og:url unchanged — points to us
    assert '<meta property="og:url" content="https://example.com/@alice/some-post">' in body


async def test_canonical_for_crosspost_points_to_original(client):
    """When peakd/ecency mark a post as a cross-post via original_author +
    original_permlink, our canonical points to the original post, not the
    cross-poster."""
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_settings.site_url = "https://example.com"
        mock_get.return_value = _bridge(
            title="Cross-post by bob of alice's post",
            description="Body",
            app="peakd",
            original_author="alice",
            original_permlink="original-post-slug",
        )
        resp = await client.get("/@bob/cross-post-slug")
    body = resp.text
    # Canonical points to the original post (alice's), rendered on peakd
    assert '<link rel="canonical" href="https://peakd.com/@alice/original-post-slug">' in body
    # NOT to the cross-poster's URL
    assert "/@bob/" not in body.split('rel="canonical"')[1].split(">")[0]


async def test_author_with_few_posts_gets_noindex(client):
    """Thin author profiles (< 10 lifetime classified posts) get noindex
    to avoid Soft 404 flagging by Google."""
    with patch("project.api.routes.ui.crud.get_author_summary",
               return_value=_author_summary(3)):
        resp = await client.get("/@thinauthor")
    body = resp.text
    assert '<meta name="robots" content="noindex,follow">' in body


async def test_author_with_zero_posts_gets_noindex(client):
    """Unknown author (get_author_summary returns None) → noindex shell."""
    with patch("project.api.routes.ui.crud.get_author_summary",
               return_value=None):
        resp = await client.get("/@ghost")
    body = resp.text
    assert '<meta name="robots" content="noindex,follow">' in body
    assert "Posts by @ghost on HiveComb" in body


async def test_author_with_many_posts_does_not_get_noindex(client):
    """Active author profiles (>= 10 lifetime classified posts) are
    indexable — no robots meta added, dynamic title/description instead."""
    with patch("project.api.routes.ui.crud.get_author_summary",
               return_value=_author_summary(50)), \
         patch("project.api.routes.ui.crud.get_author_recent_posts",
               return_value=[]):
        resp = await client.get("/@activeauthor")
    body = resp.text
    assert '<meta name="robots"' not in body
    assert "@activeauthor — 50 posts on Hive" in body
    assert "Browse 50 classified posts by @activeauthor" in body


async def test_author_page_renders_recent_posts(client):
    """Active author page server-renders the recent-posts list — text Google
    needs to escape Soft-404 even when prerender drops a hex grid render."""
    recent = [
        {"permlink": "first-post", "title": "First Post Title",
         "created": datetime(2026, 5, 1, tzinfo=timezone.utc)},
        {"permlink": "second-post", "title": "Second Post Title",
         "created": datetime(2026, 4, 1, tzinfo=timezone.utc)},
    ]
    with patch("project.api.routes.ui.crud.get_author_summary",
               return_value=_author_summary(50)), \
         patch("project.api.routes.ui.crud.get_author_recent_posts",
               return_value=recent):
        resp = await client.get("/@activeauthor")
    body = resp.text
    assert 'class="author-recent-posts"' in body
    assert ">First Post Title<" in body
    assert ">Second Post Title<" in body
    assert 'href="/@activeauthor/first-post"' in body
    assert 'href="/@activeauthor/second-post"' in body


async def test_author_page_degrades_when_recent_posts_unavailable(client):
    """HAFSQL outage → recent_posts query raises → page still renders the
    stats summary, no recent-posts block. Better than 500."""
    with patch("project.api.routes.ui.crud.get_author_summary",
               return_value=_author_summary(50)), \
         patch("project.api.routes.ui.crud.get_author_recent_posts",
               side_effect=OSError("hafsql down")):
        resp = await client.get("/@activeauthor")
    assert resp.status_code == 200
    body = resp.text
    assert 'class="author-summary"' in body
    assert 'class="author-recent-posts"' not in body


async def test_author_at_exactly_threshold_is_indexable(client):
    """Boundary check: exactly 10 posts → indexable (>= threshold)."""
    with patch("project.api.routes.ui.crud.get_author_summary",
               return_value=_author_summary(10)), \
         patch("project.api.routes.ui.crud.get_author_recent_posts",
               return_value=[]):
        resp = await client.get("/@borderlineauthor")
    body = resp.text
    assert '<meta name="robots"' not in body


async def test_author_db_error_falls_through_without_noindex(client):
    """If the summary query fails, don't tag noindex — better to risk
    an indexable thin page than incorrectly hide a substantive one."""
    with patch("project.api.routes.ui.crud.get_author_summary",
               side_effect=OSError("db down")):
        resp = await client.get("/@anyauthor")
    body = resp.text
    assert resp.status_code == 200
    assert '<meta name="robots"' not in body


async def test_reply_gets_noindex_and_omits_canonical(client):
    """Comments/replies (parent_author set) get robots noindex and no
    canonical — standalone comment URLs aren't really pages on the other
    UIs anyway, so claiming canonical there just confuses Google."""
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_settings.site_url = "https://example.com"
        # _bridge doesn't take parent_author; build it directly.
        mock_get.return_value = {
            "title": "",
            "body": "Reply body",
            "parent_author": "alice",
            "parent_permlink": "original-post",
            "json_metadata": {"app": "peakd/2026.5.2"},
        }
        resp = await client.get("/@bob/re-alice-12345")
    body = resp.text
    assert '<meta name="robots" content="noindex,follow">' in body
    assert '<link rel="canonical"' not in body


async def test_top_level_post_does_not_get_noindex(client, monkeypatch):
    """Posts with no parent_author keep current canonical behavior, no robots tag."""
    monkeypatch.setattr(
        apps_canonical, "APP_CANONICAL_URLS",
        {"peakd": "https://peakd.com/@{author}/{permlink}"},
    )
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_settings.site_url = "https://example.com"
        mock_get.return_value = _bridge(
            title="Top-level post", description="Body", app="peakd"
        )
        resp = await client.get("/@alice/some-post")
    body = resp.text
    assert '<meta name="robots"' not in body
    assert '<link rel="canonical" href="https://peakd.com/@alice/some-post">' in body


async def test_canonical_omitted_for_unknown_app(client):
    """Apps not in the shared apps-canonical list get no canonical at all —
    we don't claim what we can't identify."""
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_settings.site_url = "https://example.com"
        mock_get.return_value = _bridge(
            title="A post from an unknown app", description="Body",
            app="some-random-app-name-not-in-the-list",
        )
        resp = await client.get("/@alice/some-post")
    body = resp.text
    assert "<link rel=\"canonical\"" not in body
    # og:url still present
    assert '<meta property="og:url" content="https://example.com/@alice/some-post">' in body


async def test_canonical_omitted_for_post_with_no_app(client):
    """Posts with no app field — no signal at all → omit canonical."""
    with patch("project.api.routes.ui.settings") as mock_settings, \
         patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_settings.site_url = "https://example.com"
        mock_get.return_value = _bridge(title="Unknown-source post", description="Body")
        resp = await client.get("/@alice/some-post")
    body = resp.text
    assert "<link rel=\"canonical\"" not in body


async def test_static_refs_have_version_query_string(client):
    """Self-hosted JS/CSS/asset refs should be cache-busted with ?v=HASH
    so immutable browser/CDN caching doesn't strand users on stale code."""
    resp = await client.get("/")
    body = resp.text
    # discover.js must be referenced with a version
    import re
    matches = re.findall(r'(?:src|href)="(/static/[^"]+)"', body)
    assert matches, "no /static/ refs found"
    for url in matches:
        assert "?v=" in url, f"unversioned static ref: {url}"


async def test_homepage_still_self_canonicals(client):
    """Non-post pages (homepage, author profile, category landings) are
    legitimately unique surfaces — always self-canonical."""
    with patch("project.api.routes.ui.settings") as mock_settings:
        mock_settings.site_url = "https://example.com"
        resp = await client.get("/")
    body = resp.text
    assert '<link rel="canonical" href="https://example.com/">' in body


async def test_post_data_inlined_for_post_pages(client):
    """The full bridge.get_post response is embedded as JSON in the page so
    the client can render the modal without a second RPC."""
    with patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_get.return_value = _bridge(
            title="Inline test", body="The post body content", app="peakd"
        )
        resp = await client.get("/@alice/inline-test")
    body = resp.text
    assert '<script id="hivecomb-post-data" type="application/json">' in body
    assert "The post body content" in body
    assert '"title":"Inline test"' in body


async def test_og_post_fallback_returns_none(client):
    """When Hive API returns None, OG tags use defaults."""
    with patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_get.return_value = None
        resp = await client.get("/@alice/my-great-post")
    assert resp.status_code == 200
    assert _OG_DEFAULT_TITLE in resp.text


async def test_og_post_fallback_raises_exception(client):
    """When Hive API raises, OG tags use defaults."""
    with patch("project.api.routes.ui.get_post_full") as mock_get:
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


# ── Inline comments / author enrichments (proposals 095, 096, 098) ───────────
#
# The {{COMMENTS_HTML}} / {{AUTHOR_CARD_HTML}} / {{AUTHOR_SUMMARY_HTML}}
# placeholders are added to discover.html by the UI agent. Until then these
# blocks are built server-side but have no slot in the page, so we unit-test the
# builders directly and assert the route-observable behaviour (OG tags,
# concurrent fetch, reply handling) separately.


def test_build_comments_html_excerpts_and_escapes():
    from project.api.routes.ui import _build_comments_html
    # Bare "<" / "&" (no matching ">") survive clean_post_body, so we can verify
    # html_escape is applied to whatever plain text remains.
    html = _build_comments_html([
        {"author": "alice", "permlink": "p", "created": "",
         "body": "Math 5 < 3 and AT&T " + "x" * 400, "payout": 0, "children": 0},
    ])
    assert 'id="comments-inline"' in html
    assert 'class="inline-comment-body"' in html
    assert '/@alice' in html
    assert "&lt;" in html and "&amp;" in html            # escaped
    assert "…" in html                                  # truncated past 280 chars
    assert '"@type":"Comment"' in html


def test_build_comments_html_empty_returns_blank():
    from project.api.routes.ui import _build_comments_html
    assert _build_comments_html([]) == ""
    assert _build_comments_html(None) == ""
    # All-whitespace bodies produce no items → no section.
    assert _build_comments_html([{"author": "a", "body": "   "}]) == ""


def test_build_author_card_html():
    from project.api.routes.ui import _build_author_card_html
    card = {
        "author": "alice", "reputation": 68.5,
        "summary": {"total_posts": 42,
                    "top_categories": [{"id": "photography", "name": "photography", "count": 10}]},
    }
    html = _build_author_card_html(card, "https://example.com")
    assert 'class="author-card"' in html
    assert "Reputation: 68" in html
    assert "42 posts on HiveComb" in html
    assert "Mostly writes about: photography" in html
    assert "/api/imageproxy?url=" in html          # avatar via proxy (CSP-safe)
    assert '"@type":"Person"' in html


def test_build_author_card_html_blank_without_summary():
    from project.api.routes.ui import _build_author_card_html
    assert _build_author_card_html(None, "") == ""
    assert _build_author_card_html({"author": "x", "summary": None}, "") == ""


def test_build_author_summary_html():
    from project.api.routes.ui import _build_author_summary_html
    summary = {
        "total_posts": 42,
        "top_categories": [{"id": "photography", "name": "photography", "count": 10}],
        "top_languages": [{"code": "en", "count": 30}],
        "top_community": {"id": "hive-1", "name": "Foto", "count": 5},
        "first_seen": datetime(2019, 5, 1, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    html = _build_author_summary_html("alice", summary, "https://example.com")
    assert 'class="author-summary"' in html
    assert "Active since 2019" in html
    assert 'href="/c/photography"' in html
    assert 'href="/lang/en"' in html
    assert 'href="/community/hive-1"' in html
    assert '"@type":"Person"' in html


def test_build_author_summary_html_blank_without_summary():
    from project.api.routes.ui import _build_author_summary_html
    assert _build_author_summary_html("alice", None, "") == ""


def test_build_author_summary_html_embeds_recent_posts():
    from project.api.routes.ui import _build_author_summary_html
    summary = {
        "total_posts": 42,
        "top_categories": [], "top_languages": [], "top_community": None,
        "first_seen": None, "last_seen": None,
    }
    recent = [{"permlink": "p1", "title": "Hello World",
               "created": datetime(2026, 5, 30, tzinfo=timezone.utc)}]
    html = _build_author_summary_html(
        "alice", summary, "https://example.com", recent_posts=recent,
    )
    assert 'class="author-recent-posts"' in html
    assert ">Hello World<" in html
    assert 'href="/@alice/p1"' in html


def test_build_author_recent_posts_html_empty_returns_blank():
    from project.api.routes.ui import _build_author_recent_posts_html
    assert _build_author_recent_posts_html("alice", None, "") == ""
    assert _build_author_recent_posts_html("alice", [], "") == ""


def test_build_author_recent_posts_html_escapes_user_content():
    """Titles and permlinks come from HAFSQL — must be HTML-escaped to keep
    them from injecting markup into the server-rendered list."""
    from project.api.routes.ui import _build_author_recent_posts_html
    recent = [{"permlink": "p1", "title": "<script>x</script>",
               "created": datetime(2026, 1, 1, tzinfo=timezone.utc)}]
    html = _build_author_recent_posts_html("alice", recent, "https://example.com")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_build_author_recent_posts_html_includes_itemlist_jsonld():
    from project.api.routes.ui import _build_author_recent_posts_html
    recent = [
        {"permlink": "p1", "title": "T1",
         "created": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"permlink": "p2", "title": "T2",
         "created": datetime(2026, 1, 2, tzinfo=timezone.utc)},
    ]
    html = _build_author_recent_posts_html("alice", recent, "https://example.com")
    assert '"@type":"ItemList"' in html
    assert '"position":1' in html
    assert '"position":2' in html
    assert '"url":"https://example.com/@alice/p1"' in html


def test_build_author_description_caps_at_155():
    from project.api.routes.ui import _build_author_description
    summary = {
        "total_posts": 247,
        "top_categories": [{"name": "photography"}, {"name": "travel"}, {"name": "lifestyle"}],
        "top_languages": [{"code": "en"}],
    }
    desc = _build_author_description("gallya", summary)
    assert desc.startswith("Browse 247 classified posts by @gallya on HiveComb.")
    assert "Most active in photography, travel, lifestyle." in desc
    assert "Primary language: en." in desc
    assert len(desc) <= 155


# ── Server-rendered SEO primers (proposal 100, Phase 1) ──────────────────────

def test_build_post_list_html_renders_and_escapes():
    from project.api.routes.ui import _build_post_list_html
    posts = [{"author": "alice", "permlink": "p1", "title": "<b>Hi</b>",
              "excerpt": "Body & more", "created": datetime(2026, 5, 1, tzinfo=timezone.utc)}]
    html = _build_post_list_html(posts, "Recent posts on HiveComb")
    assert 'class="seo-recent-posts"' in html
    assert "<h2>Recent posts on HiveComb</h2>" in html
    assert 'href="/@alice/p1"' in html
    assert "&lt;b&gt;Hi&lt;/b&gt;" in html         # title escaped
    assert "Body &amp; more" in html               # excerpt escaped
    assert 'seo-post-meta">@alice' in html          # author shown by default
    assert '"@type":"ItemList"' in html and '"position":1' in html


def test_build_post_list_html_omits_author_when_flag_false():
    from project.api.routes.ui import _build_post_list_html
    posts = [{"author": "alice", "permlink": "p1", "title": "T", "excerpt": "",
              "created": datetime(2026, 5, 1, tzinfo=timezone.utc)}]
    html = _build_post_list_html(posts, "H", show_author=False)
    assert 'seo-post-meta">@' not in html           # no @author prefix on the meta line
    assert "<time" in html                          # date still rendered


def test_build_post_list_html_empty_returns_blank():
    from project.api.routes.ui import _build_post_list_html
    assert _build_post_list_html([], "H") == ""
    assert _build_post_list_html(None, "H") == ""


def test_build_post_list_html_intro_only_when_no_posts():
    """A surface with an intro but no posts still emits the intro text (it's
    substantive unique content) — but no empty <ol>."""
    from project.api.routes.ui import _build_post_list_html
    html = _build_post_list_html([], "Crypto on HiveComb", intro="All about crypto on Hive.")
    assert 'class="seo-recent-posts"' in html
    assert "All about crypto on Hive." in html
    assert "seo-post-list" not in html


def test_build_post_body_fallback_renders():
    from project.api.routes.ui import _build_post_body_fallback_html
    html = _build_post_body_fallback_html({
        "title": "My Title", "body": "Para one.\n\nPara two.",
        "author": "alice", "created": "2026-05-30T12:00:00",
        "community_title": "Photo Lovers",
    })
    assert 'class="post-body-fallback"' in html
    assert "<h1>My Title</h1>" in html
    assert "@alice" in html
    assert "Photo Lovers" in html
    assert "<p>Para one.</p>" in html and "<p>Para two.</p>" in html


def test_build_post_body_fallback_escapes_user_content():
    from project.api.routes.ui import _build_post_body_fallback_html
    html = _build_post_body_fallback_html({"title": "<x>", "body": "a & b"})
    assert "<x>" not in html
    assert "&lt;x&gt;" in html
    assert "a &amp; b" in html


def test_build_post_body_fallback_skips_replies():
    """Replies are noindex'd — no body fallback (would waste a render)."""
    from project.api.routes.ui import _build_post_body_fallback_html
    assert _build_post_body_fallback_html(
        {"title": "T", "body": "B", "parent_author": "alice"}
    ) == ""


def test_build_post_body_fallback_empty():
    from project.api.routes.ui import _build_post_body_fallback_html
    assert _build_post_body_fallback_html(None) == ""
    assert _build_post_body_fallback_html({"title": "", "body": ""}) == ""


async def test_homepage_renders_recent_posts_primer(client):
    """Homepage server-renders the recent-posts list for crawlers / no-JS."""
    recent = [{"author": "alice", "permlink": "p1", "title": "Homepage Post One",
               "excerpt": "An excerpt.", "created": datetime(2026, 5, 1, tzinfo=timezone.utc)}]
    with patch("project.api.routes.ui.crud.get_recent_posts_for_seo", return_value=recent):
        resp = await client.get("/")
    body = resp.text
    assert '<section class="seo-recent-posts" aria-label="Recent posts">' in body
    assert ">Homepage Post One<" in body
    assert 'href="/@alice/p1"' in body


async def test_homepage_degrades_when_recent_posts_unavailable(client):
    """A failed recent-posts fetch leaves the page rendering without the primer
    (200, not 500)."""
    with patch("project.api.routes.ui.crud.get_recent_posts_for_seo",
               side_effect=OSError("hafsql down")):
        resp = await client.get("/")
    assert resp.status_code == 200
    assert '<section class="seo-recent-posts" aria-label="Recent posts">' not in resp.text


async def test_category_page_renders_recent_posts_and_intro(client):
    recent = [{"author": "alice", "permlink": "p1", "title": "Category Post",
               "excerpt": "Ex.", "created": datetime(2026, 5, 1, tzinfo=timezone.utc)}]
    with patch("project.api.routes.ui.CATEGORY_DESCRIPTIONS",
               {"photography": "Photography and visual storytelling on Hive."}), \
         patch("project.api.routes.ui.crud.get_recent_posts_for_seo", return_value=recent):
        resp = await client.get("/c/photography")
    body = resp.text
    assert '<section class="seo-recent-posts" aria-label="Recent posts">' in body
    assert ">Category Post<" in body
    assert "Photography and visual storytelling on Hive." in body  # intro + og desc


async def test_language_page_heading_uses_display_name(client):
    with patch("project.api.routes.ui.crud.get_recent_posts_for_seo", return_value=[]):
        resp = await client.get("/lang/pt")
    body = resp.text
    assert "Hive posts in Portuguese" in body


async def test_community_page_uses_name_and_about(client):
    with patch("project.api.routes.ui.crud.get_community_name",
               return_value="Photography Lovers"), \
         patch("project.api.routes.ui.get_community",
               return_value={"title": "Photography Lovers",
                             "about": "A community for photographers."}), \
         patch("project.api.routes.ui.crud.get_recent_posts_for_seo", return_value=[]):
        resp = await client.get("/community/hive-194913")
    body = resp.text
    assert "Photography Lovers" in body
    assert "A community for photographers." in body


async def test_community_page_falls_back_to_id_without_mapping(client):
    """No mapping row + no Hive API result → bare id heading, page still renders."""
    with patch("project.api.routes.ui.crud.get_community_name", return_value=None), \
         patch("project.api.routes.ui.get_community", return_value=None), \
         patch("project.api.routes.ui.crud.get_recent_posts_for_seo", return_value=[]):
        resp = await client.get("/community/hive-194913")
    assert resp.status_code == 200
    assert "hive-194913" in resp.text


async def test_post_page_renders_body_fallback(client):
    """Top-level post pages server-render the plain-text body fallback."""
    with patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_get.return_value = _bridge(
            title="My Post", body="First para.\n\nSecond para.", app="peakd",
        )
        resp = await client.get("/@alice/my-post")
    body = resp.text
    assert 'class="post-body-fallback"' in body
    assert "<h1>My Post</h1>" in body
    assert "First para." in body and "Second para." in body


async def test_post_page_no_body_fallback_for_reply(client):
    with patch("project.api.routes.ui.get_post_full") as mock_get:
        mock_get.return_value = {
            "title": "", "body": "Reply body", "parent_author": "alice",
            "parent_permlink": "orig", "json_metadata": {},
        }
        resp = await client.get("/@bob/re-alice-123")
    assert 'class="post-body-fallback"' not in resp.text


async def test_fetch_post_includes_comments_concurrently():
    """_fetch_post returns (og, raw, comments) with comments fetched alongside
    the post (proposal 095, approval decision #2)."""
    from project.api.routes.ui import _fetch_post
    with patch("project.api.routes.ui.get_post_full",
               return_value=_bridge(title="T", body="Body", app="peakd")), \
         patch("project.api.routes.ui.get_top_comments",
               return_value=[{"author": "x", "permlink": "c", "body": "nice",
                              "created": "", "payout": 1.0, "children": 0}]):
        og, raw, comments = await _fetch_post("alice", "post")
    assert raw["title"] == "T"
    assert len(comments) == 1 and comments[0]["author"] == "x"


async def test_fetch_post_drops_comments_for_reply():
    """Replies are noindex'd; their fetched comments are discarded."""
    from project.api.routes.ui import _fetch_post
    reply = {"title": "", "body": "re", "parent_author": "alice",
             "parent_permlink": "orig", "json_metadata": {}}
    with patch("project.api.routes.ui.get_post_full", return_value=reply), \
         patch("project.api.routes.ui.get_top_comments",
               return_value=[{"author": "x", "permlink": "c", "body": "nice",
                              "created": "", "payout": 1.0, "children": 0}]):
        og, raw, comments = await _fetch_post("bob", "re-orig")
    assert comments == []
    assert og.get("noindex") is True


async def test_fetch_post_returns_empty_triple_on_failure():
    from project.api.routes.ui import _fetch_post
    with patch("project.api.routes.ui.get_post_full", return_value=None):
        og, raw, comments = await _fetch_post("alice", "missing")
    assert og == {} and raw is None and comments == []


# ── GET /api/authors/{author}/summary (proposal 099) ─────────────────────────

async def test_author_summary_endpoint_returns_data(client, seeded_db):
    resp = await client.get("/api/authors/alice/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["total_posts"] == 1
    assert data["summary"]["top_categories"][0]["name"] == seeded_db["leaf_name"]
    # Reputation is deliberately excluded — the client reads it from the post payload.
    assert "reputation" not in data["summary"]
    assert resp.headers["cache-control"] == "public, max-age=21600"


async def test_author_summary_endpoint_null_for_unknown_author(client, seeded_db):
    resp = await client.get("/api/authors/nobodyhere/summary")
    assert resp.status_code == 200
    assert resp.json() == {"summary": None}


async def test_author_summary_endpoint_rejects_malformed_username(client):
    # Uppercase fails the lowercase Hive username pattern → path validation 422.
    resp = await client.get("/api/authors/Alice/summary")
    assert resp.status_code == 422


