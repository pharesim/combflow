"""Image proxy endpoint — privacy-preserving server-side image fetching."""
import asyncio
import functools
import ipaddress
import logging
import socket
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

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
_RESOLVE_TTL = 60.0          # seconds — cache a validated host resolution
_RESOLVE_TIMEOUT = 5.0       # seconds — cap per-request DNS latency
_RESOLVE_CACHE_MAX = 4096    # max validated-host entries (proposal 105c)
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")  # NAT64 well-known prefix
_V4_COMPAT = ipaddress.ip_network("::/96")         # deprecated IPv4-compatible IPv6
_resolver_executor = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="imgproxy-dns"
)

# Validated-host cache. Proposal 101 keyed this into the shared, unbounded
# ``project.cache``, mixing an attacker-influenced key (the request's hostname)
# with precious, expensive-to-recompute entries (overview stats, language
# names). Sustained distinct-host churn could both grow the shared store and,
# because that store never evicts, never reclaim it (proposal 105c). A dedicated
# bounded OrderedDict isolates the churn: FIFO eviction past ``_RESOLVE_CACHE_MAX``
# caps the footprint, the TTL bounds staleness, and the shared cache no longer
# carries request-derived keys. Mirrors the worker's bounded community/blacklist
# caches. Single-threaded event-loop access only (no await inside the helpers),
# so plain dict ops are race-free.
_resolve_cache: "OrderedDict[tuple[str, int], float]" = OrderedDict()


def _reset_resolve_cache() -> None:
    """Clear the validated-host cache. Test-only (see tests/conftest.py)."""
    _resolve_cache.clear()


def _resolve_cache_get(key: tuple[str, int]) -> bool:
    """True iff ``key`` is a still-valid validated-host entry (lazy-expiring)."""
    expires_at = _resolve_cache.get(key)
    if expires_at is None:
        return False
    if expires_at <= time.monotonic():
        del _resolve_cache[key]
        return False
    _resolve_cache.move_to_end(key)
    return True


def _resolve_cache_put(key: tuple[str, int]) -> None:
    """Record ``key`` as validated for ``_RESOLVE_TTL``; evict oldest past cap."""
    _resolve_cache[key] = time.monotonic() + _RESOLVE_TTL
    _resolve_cache.move_to_end(key)
    while len(_resolve_cache) > _RESOLVE_CACHE_MAX:
        _resolve_cache.popitem(last=False)


def _ip_allowed(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True iff ``ip`` is safe to fetch: globally routable, and — for the three
    IPv6 forms that embed an IPv4 — whose embedded IPv4 is *also* globally
    routable. The embedded forms unwrapped here are:

    - IPv4-mapped       ``::ffff:0:0/96``  (``ip.ipv4_mapped``)
    - NAT64 well-known  ``64:ff9b::/96``   (low 32 bits)
    - IPv4-compatible   ``::/96``          (low 32 bits; deprecated RFC4291)

    ``is_global`` alone is necessary but not sufficient: on CPython 3.12 the
    mapped form ``::ffff:100.64.0.1``, the NAT64 form ``64:ff9b::ac13:4``, and
    the IPv4-compatible form ``::127.0.0.1`` all report ``is_global=True`` while
    the address they actually route to (100.64.0.0/10 CGNAT, 172.19.0.4 RFC1918,
    127.0.0.1 loopback) is non-global — so they would slip a bare ``is_global``
    filter (proposal 105b, extended to the IPv4-compatible form for completeness:
    all three embedded-IPv4 representations are now handled symmetrically). No
    live reach in the single-bridge topology (no CGNAT/NAT64 interface, no IPv6
    default route to auto-tunnel the embedded v4), so this is defense-in-depth;
    the tests lock it so a future ``is_global`` change can't silently widen reach.
    6to4 ``2002::/16`` and Teredo ``2001::/32`` need no special-casing — CPython
    already reports them ``is_global=False``.
    """
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped
        if embedded is None and (ip in _NAT64_WKP or ip in _V4_COMPAT):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not embedded.is_global:
            return False
    return True


async def _assert_host_is_global(host: str, port: int) -> None:
    """Resolve ``host:port`` and reject any address that isn't safe to fetch.

    SSRF guard (proposal 101, H2): rejects loopback, RFC1918, link-local
    (including the 169.254.169.254 cloud-metadata IP), ULA, CGNAT 100.64.0.0/10,
    and unspecified addresses. The per-record predicate is ``_ip_allowed``, which
    is strictly stronger than ``not ip.is_global``: besides catching CGNAT (which
    is_private/is_loopback/is_link_local/is_reserved/is_multicast/is_unspecified
    all miss), it unwraps the three embedded-IPv4 IPv6 forms (IPv4-mapped, NAT64,
    IPv4-compatible) and rejects those whose embedded IPv4 is non-global — forms a
    bare ``is_global`` reports as global (proposal 105b).

    Raises ``HTTPException(400)`` on resolution failure, an unparseable resolved
    address (fail-closed — proposal 105a), or a disallowed address. A successful
    validation is cached on ``(host, port)`` for ``_RESOLVE_TTL`` in a bounded
    OrderedDict so a burst of fetches from one host resolves it once; resolution
    runs on a dedicated thread pool, bounded by ``_RESOLVE_TIMEOUT``.
    """
    cache_key = (host, port)
    if _resolve_cache_get(cache_key):
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
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            # Fail closed (proposal 105a): a resolver returning a non-numeric
            # sockaddr[0] is a quirk we don't trust. Not attacker-reachable on a
            # normal Linux resolver (numeric IP strings only; scope ids live in
            # sockaddr[3]), but a hardened SSRF guard must reject what it can't
            # classify rather than let ValueError surface as a 500.
            raise HTTPException(status_code=400, detail="Host resolution failed")
        if not _ip_allowed(ip):
            raise HTTPException(status_code=400, detail="Address not allowed")
    _resolve_cache_put(cache_key)


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
        # Serve the normalized essence type, not the raw upstream header, so
        # attacker-controlled content-type parameter junk / leading whitespace /
        # casing never reaches the response (proposal 105d). `nosniff` already
        # pins it, but `mime` is the value we actually validated above.
        media_type=mime,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
