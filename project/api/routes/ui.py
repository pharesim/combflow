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


def _read_template(name: str) -> str:
    return _TEMPLATES[name]


# ── HTML pages ────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def root():
    return HTMLResponse(_read_template("discover.html"))


@router.get("/ui", include_in_schema=False)
async def discover_page_redirect():
    return RedirectResponse("/", status_code=301)


@router.get("/@{author}/{permlink}", include_in_schema=False)
async def discover_post(author: str, permlink: str):
    return HTMLResponse(_read_template("discover.html"))


# ── Browse API ────────────────────────────────────────────────────────────────

@router.get("/api/browse", tags=["discovery"], summary="Browse posts with filters")
async def browse_posts(
    db: AsyncSession = Depends(get_db),
    category: list[str] | None = Query(default=None),
    language: list[str] | None = Query(default=None),
    sentiment: str | None = Query(default=None),
    community: str | None = Query(default=None, description="Filter by Hive community ID (e.g. hive-174578)"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10000),
    cursor: str | None = Query(default=None, description="Opaque cursor from previous response for keyset pagination"),
):
    result = await crud.browse_posts(
        db, categories=category, languages=language,
        sentiment=sentiment, community=community,
        limit=limit, offset=offset, cursor=cursor,
    )
    return {"posts": result["posts"], "count": len(result["posts"]), "total": result["total"], "next_cursor": result["next_cursor"]}


@router.get("/api/languages", tags=["discovery"], summary="Available languages")
async def available_languages(db: AsyncSession = Depends(get_db)):
    cached = cache.get("languages")
    if cached is not None:
        return cached
    result = {"languages": await crud.get_available_languages(db)}
    cache.put("languages", result, ttl=3600)
    return result


@router.get("/api/communities", tags=["discovery"], summary="Communities with post counts")
async def available_communities(db: AsyncSession = Depends(get_db)):
    cached = cache.get("communities")
    if cached is not None:
        return cached
    rows = await crud.get_available_communities(db)
    result = {"communities": rows}
    cache.put("communities", result, ttl=300)
    return result


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
async def overview_stats(db: AsyncSession = Depends(get_db)):
    cached = cache.get("overview_stats")
    if cached is not None:
        return cached
    result = await crud.get_overview_stats(db)
    result["api_base_url"] = settings.api_base_url
    cache.put("overview_stats", result, ttl=30)
    return result
