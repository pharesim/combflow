"""UI routes — serves the discovery page and supporting API endpoints."""
import pathlib

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
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


# ── HTML pages ────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def root():
    return HTMLResponse(_TEMPLATES["discover.html"])


@router.get("/ui", include_in_schema=False)
async def discover_page_redirect():
    return RedirectResponse("/", status_code=301)


@router.get("/{prefix}/@{author}/{permlink}", include_in_schema=False)
async def discover_prefixed_post(prefix: str, author: str, permlink: str):
    return HTMLResponse(_TEMPLATES["discover.html"])


@router.get("/@{author}/{permlink}", include_in_schema=False)
async def discover_post(author: str, permlink: str):
    return HTMLResponse(_TEMPLATES["discover.html"])


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
