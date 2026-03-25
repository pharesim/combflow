"""Report endpoints — misclassification reporting with signature verification."""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import crud
from ..deps import get_db
from ..hive_auth import fetch_posting_keys, verify_hive_signature

logger = logging.getLogger(__name__)

router = APIRouter()

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,15}$")


class ReportRequest(BaseModel):
    username: str
    reason: str
    signature: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError("Invalid Hive username")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Reason cannot be empty")
        if len(v) > 1000:
            raise ValueError("Reason must be 1000 characters or less")
        return v

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, v: str) -> str:
        if not v:
            raise ValueError("Signature cannot be empty")
        return v


@router.post(
    "/api/posts/{author}/{permlink}/report",
    summary="Report a misclassified post",
    tags=["reports"],
    status_code=201,
)
async def submit_report(
    body: ReportRequest,
    author: str = Path(..., max_length=16, pattern=r"^[a-z0-9][a-z0-9.\-]{0,15}$"),
    permlink: str = Path(..., max_length=256),
    db: AsyncSession = Depends(get_db),
):
    post = await crud.get_post_by_permlink(db, author, permlink)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    message = f"combflow_report_{author}/{permlink}_{body.reason}"

    posting_keys = await fetch_posting_keys(body.username)
    if not posting_keys:
        raise HTTPException(status_code=403, detail="Signature verification failed")

    sig_valid = verify_hive_signature(message, body.signature, posting_keys)
    if not sig_valid:
        raise HTTPException(status_code=403, detail="Signature verification failed")

    try:
        report = await crud.create_post_report(
            db,
            post_id=post["id"],
            reporter=body.username,
            reason=body.reason,
            signature=body.signature,
            message=message,
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail="You have already reported this post")

    return report


@router.get(
    "/api/reports",
    summary="List misclassification reports",
    tags=["reports"],
)
async def list_reports(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    post_author: str | None = Query(default=None),
    post_permlink: str | None = Query(default=None),
    reporter: str | None = Query(default=None),
):
    return await crud.list_post_reports(
        db,
        limit=limit,
        offset=offset,
        post_author=post_author,
        post_permlink=post_permlink,
        reporter=reporter,
    )
