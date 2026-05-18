"""UI routes — serves the discovery page and supporting API endpoints."""
import asyncio
import logging
import pathlib
import re
from datetime import datetime, timezone
from html import escape as html_escape
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ... import cache
from ...categories import LEAF_CATEGORIES
from ...config import settings
from ...db import crud
from ...hafsql import get_hivecomb_posts, get_post_metadata
from ..deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = {
    name: (_TEMPLATE_DIR / name).read_text()
    for name in ["discover.html", "privacy.html", "terms.html", "takedown.html"]
}

# Default OG values — must match what's in discover.html.
_OG_DEFAULT_TITLE = "HiveComb \u2014 Discover Hive Blockchain Content"
_OG_DEFAULT_DESC = (
    "Discover and explore Hive blockchain posts by topic, language, and sentiment. "
    "Browse communities, filter by category, and find content that matches your interests."
)


def _render_legal(name: str) -> HTMLResponse:
    """Return a legal page template with placeholders replaced."""
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    html = (
        _TEMPLATES[name]
        .replace("{{SITE_URL}}", site_url)
        .replace("{{LEGAL_DATE}}", "12 April 2026")
    )
    return HTMLResponse(html)


def _render(name: str, request: Request, og: dict | None = None) -> HTMLResponse:
    """Return an HTML template with placeholders replaced.

    Replaces {{SITE_URL}}, {{CANONICAL_URL}}, and {{OG_*}} placeholders.
    OG placeholders fall back to defaults when no overrides are provided.
    If og["canonical"] is set and points away from our site, it is used as
    the canonical/og:url — honoring the publisher's json_metadata.canonical_url.
    """
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    default_image = f"{site_url}/static/og-image.png"

    # Canonical: honor publisher-declared canonical when it points off-site;
    # otherwise self-canonical to our own URL.
    own_url = site_url + request.url.path
    external_canonical = (og or {}).get("canonical") if og else None
    if external_canonical and site_url and not external_canonical.startswith(site_url):
        canonical_url = html_escape(external_canonical)
    else:
        canonical_url = html_escape(own_url)

    og_type = html_escape(og["type"]) if og and "type" in og else "website"
    og_title = html_escape(og["title"]) if og and "title" in og else _OG_DEFAULT_TITLE
    og_desc = html_escape(og["description"]) if og and "description" in og else _OG_DEFAULT_DESC
    og_image = html_escape(og["image"]) if og and "image" in og else default_image

    html = (
        _TEMPLATES[name]
        .replace("{{SITE_URL}}", site_url)
        .replace("{{CANONICAL_URL}}", canonical_url)
        .replace("{{OG_TYPE}}", og_type)
        .replace("{{OG_TITLE}}", og_title)
        .replace("{{OG_DESCRIPTION}}", og_desc)
        .replace("{{OG_IMAGE}}", og_image)
    )
    return HTMLResponse(html)


async def _fetch_post_og(author: str, permlink: str) -> dict:
    """Fetch OG overrides for a post from Hive API. Returns {} on failure."""
    try:
        meta = await asyncio.to_thread(get_post_metadata, author, permlink)
        if not meta:
            return {}
        og: dict = {"type": "article"}
        if meta["title"]:
            og["title"] = meta["title"]
        if meta["description"]:
            og["description"] = meta["description"]
        if meta["image"]:
            og["image"] = f"https://images.hive.blog/0x0/{meta['image']}"
        if meta.get("canonical_url"):
            og["canonical"] = meta["canonical_url"]
        return og
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug("OG fetch failed for %s/%s: %s", author, permlink, exc)
        return {}


# ── HTML pages ────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def root(request: Request):
    return _render("discover.html", request)


@router.get("/ui", include_in_schema=False)
async def discover_page_redirect():
    return RedirectResponse("/", status_code=301)


_LEAF_CATEGORY_SET = set(LEAF_CATEGORIES)
_COMMUNITY_RE = re.compile(r"^hive-\d{6,}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}$")


@router.get("/c/{category}", include_in_schema=False)
async def discover_category(request: Request, category: str):
    if category not in _LEAF_CATEGORY_SET:
        raise HTTPException(status_code=404, detail="Unknown category")
    pretty = category.replace("-", " ").title()
    og = {
        "title": f"{pretty} — Hive posts on HiveComb",
        "description": f"Discover Hive blockchain posts about {category.replace('-', ' ')}, classified by topic, language, and sentiment.",
    }
    return _render("discover.html", request, og=og)


