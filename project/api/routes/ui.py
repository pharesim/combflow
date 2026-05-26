"""UI routes — serves the discovery page and supporting API endpoints."""
import asyncio
import json as _json
import logging
import pathlib
import re
from datetime import datetime, timezone
from html import escape as html_escape
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ... import apps_canonical, cache
from ...categories import LEAF_CATEGORIES
from ...config import settings
from ...db import crud
from ...hafsql import (
    extract_post_metadata,
    get_hivecomb_posts,
    get_post_full,
    get_top_comments,
)
from ...text import clean_post_body
from ..deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = _TEMPLATE_DIR / "static"


def _compute_asset_version() -> str:
    """Combined sha256 of every static file (sorted), used as a cache buster
    so /static/X.js becomes /static/X.js?v=HASH. Hash changes on any static
    file change → URL changes → browser/CDN cache invalidates instantly."""
    import hashlib
    h = hashlib.sha256()
    if _STATIC_DIR.exists():
        for path in sorted(_STATIC_DIR.rglob("*")):
            if path.is_file():
                h.update(str(path.relative_to(_STATIC_DIR)).encode())
                h.update(b"\0")
                h.update(path.read_bytes())
                h.update(b"\0")
    return h.hexdigest()[:12]


_ASSET_VERSION = _compute_asset_version()
# Rewrite every self-hosted static reference to include ?v=HASH at template
# load time. One-time work; per-request rendering pays nothing.
_STATIC_REF_RE = __import__("re").compile(r'(src|href)="(/static/[^"?]+)"')
_TEMPLATES = {
    name: _STATIC_REF_RE.sub(
        rf'\1="\2?v={_ASSET_VERSION}"',
        (_TEMPLATE_DIR / name).read_text(),
    )
    for name in ["discover.html", "privacy.html", "terms.html", "takedown.html"]
}

# Default OG values — must match what's in discover.html.
_OG_DEFAULT_TITLE = "HiveComb \u2014 Discover Hive Blockchain Content"
_OG_DEFAULT_DESC = (
    "Discover and explore Hive blockchain posts by topic, language, and sentiment. "
    "Browse communities, filter by category, and find content that matches your interests."
)


# Inline-content limits for the server-rendered post-page enrichments
# (proposals 095/096). Top N comments, each excerpted to keep page weight bounded.
_INLINE_COMMENT_LIMIT = 10
_INLINE_COMMENT_EXCERPT = 280


def _proxied_avatar(author: str) -> str:
    """Same-origin avatar URL (routed through the image proxy so it satisfies
    the tightened CSP img-src, which only allows 'self')."""
    target = f"https://images.hive.blog/u/{quote(author, safe='')}/avatar/small"
    return f"/api/imageproxy?url={quote(target, safe='')}"


