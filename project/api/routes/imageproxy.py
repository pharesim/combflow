"""Image proxy endpoint — privacy-preserving server-side image fetching."""
import asyncio
import functools
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ... import cache

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_URL_LENGTH = 2048
_MAX_BODY_SIZE = 50 * 1024 * 1024  # 50 MB

# Strict raster allowlist. Excludes image/svg+xml: browsers execute <script>
# and event handlers inside an SVG loaded as a top-level document, which would
# turn the proxy into a stored-XSS vector on the app origin (proposal 101, H1).
_ALLOWED_CONTENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg",
    "image/gif", "image/webp", "image/avif",
})

# Host-resolution hardening (proposal 101, Change 4a). getaddrinfo is blocking,
# so it runs on a small DEDICATED thread pool rather than the shared
# asyncio.to_thread executor — a slow or bursty resolver on the image hot path
# (the UI fires 6 concurrent fetchMeta requests, and Cloudflare caches only the
# 200 path) then can't starve unrelated to_thread work elsewhere in the process.
# Validated (host, port) outcomes are cached briefly so a page-load burst
# resolves each distinct host once.
_RESOLVE_TTL = 60.0      # seconds — cache a validated host resolution
_RESOLVE_TIMEOUT = 5.0   # seconds — cap per-request DNS latency
_resolver_executor = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="imgproxy-dns"
)


async def _assert_host_is_global(host: str, port: int) -> None:
    """Resolve ``host:port`` and reject any non-globally-routable address.

    SSRF guard (proposal 101, H2): rejects loopback, RFC1918, link-local
    (including the 169.254.169.254 cloud-metadata IP), ULA, CGNAT 100.64.0.0/10,
    and unspecified addresses, plus their IPv4-mapped-IPv6 forms. ``not
    ip.is_global`` is strictly stronger than the explicit private/loopback/...
    disjunction — notably it also catches CGNAT, which all of is_private/
    is_loopback/is_link_local/is_reserved/is_multicast/is_unspecified miss.

    Raises ``HTTPException(400)`` on resolution failure or a disallowed address.
    A successful validation is cached ``(host, port) -> True`` for
    ``_RESOLVE_TTL`` so a burst of fetches from one host resolves it once;
    resolution runs on a dedicated thread pool, bounded by ``_RESOLVE_TIMEOUT``.
    """
    cache_key = f"imgproxy_dns:{host}:{port}"
    if cache.get(cache_key) is True:
        return
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(
                _resolver_executor,
                functools.partial(
                    socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
                ),
            ),
            timeout=_RESOLVE_TIMEOUT,
        )
    except (socket.gaierror, UnicodeError, asyncio.TimeoutError):
        # gaierror: name doesn't resolve. UnicodeError: IDNA-encoding failure on
        # an over-long/invalid host — it is NOT an OSError, so it would otherwise
        # surface as a 500 (proposal 101, 4b). TimeoutError: resolver too slow
        # (the worker thread is left to finish; only the request is bounded).
        raise HTTPException(status_code=400, detail="Host resolution failed")
    if not infos:
        # Defensive: getaddrinfo raises rather than returning [] in practice, but
        # an empty list would skip the loop below and let an unvalidated host
        # reach the fetch (proposal 101, 4c).
        raise HTTPException(status_code=400, detail="Host resolution failed")
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise HTTPException(status_code=400, detail="Address not allowed")
    cache.put(cache_key, True, ttl=_RESOLVE_TTL)


@router.get(
    "/api/imageproxy",
    include_in_schema=False,
)
async def imageproxy(
    request: Request,
    url: str = Query(..., max_length=_MAX_URL_LENGTH),
):
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Only HTTPS URLs are allowed")

    # SSRF guard: resolve the host and reject any non-globally-routable address.
    # The HTTPS gate above is retained; this block does host/port/IP validation
    # only (do not re-add a scheme check here).
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 443  # urlparse(...).port raises on a malformed port
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid URL")
    if not host:
        raise HTTPException(status_code=400, detail="Invalid URL")
    await _assert_host_is_global(host, port)

    client: httpx.AsyncClient = request.app.state.http_client

    try:
        upstream = await client.send(
            client.build_request("GET", url),
            stream=True,
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=502, detail="Upstream timeout")
    except httpx.HTTPError as exc:
        logger.warning("Image proxy connection error for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail="Upstream connection error")

    # follow_redirects=False (M1) means a host that 301/302s its primary image
    # URL now fails the content-type check below with the same 502 an SVG would.
    # Log redirects distinctly so first-hour deploy monitoring can tell an
    # un-followed-redirect 502 apart from an SVG-rejection 502 (proposal 101, 4d).
    if 300 <= upstream.status_code < 400:
        logger.warning(
            "image proxy got un-followed redirect %s -> %s",
            url, upstream.headers.get("location", ""),
        )

    content_type = upstream.headers.get("content-type", "")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime not in _ALLOWED_CONTENT_TYPES:
        await upstream.aclose()
        raise HTTPException(status_code=502, detail="Unsupported image type")

    content_length = upstream.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            # Malformed Content-Length: ignore the header instead of letting
            # int() raise (which would 500 and leak the open upstream stream).
            # The streaming guard below still enforces the size cap.
            declared_size = None
        if declared_size is not None and declared_size > _MAX_BODY_SIZE:
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
