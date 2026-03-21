"""Post endpoints — ingestion, detail, and comments."""
import collections
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ... import cache
from ...db import crud
from ...hafsql import get_comments as hafsql_get_comments
from ..deps import get_db, require_api_key, require_jwt
from ..schemas import PostCreate

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Per-user rate limiting for comment cache invalidation (proposal 003) ─────
_RATE_WINDOW = 60  # seconds
_RATE_MAX_INVALIDATE = 5  # max cache invalidations per user per window
_rate_log: dict[str, collections.deque] = {}
_RATE_LOG_MAX = 50_000


def _check_user_rate(username: str, limit: int) -> None:
    now = time.time()
    key = f"comment_cache:{username}"
    bucket = _rate_log.setdefault(key, collections.deque())
    while bucket and bucket[0] < now - _RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, "Too many requests, try again later")
    bucket.append(now)
    if len(_rate_log) > _RATE_LOG_MAX:
        stale = [k for k, v in _rate_log.items() if not v or v[-1] < now - _RATE_WINDOW]
        for k in stale:
            del _rate_log[k]


# ── Post ingestion (authenticated — used by worker fallback / external tools) ─

@router.post(
    "/posts",
    dependencies=[Depends(require_api_key)],
    summary="Ingest a classified post",
    tags=["posts"],
)
async def create_post_endpoint(post: PostCreate, db: AsyncSession = Depends(get_db)):
    await crud.create_post(db, post.model_dump())
    return {"status": "success"}


# ── Post detail (public) ─────────────────────────────────────────────────────

@router.get(
    "/posts/{author}/{permlink}",
    summary="Get a post with classification details",
    tags=["posts"],
    description="Returns the post with its categories, languages, and sentiment.",
)
async def get_post(
    author: str = Path(..., max_length=16, pattern=r"^[a-z0-9][a-z0-9.\-]{0,15}$"),
    permlink: str = Path(..., max_length=256),
    db: AsyncSession = Depends(get_db),
):
    post = await crud.get_post_by_permlink(db, author, permlink)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


# ── Comment tree (proposal 002) ─────────────────────────────────────────────

_COMMENTS_CACHE_TTL = 120  # seconds


def _build_comment_tree(
    flat: list[dict], root_author: str, root_permlink: str, max_depth: int
) -> tuple[list[dict], int]:
    """Build hierarchical comment tree from flat list.

    Filters out comments with reputation <= 0.
    Returns (tree, hidden_count).
    """
    hidden_count = 0
    visible = []
    for c in flat:
        if c["reputation"] <= 0:
            hidden_count += 1
        else:
            visible.append(c)

    # Index by (author, permlink) for parent lookup.
    by_key: dict[tuple[str, str], dict] = {}
    for c in visible:
        node = {
            "author": c["author"],
            "permlink": c["permlink"],
            "body": c["body"],
            "created": c["created"],
            "reputation": c["reputation"],
            "children": [],
            "_depth": 0,
        }
        by_key[(c["author"], c["permlink"])] = node

    # Build tree.
    roots: list[dict] = []
    for c in visible:
        node = by_key[(c["author"], c["permlink"])]
        parent_key = (c["parent_author"], c["parent_permlink"])
        parent = by_key.get(parent_key)
        if parent:
            node["_depth"] = parent["_depth"] + 1
            if node["_depth"] <= max_depth:
                parent["children"].append(node)
            else:
                hidden_count += 1
        else:
            # Top-level comment (parent is the root post).
            node["_depth"] = 1
            roots.append(node)

    # Strip internal _depth field.
    def _strip(nodes: list[dict]) -> list[dict]:
        for n in nodes:
            n.pop("_depth", None)
            _strip(n["children"])
        return nodes

    return _strip(roots), hidden_count


@router.get(
    "/posts/{author}/{permlink}/comments",
    summary="Hierarchical comment tree for a post",
    tags=["comments"],
)
async def get_comments(
    author: str = Path(..., max_length=16, pattern=r"^[a-z0-9][a-z0-9.\-]{0,15}$"),
    permlink: str = Path(..., max_length=256),
    depth: int = Query(6, ge=1, le=10, description="Maximum nesting depth"),
):
    cache_key = f"comments:{author}/{permlink}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    flat = hafsql_get_comments(author, permlink)
    tree, hidden_count = _build_comment_tree(flat, author, permlink, depth)
    result = {"comments": tree, "hidden_count": hidden_count}
    cache.put(cache_key, result, _COMMENTS_CACHE_TTL)
    return result


# ── Comment cache invalidation (proposal 003) ───────────────────────────────

@router.delete(
    "/posts/{author}/{permlink}/comments/cache",
    summary="Invalidate cached comment tree",
    tags=["comments"],
    status_code=204,
)
async def invalidate_comment_cache(
    author: str = Path(..., max_length=16, pattern=r"^[a-z0-9][a-z0-9.\-]{0,15}$"),
    permlink: str = Path(..., max_length=256),
    username: str = Depends(require_jwt),
):
    _check_user_rate(username, _RATE_MAX_INVALIDATE)
    cache.invalidate(f"comments:{author}/{permlink}")