def _build_comments_html(comments: list[dict] | None) -> str:
    """Server-render the top comments as plain-text excerpts (proposal 095,
    approval decision #1 — no server-side markdown renderer). Returns the full
    ``<section>`` block (so it renders nothing on pages without comments) or ""."""
    if not comments:
        return ""
    items: list[str] = []
    ld_comments: list[dict] = []
    for c in comments:
        author = c.get("author") or ""
        body = clean_post_body(c.get("body") or "")
        if not body:
            continue
        excerpt = body[:_INLINE_COMMENT_EXCERPT].rstrip()
        if len(body) > _INLINE_COMMENT_EXCERPT:
            excerpt += "…"
        items.append(
            '<li class="inline-comment">'
            f'<a class="inline-comment-author" href="/@{html_escape(author)}">@{html_escape(author)}</a>'
            f'<blockquote class="inline-comment-body">{html_escape(excerpt)}</blockquote>'
            "</li>"
        )
        ld_comments.append({
            "@type": "Comment",
            "author": {"@type": "Person", "name": f"@{author}"},
            "text": excerpt,
        })
    if not items:
        return ""
    ld = _json.dumps(
        {"@context": "https://schema.org", "@type": "ItemList",
         "itemListElement": ld_comments},
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return (
        '<section id="comments-inline" aria-label="Top comments">'
        "<h2>Comments</h2>"
        f'<ul class="inline-comments">{"".join(items)}</ul>'
        f'<script type="application/ld+json">{ld}</script>'
        "</section>"
    )


def _build_author_card_html(card: dict | None, site_url: str) -> str:
    """Compact 'About the author' card for post pages (proposal 096). Reuses the
    summary from ``crud.get_author_summary``; reputation comes from the already
    fetched bridge.get_post payload (no extra RPC). Returns the full ``<aside>``
    block or "" when the author has no classified posts."""
    if not card:
        return ""
    summary = card.get("summary")
    if not summary:
        return ""
    author = card["author"]
    a = html_escape(author)
    total = summary["total_posts"]
    rep = card.get("reputation")
    rep_html = (
        f'<span class="reputation">Reputation: {int(rep)}</span>'
        if isinstance(rep, (int, float))
        else ""
    )
    cats = ", ".join(c["name"] for c in summary.get("top_categories", [])[:3])
    cats_html = (
        f'<div class="top-cats">Mostly writes about: {html_escape(cats)}</div>'
        if cats
        else ""
    )
    ld = _json.dumps(
        {"@context": "https://schema.org", "@type": "Person", "name": f"@{author}",
         "url": f"{site_url}/@{author}",
         "knowsAbout": [c["name"] for c in summary.get("top_categories", [])]},
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return (
        '<aside class="author-card" aria-label="About the author">'
        f'<img src="{_proxied_avatar(author)}" alt="@{a}" width="48" height="48" loading="lazy">'
        '<div class="meta">'
        f'<a href="/@{a}"><strong>@{a}</strong></a>'
        f"{rep_html}"
        f'<span class="post-count">{total} posts on HiveComb</span>'
        f"{cats_html}"
        f'<a class="all-posts" href="/@{a}">View all posts →</a>'
        "</div>"
        f'<script type="application/ld+json">{ld}</script>'
        "</aside>"
    )


def _build_author_summary_html(author: str, summary: dict | None, site_url: str) -> str:
    """Server-render the author profile summary block for ``/@author`` pages
    (proposal 098). Unique-per-page content above the post grid, plus a Person
    JSON-LD blob. Returns the full ``<section>`` or "" when no summary."""
    if not summary:
        return ""
    a = html_escape(author)
    total = summary["total_posts"]
    first_seen = summary.get("first_seen")
    since = f" · Active since {first_seen.year}" if first_seen else ""
    cat_html = "".join(
        f'<a href="/c/{html_escape(c["id"])}">{html_escape(c["name"])}</a> ({c["count"]}) '
        for c in summary.get("top_categories", [])
    )
    lang_html = "".join(
        f'<a href="/lang/{html_escape(l["code"])}">{html_escape(l["code"])}</a> ({l["count"]}) '
        for l in summary.get("top_languages", [])
    )
    tc = summary.get("top_community")
    comm_html = (
        "<div><dt>Top community</dt>"
        f'<dd><a href="/community/{html_escape(tc["id"])}">{html_escape(tc["name"])}</a> ({tc["count"]})</dd></div>'
        if tc
        else ""
    )
    ld = _json.dumps(
        {"@context": "https://schema.org", "@type": "Person", "name": f"@{author}",
         "url": f"{site_url}/@{author}",
         "knowsAbout": [c["name"] for c in summary.get("top_categories", [])]},
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return (
        '<section class="author-summary" aria-label="Author profile">'
        f"<header><h1>@{a}</h1>"
        f'<p class="lede">{total} posts on Hive{since}</p></header>'
        '<dl class="author-stats">'
        f"<div><dt>Top categories</dt><dd>{cat_html}</dd></div>"
        f"<div><dt>Languages</dt><dd>{lang_html}</dd></div>"
        f"{comm_html}"
        "</dl>"
        f'<script type="application/ld+json">{ld}</script>'
        "</section>"
    )


def _build_author_description(author: str, summary: dict) -> str:
    """Per-author meta description for ``/@author`` pages (proposal 098).
    Capped at ~155 chars to fit Google's snippet budget."""
    parts = [f"Browse {summary['total_posts']} classified posts by @{author} on HiveComb."]
    cats = [c["name"] for c in summary.get("top_categories", [])[:3]]
    if cats:
        parts.append(f"Most active in {', '.join(cats)}.")
    langs = [l["code"] for l in summary.get("top_languages", [])[:1]]
    if langs:
        parts.append(f"Primary language: {langs[0]}.")
    return " ".join(parts)[:155]


def _render_legal(name: str) -> HTMLResponse:
    """Return a legal page template with placeholders replaced."""
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    html = (
        _TEMPLATES[name]
        .replace("{{SITE_URL}}", site_url)
        .replace("{{LEGAL_DATE}}", "12 April 2026")
    )
    return HTMLResponse(html)


def _render(
    name: str,
    request: Request,
    og: dict | None = None,
    post_data: dict | None = None,
    comments: list[dict] | None = None,
    author_card: dict | None = None,
    author_summary: dict | None = None,
) -> HTMLResponse:
    """Render the discover template with placeholders substituted.

    Distinguishes canonical (SEO claim, may be off-site or omitted) from
    og:url (share endpoint, always our URL).

    `og["canonical_self"]` (bool, default True): whether to self-canonical
    when no off-site canonical is given. Post pages may pass False to omit
    the canonical tag entirely when we can't identify the rightful publisher.
    """
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    default_image = f"{site_url}/static/og-image.png"

    # og:url always points to us — that's the URL share clicks should land on.
    own_url = html_escape(site_url + request.url.path)

    # Canonical: prefer off-site (when set and pointing elsewhere), else
    # self-canonical, else omit entirely.
    external_canonical = (og or {}).get("canonical") if og else None
    canonical_self = (og or {}).get("canonical_self", True) if og else True
    if external_canonical and site_url and not external_canonical.startswith(site_url):
        canonical_tag = f'<link rel="canonical" href="{html_escape(external_canonical)}">'
    elif canonical_self:
        canonical_tag = f'<link rel="canonical" href="{own_url}">'
    else:
        canonical_tag = ""

    og_type = html_escape(og["type"]) if og and "type" in og else "website"
    og_title = html_escape(og["title"]) if og and "title" in og else _OG_DEFAULT_TITLE
    og_desc = html_escape(og["description"]) if og and "description" in og else _OG_DEFAULT_DESC
    og_image = html_escape(og["image"]) if og and "image" in og else default_image

    robots_tag = (
        '<meta name="robots" content="noindex,follow">'
        if og and og.get("noindex")
        else ""
    )

    # Inline post data so the client can render the modal without a second
    # bridge.get_post RPC. Use JSON in a script tag; safe per HTML spec as
    # long as we replace the literal "</" sequence (case-insensitive).
    if post_data:
        import json as _json
        post_json = _json.dumps(post_data, separators=(",", ":"), default=str)
        post_json = post_json.replace("</", "<\\/")
        post_data_tag = (
            '<script id="hivecomb-post-data" type="application/json">'
            f'{post_json}</script>'
        )
    else:
        post_data_tag = ""

    # Server-rendered post-page / author-page enrichments (proposals 095/096/098).
    # Each builder returns the complete block or "" — so the placeholder renders
    # nothing on pages without the data (this template engine has no conditionals).
    # author_card / author_summary are {"author": str, "summary": dict | None} wrappers.
    comments_html = _build_comments_html(comments)
    author_card_html = _build_author_card_html(author_card, site_url)
    if author_summary:
        author_summary_html = _build_author_summary_html(
            author_summary["author"], author_summary.get("summary"), site_url
        )
    else:
        author_summary_html = ""

    html = (
        _TEMPLATES[name]
        .replace("{{SITE_URL}}", site_url)
        .replace("{{CANONICAL_LINK_TAG}}", canonical_tag)
        .replace("{{ROBOTS_TAG}}", robots_tag)
        .replace("{{OG_URL}}", own_url)
        .replace("{{OG_TYPE}}", og_type)
        .replace("{{OG_TITLE}}", og_title)
        .replace("{{OG_DESCRIPTION}}", og_desc)
        .replace("{{OG_IMAGE}}", og_image)
        .replace("{{POST_DATA_SCRIPT}}", post_data_tag)
        .replace("{{COMMENTS_HTML}}", comments_html)
        .replace("{{AUTHOR_CARD_HTML}}", author_card_html)
        .replace("{{AUTHOR_SUMMARY_HTML}}", author_summary_html)
    )
    return HTMLResponse(html)


# Hard-coded fallback for the cross-post canonical destination: peakd renders
# any Hive post, and pinning to one consistent surface lets Google consolidate.
_CROSSPOST_DEFAULT_TEMPLATE = "https://peakd.com/@{author}/{permlink}"


def _build_og_from_meta(meta: dict, author: str, permlink: str) -> dict:
    """Turn extracted post metadata into the og dict consumed by _render.

    Canonical resolution order (top-level posts only):
      1. Explicit json_metadata.canonical_url → honor it
      2. Cross-post (original_author + original_permlink) → canonical to
         the original (rendered on peakd, which serves any Hive post)
      3. Known publishing app in the shared apps-canonical list → derive
      4. Otherwise → omit canonical (canonical_self=False)

    Replies/comments (parent_author set) get noindex,follow and no
    canonical. peakd/ecency/hive.blog don't render comment URLs as
    standalone pages — they show the parent discussion — so any
    canonical we'd claim points at content Google sees as duplicate
    of the parent post URL, and the dedupe overrides ours anyway.
    """
    og: dict = {"type": "article"}
    if meta["title"]:
        og["title"] = meta["title"]
    if meta["description"]:
        og["description"] = meta["description"]
    if meta["image"]:
        og["image"] = f"https://images.hive.blog/0x0/{meta['image']}"

    if meta.get("parent_author"):
        og["noindex"] = True
        og["canonical_self"] = False
        return og

    app_urls = apps_canonical.APP_CANONICAL_URLS
    if meta.get("canonical_url"):
        og["canonical"] = meta["canonical_url"]
    elif meta.get("original_author") and meta.get("original_permlink"):
        template = app_urls.get("peakd", _CROSSPOST_DEFAULT_TEMPLATE)
        og["canonical"] = template.format(
            author=meta["original_author"], permlink=meta["original_permlink"]
        )
    elif meta.get("app") in app_urls:
        og["canonical"] = app_urls[meta["app"]].format(
            author=author, permlink=permlink
        )
    else:
        og["canonical_self"] = False
    return og


async def _fetch_post(
    author: str, permlink: str
) -> tuple[dict, dict | None, list[dict]]:
    """Fetch post + top comments concurrently (proposal 095).

    → (og_overrides, raw_post_data, top_comments). The bridge.get_post and
    bridge.get_discussion RPCs run concurrently via ``asyncio.gather`` (approval
    decision #2 — shipping them sequentially is a latency regression). Raw post
    data is inlined into the HTML so the client renders the modal without a
    duplicate RPC; og overrides drive the per-page tags. Returns ({}, None, [])
    on RPC failure.
    """
    try:
        raw, comments = await asyncio.gather(
            asyncio.to_thread(get_post_full, author, permlink),
            asyncio.to_thread(get_top_comments, author, permlink, _INLINE_COMMENT_LIMIT),
        )
        if not raw:
            return ({}, None, [])
        og = _build_og_from_meta(extract_post_metadata(raw), author, permlink)
        # Replies are noindex'd and don't have a standalone discussion worth
        # inlining — drop the (already fetched, concurrently) comments for them.
        if raw.get("parent_author"):
            comments = []
        return (og, raw, comments)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug("post fetch failed for %s/%s: %s", author, permlink, exc)
        return ({}, None, [])


async def _safe_author_summary(db: AsyncSession, author: str) -> dict | None:
    """crud.get_author_summary that swallows DB errors (returns None). Lets a
    post-page render proceed even if the author aggregation query fails."""
    try:
        return await crud.get_author_summary(db, author)
    except Exception as exc:
        logger.debug("author summary lookup failed for %s: %s", author, exc)
        return None


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


# Below this lifetime classified-post count, an /@author page is too thin
# for Google to want to index \u2014 it'll get flagged as Soft 404. We mark
# such pages noindex,follow so they're cleanly excluded rather than failing.
_AUTHOR_INDEX_MIN_POSTS = 10


@router.get("/@{author}", include_in_schema=False)
async def discover_author(
    request: Request, author: str, db: AsyncSession = Depends(get_db)
):
    # Default shell: also the graceful fallback on DB error \u2014 render WITHOUT
    # noindex, since hiding a possibly-substantive profile on a transient error
    # is worse than risking an indexable thin one.
    default_og = {
        "title": f"@{author} \u2014 HiveComb",
        "description": f"Posts by @{author} on HiveComb",
    }
    try:
        summary = await crud.get_author_summary(db, author)
    except Exception as exc:
        logger.debug("author summary lookup failed for %s: %s", author, exc)
        return _render("discover.html", request, og=default_og)

    total = summary["total_posts"] if summary else 0
    if total < _AUTHOR_INDEX_MIN_POSTS:
        # Thin profile \u2192 noindex,follow (Soft-404 avoidance) + shell-only render.
        return _render(
            "discover.html", request, og={**default_og, "noindex": True}
        )

    cats = ", ".join(c["name"] for c in summary["top_categories"][:3])
    og = {
        "canonical_self": True,
        "title": f"@{author} \u2014 {total} posts on Hive" + (f" \u00b7 {cats}" if cats else ""),
        "description": _build_author_description(author, summary),
    }
    return _render(
        "discover.html", request, og=og,
        author_summary={"author": author, "summary": summary},
    )


@router.get("/@{author}/{permlink}", include_in_schema=False)
async def discover_post(
    request: Request, author: str, permlink: str, db: AsyncSession = Depends(get_db)
):
    # Post fetch (+ inline comments) and the author aggregation run concurrently.
    (og, raw, comments), summary = await asyncio.gather(
        _fetch_post(author, permlink),
        _safe_author_summary(db, author),
    )
    # Reputation for the mini-card comes free from the bridge.get_post payload
    # (no extra RPC) \u2014 bridge posts carry a pre-computed author_reputation score.
    reputation = raw.get("author_reputation") if raw else None
    return _render(
        "discover.html", request, og=og, post_data=raw, comments=comments,
        author_card={"author": author, "summary": summary, "reputation": reputation},
    )


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


async def warm_sitemap_cache(session_factory) -> bool:
    """Pre-build and cache the sitemap. Returns True on success, False on
    failure (so periodic_sitemap_warm can retry sooner). Doesn't overwrite
    the existing cache when the build fails — stale-but-valid beats empty."""
    site_url = settings.site_url.rstrip("/") if settings.site_url else ""
    if not site_url:
        return True
    try:
        async with session_factory() as session:
            xml = await _build_sitemap_xml(session, site_url)
        cache.put("sitemap_xml", xml, ttl=_SITEMAP_TTL)
        logger.info("sitemap cache warmed (%d bytes)", len(xml))
        return True
    except Exception as exc:
        logger.warning("sitemap warm failed: %s", exc)
        return False


async def periodic_sitemap_warm(session_factory, interval: int = _SITEMAP_TTL // 2) -> None:
    """Warm immediately, then re-warm every `interval` seconds (default 12h).
    On failure, retry after 5 min instead of waiting the full interval — a
    transient HAFSQL hiccup at startup shouldn't lose a day of fresh sitemaps."""
    while True:
        ok = await warm_sitemap_cache(session_factory)
        delay = interval if ok else 300
        try:
            await asyncio.sleep(delay)
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


# Hive username pattern (matches the post/report routes' author validation).
_USERNAME_PATTERN = r"^[a-z0-9][a-z0-9.\-]{0,15}$"


@router.get("/api/authors/{author}/summary", tags=["discovery"],
            summary="Author profile summary")
async def author_summary(
    response: Response,
    author: str = Path(..., max_length=16, pattern=_USERNAME_PATTERN),
    db: AsyncSession = Depends(get_db),
):
    """Aggregated author stats for the post-page author mini-card (proposal 099):
    total classified posts, top categories/languages/community. Lets the SPA modal
    render the card for any post opened client-side, where no server-rendered copy
    exists.

    Reputation is intentionally NOT returned — the modal already holds
    ``author_reputation`` from the bridge.get_post payload, so this stays a fast,
    DB-only read. Backed by ``crud.get_author_summary`` (6h in-process cache).
    Returns ``{"summary": {...} | null}``; null when the author has no classified
    posts. Malformed usernames are rejected by path validation (422).
    """
    summary = await crud.get_author_summary(db, author)
    response.headers["Cache-Control"] = "public, max-age=21600"
    return {"summary": summary}
