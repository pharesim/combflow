"""Post endpoints — detail."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import crud
from ..deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


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