@router.get("/community/{community_id}", include_in_schema=False)
async def discover_community(request: Request, community_id: str):
    if not _COMMUNITY_RE.match(community_id):
        raise HTTPException(status_code=404, detail="Invalid community id")
    og = {
        "title": f"{community_id} — Hive community on HiveComb",
        "description": f"Posts from the {community_id} Hive community on HiveComb.",
    }
    return _render("discover.html", request, og=og)


@router.get("/lang/{lang}", include_in_schema=False)
async def discover_language(request: Request, lang: str):
    if not _LANG_RE.match(lang):
        raise HTTPException(status_code=404, detail="Invalid language code")
    og = {
        "title": f"Hive posts in {lang} — HiveComb",
        "description": f"Discover Hive blockchain posts written in {lang}.",
    }
    return _render("discover.html", request, og=og)


@router.get("/{prefix}/@{author}/{permlink}", include_in_schema=False)
async def discover_prefixed_post(prefix: str, author: str, permlink: str):
    return RedirectResponse(f"/@{author}/{permlink}", status_code=301)


@router.get("/@{author}", include_in_schema=False)
async def discover_author(request: Request, author: str):
    og = {
        "title": f"@{author} \u2014 HiveComb",
        "description": f"Posts by @{author} on HiveComb",
    }
    return _render("discover.html", request, og=og)


@router.get("/@{author}/{permlink}", include_in_schema=False)
async def discover_post(request: Request, author: str, permlink: str):
    og = await _fetch_post_og(author, permlink)
    return _render("discover.html", request, og=og)


# ── Legal pages ──────────────────────────────────────────────────────────────

@router.get("/privacy", include_in_schema=False)
async def privacy_page():
    return _render_legal("privacy.html")


@router.get("/terms", include_in_schema=False)
async def terms_page():
    return _render_legal("terms.html")


@router.get("/takedown", include_in_schema=False)
async def takedown_page():
    return _render_legal("takedown.html")


# ── SEO ──────────────────────────────────────────────────────────────────────

@router.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    sitemap_line = f"\nSitemap: {site_url}/sitemap.xml" if site_url else ""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /health\n"
        "Disallow: /docs\n"
        "\n"
        "# AI crawlers\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "\n"
        "User-agent: anthropic-ai\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "\n"
        "User-agent: CCBot\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        f"{sitemap_line}\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


_SITEMAP_TTL = 86400  # 24h — sitemap data changes slowly; lifespan pre-warms on boot


