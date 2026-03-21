"""Internal API endpoints — used by the seed script and Hive worker.

All routes require the X-API-Key header.
"""
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import crud
from ..deps import get_db, require_api_key

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_api_key)])


# ── Centroids ─────────────────────────────────────────────────────────────────

class CentroidsPayload(BaseModel):
    centroids: dict[str, list[float]] = Field(
        ...,
        description="Map of category name → embedding vector (must be 384-dimensional).",
        examples=[{"crypto": [0.012] * 384, "programming": [0.034] * 384}],
    )
    metadata: dict = Field(
        default={},
        description="Optional metadata (similarity_threshold, llm_model, embedding_model, etc.).",
        examples=[{"similarity_threshold": 0.45, "llm_model": "llama3.1:8b", "embedding_model": "all-MiniLM-L6-v2"}],
    )


@router.post(
    "/centroids",
    summary="Upload category centroids",
    description=(
        "Save category centroid vectors to pgvector and immediately reload them "
        "into the in-memory classifier — no restart required.\n\n"
        "Called by the seed script after computing centroids from LLM-labeled posts."
    ),
)
async def upload_centroids(
    payload: CentroidsPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await crud.save_centroids(db, payload.centroids, payload.metadata)

    # Reload in-memory centroids for the running process.
    fresh = await crud.get_centroids(db)
    request.app.state.centroids = {cat: np.array(vec) for cat, vec in fresh.items()}
    threshold = payload.metadata.get("similarity_threshold", 0.45)
    request.app.state.similarity_threshold = threshold

    return {
        "saved": len(payload.centroids),
        "active": len(request.app.state.centroids),
        "threshold": threshold,
    }


# ── Stream cursors ─────────────────────────────────────────────────────────────

class CursorPayload(BaseModel):
    block_num: int = Field(
        ...,
        description="Hive block number to store as the cursor position.",
        examples=[95000000],
    )


@router.get(
    "/stream-cursor/{key}",
    summary="Read stream cursor",
    description="Return the last processed Hive block number for a given consumer key. Returns 404 if not set yet.",
)
async def get_stream_cursor(key: str, db: AsyncSession = Depends(get_db)):
    block_num = await crud.get_cursor(db, key)
    if block_num is None:
        raise HTTPException(status_code=404, detail=f"No cursor found for key '{key}'")
    return {"key": key, "block_num": block_num}


@router.put(
    "/stream-cursor/{key}",
    summary="Update stream cursor",
    description="Upsert the stream cursor position for a consumer. Used by the Hive worker to track its progress.",
)
async def set_stream_cursor(
    key: str, payload: CursorPayload, db: AsyncSession = Depends(get_db)
):
    await crud.set_cursor(db, key, payload.block_num)
    return {"key": key, "block_num": payload.block_num}
