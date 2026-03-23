"""UI routes — serves the discovery page and supporting API endpoints."""
import pathlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ... import cache
from ...config import settings
from ...db import crud
from ..deps import get_db

router = APIRouter()

_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
_TEMPLATES = {
    name: (_TEMPLATE_DIR / name).read_text()
    for name in ["discover.html"]
}


def _render(name: str, request: Request) -> HTMLResponse:
    """Return an HTML template with {{SITE_URL}} and {{CANONICAL_PATH}} replaced."""
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    canonical_path = request.url.path
    html = (
        _TEMPLATES[name]
        .replace("{{SITE_URL}}", site_url)
        .replace("{{CANONICAL_PATH}}", canonical_path)
    )
    return HTMLResponse(html)


# ── HTML pages ────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def root(request: Request):
    return _render("discover.html", request)


@router.get("/ui", include_in_schema=False)
async def discover_page_redirect():
    return RedirectResponse("/", status_code=301)


@router.get("/{prefix}/@{author}/{permlink}", include_in_schema=False)
async def discover_prefixed_post(request: Request, prefix: str, author: str, permlink: str):
    return _render("discover.html", request)


@router.get("/@{author}", include_in_schema=False)
async def discover_author(request: Request, author: str):
    return _render("discover.html", request)


@router.get("/@{author}/{permlink}", include_in_schema=False)
async def discover_post(request: Request, author: str, permlink: str):
    return _render("discover.html", request)


# ── SEO ──────────────────────────────────────────────────────────────────────

@router.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    sitemap_line = f"\nSitemap: {site_url}/sitemap.xml" if site_url else ""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /internal/\n"
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

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    urls = [
        f"  <url><loc>{xml_escape(site_url)}/</loc>"
        f"<lastmod>{now}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>"
    ]

    # Recent posts (last 1000)
    result = await db.execute(text(
        "SELECT p.author, p.permlink, p.created FROM posts p "
        "WHERE EXISTS (SELECT 1 FROM post_category WHERE post_id = p.id) "
        "ORDER BY p.created DESC LIMIT 1000"
    ))
    for row in result:
        author, permlink, created = row
        lastmod = created.strftime("%Y-%m-%d") if created else now
        loc = f"{site_url}/@{xml_escape(author)}/{xml_escape(permlink)}"
        urls.append(
            f"  <url><loc>{loc}</loc>"
            f"<lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n"
        '</urlset>\n'
    )

    cache.put("sitemap_xml", xml, ttl=600)
    return Response(content=xml, media_type="application/xml")


@router.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    body = (
        "# HiveComb\n"
        "\n"
        "> Semantic post discovery for the Hive blockchain.\n"
        "\n"
        "HiveComb is a read-only content discovery overlay for the Hive blockchain.\n"
        "It streams posts, classifies them by topic, language, and sentiment using\n"
        "embedding-based cosine similarity, and lets users browse and filter posts\n"
        "across Hive communities.\n"
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
    sentiment: str | None = Query(default=None),
    community: str | None = Query(default=None, description="Filter by Hive community ID (e.g. hive-174578)"),
    communities: list[str] | None = Query(default=None, description="Filter by multiple community IDs; overrides community"),
    authors: list[str] | None = Query(default=None, description="Filter by author usernames"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10000),
    cursor: str | None = Query(default=None, description="Opaque cursor from previous response for keyset pagination"),
):
    result = await crud.browse_posts(
        db, categories=category, languages=language,
        sentiment=sentiment,
        community=None if communities else community,
        communities=communities,
        authors=authors,
        limit=limit, offset=offset, cursor=cursor,
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
@cache.cached_response("overview_stats", ttl=30)
async def overview_stats(db: AsyncSession = Depends(get_db)):
    result = await crud.get_overview_stats(db)
    result["api_base_url"] = settings.api_base_url
    return result