async def _build_sitemap_xml(db: AsyncSession, site_url: str) -> str:
    """Build the sitemap XML body. Expensive (HAFSQL JSONB scan, language
    unnest). Cached for _SITEMAP_TTL after the first build."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    urls = [
        f"  <url><loc>{xml_escape(site_url)}/</loc>"
        f"<lastmod>{now}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>"
    ]

    for legal in ("privacy", "terms", "takedown"):
        urls.append(
            f"  <url><loc>{xml_escape(site_url)}/{legal}</loc>"
            f"<lastmod>{now}</lastmod><changefreq>yearly</changefreq><priority>0.3</priority></url>"
        )

    # Category landing pages — fixed taxonomy, all 38 leaves are valid surfaces.
    for cat in LEAF_CATEGORIES:
        urls.append(
            f"  <url><loc>{xml_escape(site_url)}/c/{xml_escape(cat)}</loc>"
            f"<lastmod>{now}</lastmod><changefreq>daily</changefreq><priority>0.6</priority></url>"
        )

    # Language landing pages — only languages with actual posts.
    try:
        langs = await crud.get_available_languages(db)
        for entry in langs[:100]:
            code = entry.get("language")
            if code and _LANG_RE.match(code):
                urls.append(
                    f"  <url><loc>{xml_escape(site_url)}/lang/{xml_escape(code)}</loc>"
                    f"<lastmod>{now}</lastmod><changefreq>daily</changefreq><priority>0.5</priority></url>"
                )
    except Exception as exc:
        logger.warning("sitemap language enumeration failed: %s", exc)

    # Community landing pages — top by post count.
    try:
        comms = await crud.get_available_communities(db)
        for entry in comms[:500]:
            cid = entry.get("id")
            if cid and _COMMUNITY_RE.match(cid):
                urls.append(
                    f"  <url><loc>{xml_escape(site_url)}/community/{xml_escape(cid)}</loc>"
                    f"<lastmod>{now}</lastmod><changefreq>daily</changefreq><priority>0.5</priority></url>"
                )
    except Exception as exc:
        logger.warning("sitemap community enumeration failed: %s", exc)

    # Only list posts where HiveComb is the rightful canonical (posted via HiveComb).
    # For posts originally published on PeakD/Ecency/etc., Google would mark our
    # copies as "Duplicate, Google chose different canonical" — wasted crawl budget
    # and a low-quality signal on our sitemap.
    posts = await asyncio.to_thread(get_hivecomb_posts, 1000)
    author_lastmod: dict[str, str] = {}
    for author, permlink, created in posts:
        lastmod = created.strftime("%Y-%m-%d") if created else now
        loc = f"{site_url}/@{xml_escape(author)}/{xml_escape(permlink)}"
        urls.append(
            f"  <url><loc>{loc}</loc>"
            f"<lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )
        author_lastmod[author] = max(author_lastmod.get(author, ""), lastmod)

    # Author profile pages: unique aggregation surface, not duplicate content.
    # Include recently active authors regardless of which UI they post from.
    active_authors = await crud.get_recently_active_authors(db, days=60, limit=1000)
    for author, last_created in active_authors:
        lastmod = last_created.strftime("%Y-%m-%d") if last_created else now
        author_lastmod[author] = max(author_lastmod.get(author, ""), lastmod)

    for author in sorted(author_lastmod):
        loc = f"{site_url}/@{xml_escape(author)}"
        urls.append(
            f"  <url><loc>{loc}</loc>"
            f"<lastmod>{author_lastmod[author]}</lastmod><changefreq>daily</changefreq><priority>0.6</priority></url>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n"
        '</urlset>\n'
    )


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(db: AsyncSession = Depends(get_db)):
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    if not site_url:
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>',
            media_type="application/xml",
        )
    cached = cache.get("sitemap_xml")
    if cached is not None:
        return Response(content=cached, media_type="application/xml")
    xml = await _build_sitemap_xml(db, site_url)
    cache.put("sitemap_xml", xml, ttl=_SITEMAP_TTL)
    return Response(content=xml, media_type="application/xml")


async def warm_sitemap_cache(session_factory) -> None:
    """Pre-build and cache the sitemap. Call from lifespan so the slow
    first build happens during startup, not when Googlebot is waiting."""
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    if not site_url:
        return
    try:
        async with session_factory() as session:
            xml = await _build_sitemap_xml(session, site_url)
        cache.put("sitemap_xml", xml, ttl=_SITEMAP_TTL)
        logger.info("sitemap cache warmed (%d bytes)", len(xml))
    except Exception as exc:
        logger.warning("sitemap warm failed: %s", exc)


async def periodic_sitemap_warm(session_factory, interval: int = _SITEMAP_TTL // 2) -> None:
    """Warm immediately, then re-warm every `interval` seconds (default 12h).
    Cache TTL is 24h so the entry is always at least 12h fresh when re-warmed —
    no real request ever hits a cold cache. Runs until the task is cancelled."""
    while True:
        await warm_sitemap_cache(session_factory)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


@router.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    body = (
        "# HiveComb\n"
        "\n"
        "> Semantic post discovery for the Hive blockchain.\n"
        "\n"
        "HiveComb is a content discovery and publishing interface for the Hive blockchain.\n"
        "It streams posts, classifies them by topic, language, and sentiment using\n"
        "embedding-based cosine similarity, and lets users browse, filter, and publish\n"
        "posts across Hive communities.\n"
        "\n"
        "## Features\n"
        "- Browse posts filtered by category, language, sentiment, and community\n"
        "- Hierarchical comment threads\n"
        "- Community discovery and joining\n"
        "- Post and comment creation via Hive Keychain (client-side signing)\n"
        "- Three view modes: hex grid, card grid, and list\n"
        "\n"
        "## Pages\n"
        f"- Homepage: {site_url}/\n"
        f"- Author pages: {site_url}/@{{username}}\n"
        f"- Post pages: {site_url}/@{{author}}/{{permlink}}\n"
        "\n"
        "## API\n"
        f"- Browse posts: {site_url}/api/browse?category=&language=&sentiment=\n"
        f"- Categories: {site_url}/categories\n"
        f"- Communities: {site_url}/api/communities\n"
        f"- Languages: {site_url}/api/languages\n"
        f"- API docs: {site_url}/docs\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


# ── Browse API ────────────────────────────────────────────────────────────────

@router.get("/api/browse", tags=["discovery"], summary="Browse posts with filters")
async def browse_posts(
    db: AsyncSession = Depends(get_db),
    category: list[str] | None = Query(default=None),
    language: list[str] | None = Query(default=None),
    sentiment: str | None = Query(default=None, pattern=r"^(positive|negative|neutral)$"),
    community: str | None = Query(default=None, description="Filter by Hive community ID (e.g. hive-174578)"),
    communities: list[str] | None = Query(default=None, description="Filter by multiple community IDs; overrides community"),
    authors: list[str] | None = Query(default=None, description="Filter by author usernames"),
    include_nsfw: bool = Query(default=False, description="Include NSFW-tagged posts"),
    nsfw_only: bool = Query(default=False, description="Show only NSFW-tagged posts"),
    max_age: str | None = Query(default=None, description="Max post age, e.g. '6h', '1d', '7d'", pattern=r"^\d+[hd]$"),
    sort: str | None = Query(default=None, description="Sort order: 'newest' (default) or 'oldest'", pattern=r"^(newest|oldest)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2000),
    cursor: str | None = Query(default=None, description="Opaque cursor from previous response for keyset pagination"),
):
    # Bound filter list lengths (proposal 033).
    if category:
        category = category[:50]
    if language:
        language = language[:100]
    if communities:
        communities = communities[:200]
    if authors:
        authors = authors[:3000]

    result = await crud.browse_posts(
        db, categories=category, languages=language,
        sentiment=sentiment,
        community=None if communities else community,
        communities=communities,
        authors=authors,
        limit=limit, offset=offset, cursor=cursor,
        include_nsfw=include_nsfw, nsfw_only=nsfw_only,
        max_age=max_age, sort=sort,
    )
    return {"posts": result["posts"], "count": len(result["posts"]), "total": result["total"], "next_cursor": result["next_cursor"]}


class BrowseRequest(BaseModel):
    category: list[str] | None = None
    language: list[str] | None = None
    sentiment: str | None = Field(default=None, pattern=r"^(positive|negative|neutral)$")
    community: str | None = None
    communities: list[str] | None = None
    authors: list[str] | None = None
    include_nsfw: bool = False
    nsfw_only: bool = False
    max_age: str | None = Field(default=None, pattern=r"^\d+[hd]$")
    sort: str | None = Field(default=None, pattern=r"^(newest|oldest)$")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=2000)
    cursor: str | None = None


@router.post("/api/browse", tags=["discovery"], summary="Browse posts with filters (POST)")
async def browse_posts_post(
    body: BrowseRequest,
    db: AsyncSession = Depends(get_db),
):
    category = body.category[:50] if body.category else None
    language = body.language[:100] if body.language else None
    communities = body.communities[:200] if body.communities else None
    authors = body.authors[:3000] if body.authors else None

    result = await crud.browse_posts(
        db, categories=category, languages=language,
        sentiment=body.sentiment,
        community=None if communities else body.community,
        communities=communities,
        authors=authors,
        limit=body.limit, offset=body.offset, cursor=body.cursor,
        include_nsfw=body.include_nsfw, nsfw_only=body.nsfw_only,
        max_age=body.max_age, sort=body.sort,
    )
    return {"posts": result["posts"], "count": len(result["posts"]), "total": result["total"], "next_cursor": result["next_cursor"]}


@router.get("/api/languages", tags=["discovery"], summary="Available languages")
@cache.cached_response("languages", ttl=3600)
async def available_languages(db: AsyncSession = Depends(get_db)):
    return {"languages": await crud.get_available_languages(db)}


@router.get("/api/communities", tags=["discovery"], summary="Communities with post counts")
@cache.cached_response("communities", ttl=300)
async def available_communities(db: AsyncSession = Depends(get_db)):
    rows = await crud.get_available_communities(db)
    return {"communities": rows}


@router.get("/api/communities/suggested", tags=["discovery"], summary="Suggested communities for given categories")
async def suggested_communities(
    db: AsyncSession = Depends(get_db),
    category: list[str] = Query(..., description="Category slugs to match against community mappings"),
):
    sorted_cats = tuple(sorted(category))
    cache_key = f"comm_suggested:{'|'.join(sorted_cats)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    rows = await crud.get_suggested_communities(db, list(sorted_cats))
    result = {"suggestions": rows}
    cache.put(cache_key, result, ttl=300)
    return result


@router.get("/api/stats", tags=["discovery"], summary="Overview statistics")
@cache.cached_response("overview_stats", ttl=300)
async def overview_stats(db: AsyncSession = Depends(get_db)):
    result = await crud.get_overview_stats(db)
    result["api_base_url"] = settings.api_base_url
    return result
