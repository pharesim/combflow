"""Image proxy endpoint — privacy-preserving server-side image fetching."""
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_URL_LENGTH = 2048
_MAX_BODY_SIZE = 50 * 1024 * 1024  # 50 MB
_CHUNK_SIZE = 64 * 1024  # 64 KB


@router.get(
    "/api/imageproxy",
    summary="Proxy an external image",
    tags=["images"],
    responses={
        400: {"description": "Missing or invalid URL"},
        413: {"description": "Image exceeds 50 MB size limit"},
        502: {"description": "Upstream error (non-image, timeout, connection failure)"},
    },
)
async def imageproxy(
    request: Request,
    url: str = Query(..., max_length=_MAX_URL_LENGTH),
):
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Only HTTPS URLs are allowed")

    client: httpx.AsyncClient = request.app.state.http_client

    try:
        upstream = await client.send(
            client.build_request("GET", url),
            stream=True,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=502, detail="Upstream timeout")
    except httpx.HTTPError as exc:
        logger.warning("Image proxy connection error for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail="Upstream connection error")

    content_type = upstream.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        await upstream.aclose()
        raise HTTPException(status_code=502, detail="Upstream returned non-image content")

    content_length = upstream.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_SIZE:
        await upstream.aclose()
        raise HTTPException(status_code=413, detail="Image exceeds size limit")

    async def _stream():
        bytes_read = 0
        try:
            async for chunk in upstream.aiter_bytes():
                bytes_read += len(chunk)
                if bytes_read > _MAX_BODY_SIZE:
                    break
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        _stream(),
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
