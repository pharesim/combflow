"""Tests for GET /api/imageproxy endpoint."""
import socket

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _allow_public_dns():
    """Resolve every test's host to a fixed public IP so the SSRF guard passes
    without touching the network. Tests that need a different resolution
    (private IP, gaierror, IPv6, ...) override this with their own patch()."""
    addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.20.23.154", 443))]
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        yield


@pytest.mark.asyncio
async def test_imageproxy_missing_url(client):
    resp = await client.get("/api/imageproxy")
    assert resp.status_code == 422  # FastAPI validation error


@pytest.mark.asyncio
async def test_imageproxy_non_https_url(client):
    resp = await client.get("/api/imageproxy", params={"url": "http://example.com/img.png"})
    assert resp.status_code == 400
    assert "HTTPS" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_imageproxy_url_too_long(client):
    long_url = "https://example.com/" + "a" * 2100
    resp = await client.get("/api/imageproxy", params={"url": long_url})
    assert resp.status_code == 422  # FastAPI max_length validation


@pytest.mark.asyncio
async def test_imageproxy_success(client):
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/png"}

    async def _aiter_bytes():
        yield image_bytes

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://images.hive.blog/u/alice/avatar"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["cache-control"] == "public, max-age=86400"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.content == image_bytes
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_non_image_content_type(client):
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/page.html"})
        assert resp.status_code == 502
        assert "Unsupported image type" in resp.json()["detail"]
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_upstream_timeout(client):
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://slow.example.com/big.jpg"})
        assert resp.status_code == 502
        assert "timeout" in resp.json()["detail"].lower()
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_upstream_connection_error(client):
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://down.example.com/img.jpg"})
        assert resp.status_code == 502
        assert "connection" in resp.json()["detail"].lower()
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_content_length_too_large(client):
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/jpeg", "content-length": str(60 * 1024 * 1024)}
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/huge.jpg"})
        assert resp.status_code == 413
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_streaming_size_limit(client):
    """Verify streaming aborts when cumulative size exceeds limit."""
    chunk = b"\x00" * (1024 * 1024)  # 1 MB chunks

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/jpeg"}

    async def _aiter_bytes():
        for _ in range(60):  # 60 MB total
            yield chunk

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/huge-stream.jpg"})
        # Response starts streaming successfully but stops at 50 MB
        assert resp.status_code == 200
        assert len(resp.content) <= 50 * 1024 * 1024
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_svg_rejected(client):
    """image/svg+xml is rejected by the raster allowlist (H1 stored XSS)."""
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/svg+xml"}
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/evil.svg"})
        assert resp.status_code == 502
        assert "Unsupported image type" in resp.json()["detail"]
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_redirect_to_private_host_rejected(client):
    """A 30x to an internal address is rejected by re-running the SSRF guard on
    the redirect target — the redirect-based SSRF bypass that kept httpx's own
    follow_redirects OFF (proposal 101, M1). httpx never chases the hop itself;
    the proxy validates the Location host and refuses before any second fetch.

    `send` is wired with a two-element side_effect: the 302 first, then a *valid*
    image. Asserting 400 + call_count==1 proves the private target is rejected
    pre-fetch (the image hop is never issued)."""
    redirect_response = MagicMock(spec=httpx.Response)
    redirect_response.status_code = 302
    redirect_response.headers = {"content-type": "text/html", "location": "https://internal.invalid/x.png"}
    redirect_response.aclose = AsyncMock()

    image_response = MagicMock(spec=httpx.Response)
    image_response.status_code = 200
    image_response.headers = {"content-type": "image/png"}

    async def _aiter_bytes():
        yield b"\x89PNG\r\n\x1a\n"

    image_response.aiter_bytes = _aiter_bytes
    image_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(side_effect=[redirect_response, image_response])

    # Initial host resolves public; the redirect target resolves to RFC1918.
    def _resolve(host, *a, **k):
        if host == "internal.invalid":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.20.23.154", 443))]

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        with patch("project.api.routes.imageproxy.socket.getaddrinfo", side_effect=_resolve):
            resp = await client.get("/api/imageproxy", params={"url": "https://example.com/redirector"})
        assert resp.status_code == 400
        assert "Address not allowed" in resp.json()["detail"]
        assert mock_client.send.call_count == 1  # image hop never fetched
        _, kwargs = mock_client.send.call_args
        assert kwargs.get("follow_redirects") is False
        assert kwargs.get("stream") is True
        redirect_response.aclose.assert_awaited()  # 302 stream closed before refusal
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_build_request_invalid_url(client):
    """A host that passes the SSRF guard (urlparse + getaddrinfo) but httpx's
    stricter IDNA encoder rejects at build_request must fail closed with 400, not
    surface as a 500. httpx.InvalidURL is NOT an httpx.HTTPError subclass, so it
    would otherwise escape the per-hop try/except (regression guard)."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.side_effect = httpx.InvalidURL("malformed host")
    mock_client.send = AsyncMock()

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/x.png"})
        assert resp.status_code == 400
        assert "Invalid URL" in resp.json()["detail"]
        assert mock_client.send.call_count == 0  # never reached the network
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_redirect_to_non_https_rejected(client):
    """A 30x downgrading to http:// (or any non-HTTPS scheme) is refused, so a
    redirect can't smuggle the request off the HTTPS-only contract."""
    redirect_response = MagicMock(spec=httpx.Response)
    redirect_response.status_code = 302
    redirect_response.headers = {"content-type": "text/html", "location": "http://cdn.example.com/real.png"}
    redirect_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=redirect_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/redirector"})
        assert resp.status_code == 400
        assert "HTTPS" in resp.json()["detail"]
        assert mock_client.send.call_count == 1
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_redirect_chain_limit(client):
    """A redirect loop is bounded: after _MAX_REDIRECTS hops the proxy gives up
    with a 502 rather than chasing forever."""
    from project.api.routes import imageproxy as ip

    def _make_redirect():
        r = MagicMock(spec=httpx.Response)
        r.status_code = 302
        r.headers = {"content-type": "text/html", "location": "https://cdn.example.com/next.png"}
        r.aclose = AsyncMock()
        return r

    # One initial fetch + _MAX_REDIRECTS follows = _MAX_REDIRECTS + 1 sends, all 302.
    hops = [_make_redirect() for _ in range(ip._MAX_REDIRECTS + 1)]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(side_effect=hops)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/loop"})
        assert resp.status_code == 502
        assert "Too many redirects" in resp.json()["detail"]
        assert mock_client.send.call_count == ip._MAX_REDIRECTS + 1
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_private_host_rejected(client):
    """A host resolving to loopback is rejected before any fetch (SSRF guard)."""
    addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        resp = await client.get("/api/imageproxy", params={"url": "https://internal.example.com/x.png"})
    assert resp.status_code == 400
    assert "Address not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime",
    ["image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/avif"],
)
async def test_imageproxy_allowlist_success(client, mime):
    """Every allowlisted raster type streams through; the public-IP autouse
    fixture confirms the SSRF filter doesn't reject legitimate traffic."""
    image_bytes = b"\x00" * 64

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": mime}

    async def _aiter_bytes():
        yield image_bytes

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://images.hive.blog/x.img"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == mime
        assert resp.content == image_bytes
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_unresolvable_host(client):
    """A host that fails DNS resolution returns 400, not 502."""
    with patch(
        "project.api.routes.imageproxy.socket.getaddrinfo",
        side_effect=socket.gaierror("name or service not known"),
    ):
        resp = await client.get("/api/imageproxy", params={"url": "https://nonexistent.invalid/x.png"})
    assert resp.status_code == 400
    assert "Host resolution failed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_imageproxy_metadata_ip_rejected(client):
    """The 169.254.169.254 cloud-metadata IP (link-local) is rejected."""
    addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        resp = await client.get("/api/imageproxy", params={"url": "https://metadata.example.com/x.png"})
    assert resp.status_code == 400
    assert "Address not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_imageproxy_cgnat_rejected(client):
    """CGNAT 100.64.0.0/10 is rejected — locks the `not is_global` fix; the old
    is_private/is_loopback/... disjunction would have let this through."""
    addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))]
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        resp = await client.get("/api/imageproxy", params={"url": "https://cgnat.example.com/x.png"})
    assert resp.status_code == 400
    assert "Address not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_imageproxy_ipv4_mapped_ipv6_rejected(client):
    """An IPv4-mapped IPv6 address pointing at loopback is rejected (locks
    mapped-address handling against Python-version drift)."""
    addr = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0))]
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        resp = await client.get("/api/imageproxy", params={"url": "https://mapped.example.com/x.png"})
    assert resp.status_code == 400
    assert "Address not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_imageproxy_malformed_port_rejected(client):
    """A malformed port makes urlparse(...).port raise ValueError → 400. Covers
    the `except ValueError` branch the SSRF guard introduced."""
    resp = await client.get("/api/imageproxy", params={"url": "https://example.com:99999/x.png"})
    assert resp.status_code == 400
    assert "Invalid URL" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_imageproxy_ipv4_mapped_public_allowed(client):
    """An IPv4-mapped IPv6 address pointing at a *public* IP is allowed (locks
    the spec's "do not OR is_global with is_reserved" constraint — only the
    reject side was covered before; a future is_reserved re-add would silently
    break dual-stack traffic with no failing test). (proposal 101, 4e.1)"""
    addr = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:8.8.8.8", 443, 0, 0))]
    image_bytes = b"\x00" * 32

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/png"}

    async def _aiter_bytes():
        yield image_bytes

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        try:
            resp = await client.get("/api/imageproxy", params={"url": "https://dualstack.example.com/x.png"})
            assert resp.status_code == 200
            assert resp.content == image_bytes
        finally:
            if original_client is not None:
                app.state.http_client = original_client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ctype",
    [
        "image/svg+xml",
        "image/svg+xml; charset=utf-8",
        "IMAGE/SVG+XML",
        "text/html; charset=utf-8",
    ],
)
async def test_imageproxy_content_type_normalization_rejects(client, ctype):
    """Content-type is normalized via split(";")[0].strip().lower() before the
    allowlist check, so charset params and casing don't smuggle a banned type
    through (proposal 101, 4e.3)."""
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": ctype}
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/x"})
        assert resp.status_code == 502
        assert "Unsupported image type" in resp.json()["detail"]
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_mixed_records_rejected(client):
    """When getaddrinfo returns multiple records and ANY is private, the host is
    rejected — the loop does not short-circuit on the leading public record
    (proposal 101, 4e.4)."""
    addr = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ]
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        resp = await client.get("/api/imageproxy", params={"url": "https://mixed.example.com/x.png"})
    assert resp.status_code == 400
    assert "Address not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_imageproxy_resolution_cached(client):
    """A second request for the same host hits the DNS cache — getaddrinfo runs
    once, killing the page-load burst case (proposal 101, 4a)."""
    addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.20.23.154", 443))]

    def _make_image_response():
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.headers = {"content-type": "image/png"}

        async def _aiter_bytes():
            yield b"\x00" * 16

        r.aiter_bytes = _aiter_bytes
        r.aclose = AsyncMock()
        return r

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(side_effect=[_make_image_response(), _make_image_response()])

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    # Override the autouse fixture so we own the mock and can count its calls.
    with patch(
        "project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr
    ) as mock_getaddrinfo:
        try:
            # Same host, different paths — only the host:port is resolved/cached.
            # Host is unique to this test so no other test's success path can
            # pre-seed imgproxy_dns:<host>:443: the call_count==1 proof must not
            # depend on the autouse cache.clear() fixture for correctness.
            r1 = await client.get("/api/imageproxy", params={"url": "https://cache-probe.example.com/a.png"})
            r2 = await client.get("/api/imageproxy", params={"url": "https://cache-probe.example.com/b.png"})
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert mock_getaddrinfo.call_count == 1  # second request served from cache
        finally:
            if original_client is not None:
                app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_malformed_content_length_ignored(client):
    """A non-numeric Content-Length is ignored rather than raising (which would
    500 and leak the open upstream stream); the streaming size guard still
    applies (proposal 101, pre-existing follow-up)."""
    image_bytes = b"\x00" * 32

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/png", "content-length": "not-a-number"}

    async def _aiter_bytes():
        yield image_bytes

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/x.png"})
        assert resp.status_code == 200
        assert resp.content == image_bytes
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_relative_redirect_followed_to_image(client):
    """The real-world avatar case: images.hive.blog/u/<acct>/avatar answers 302
    with a *relative* Location to the stored image. The proxy resolves it against
    the request URL (same host), re-validates, fetches the target, and streams the
    image back — restoring the no-thumbnail fallback avatar in every grid view."""
    redirect_response = MagicMock(spec=httpx.Response)
    redirect_response.status_code = 302
    redirect_response.headers = {"content-type": "text/html", "location": "/p/abc123?width=128&height=128"}
    redirect_response.aclose = AsyncMock()

    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    image_response = MagicMock(spec=httpx.Response)
    image_response.status_code = 200
    image_response.headers = {"content-type": "image/webp"}

    async def _aiter_bytes():
        yield image_bytes

    image_response.aiter_bytes = _aiter_bytes
    image_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(side_effect=[redirect_response, image_response])

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get(
            "/api/imageproxy", params={"url": "https://images.hive.blog/u/alice/avatar"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/webp"
        assert resp.content == image_bytes
        assert mock_client.send.call_count == 2  # 302, then the resolved image
        # Second hop fetched the Location resolved against the original host.
        second_url = mock_client.build_request.call_args_list[1].args[1]
        assert second_url == "https://images.hive.blog/p/abc123?width=128&height=128"
        redirect_response.aclose.assert_awaited()  # 302 stream closed before refetch
    finally:
        if original_client is not None:
            app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_unparseable_resolved_address(client):
    """A resolver returning a non-numeric sockaddr[0] fails closed with 400, not
    500 (proposal 105a). Not reachable via a normal Linux resolver (numeric IP
    strings only; scope ids live in sockaddr[3]), but the SSRF guard must reject
    what ipaddress can't classify rather than let ValueError surface as a 500."""
    addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("garbage", 443))]
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        resp = await client.get("/api/imageproxy", params={"url": "https://weird.example.com/x.png"})
    assert resp.status_code == 400
    assert "Host resolution failed" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved_ip", [
    "::ffff:100.64.0.1",   # IPv4-mapped CGNAT
    "64:ff9b::ac13:4",     # NAT64 of 172.19.0.4 (RFC1918)
    "::127.0.0.1",         # IPv4-compatible (deprecated ::/96) loopback
])
async def test_imageproxy_embedded_ipv4_private_rejected(client, resolved_ip):
    """The three embedded-IPv4 IPv6 forms whose embedded v4 is private all report
    is_global=True at the IPv6 layer but route to a non-global IPv4 — `_ip_allowed`
    unwraps the embedded v4 and rejects them (proposal 105b + the IPv4-compatible
    completion). A mock http_client is installed so that if the filter were
    reverted/widened the request would reach the fetch and return 200, giving a
    crisp `400 != 200` rather than an incidental AttributeError. The reject must
    happen BEFORE any fetch and must NOT poison the validated-host cache."""
    from project.api.routes import imageproxy

    image_response = MagicMock(spec=httpx.Response)
    image_response.status_code = 200
    image_response.headers = {"content-type": "image/png"}

    async def _aiter_bytes():
        yield b"\x89PNG\r\n\x1a\n"

    image_response.aiter_bytes = _aiter_bytes
    image_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=image_response)

    addr = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (resolved_ip, 443, 0, 0))]
    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        try:
            resp = await client.get("/api/imageproxy", params={"url": "https://embed.example.com/x.png"})
            assert resp.status_code == 400  # would be 200 if _ip_allowed wrongly permitted it
            assert "Address not allowed" in resp.json()["detail"]
            mock_client.send.assert_not_called()  # rejected before any upstream fetch
            assert ("embed.example.com", 443) not in imageproxy._resolve_cache  # reject ⇒ not cached
        finally:
            if original_client is not None:
                app.state.http_client = original_client


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved_ip", [
    "64:ff9b::808:808",   # NAT64 of 8.8.8.8 (public) — locks the int(ip)&0xFFFFFFFF derivation
    "::8.8.8.8",          # IPv4-compatible of 8.8.8.8 (public)
])
async def test_imageproxy_embedded_ipv4_public_allowed(client, resolved_ip):
    """The embedded-IPv4 unwrap must ALLOW forms whose embedded v4 is public — a
    derivation-mask bug (or an over-broad reject) would otherwise silently block
    legitimate NAT64/dual-stack traffic with no failing test, since the reject-side
    tests can't catch an allow-side regression. Complements the mapped-public
    `::ffff:8.8.8.8` allow test (proposal 105b allow-side coverage)."""
    addr = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (resolved_ip, 443, 0, 0))]
    image_bytes = b"\x00" * 16

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/png"}

    async def _aiter_bytes():
        yield image_bytes

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        try:
            resp = await client.get("/api/imageproxy", params={"url": "https://embedpub.example.com/x.png"})
            assert resp.status_code == 200
            assert resp.content == image_bytes
        finally:
            if original_client is not None:
                app.state.http_client = original_client


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved_ip", [
    "::ffff:0:7f00:1",   # IPv4-translated/SIIT of 127.0.0.1 (loopback)
    "::ffff:0:a13:6",    # IPv4-translated/SIIT of 10.19.0.6 (RFC1918)
])
async def test_imageproxy_siit_translated_ipv4_private_rejected(client, resolved_ip):
    """The IPv4-translated/SIIT form ``::ffff:0:0:0/96`` (RFC 8215) — the fourth
    embedded-IPv4 IPv6 representation — reports is_global=True at the IPv6 layer
    but routes to a non-global IPv4. `_ip_allowed` unwraps the low 32 bits and
    rejects a private/loopback embed (proposal 109; completes the 105b symmetry
    claim of handling "all embedded-IPv4 representations"). A mock http_client is
    installed so a reverted/widened filter would reach the fetch and return 200,
    giving a crisp `400 != 200` rather than an incidental AttributeError. The
    reject must happen BEFORE any fetch and must NOT poison the validated-host
    cache.

    Mutation check: dropping ``or ip in _V4_TRANSLATED`` from `_ip_allowed` turns
    this red (200 instead of 400)."""
    from project.api.routes import imageproxy

    image_response = MagicMock(spec=httpx.Response)
    image_response.status_code = 200
    image_response.headers = {"content-type": "image/png"}

    async def _aiter_bytes():
        yield b"\x89PNG\r\n\x1a\n"

    image_response.aiter_bytes = _aiter_bytes
    image_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=image_response)

    addr = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (resolved_ip, 443, 0, 0))]
    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        try:
            resp = await client.get("/api/imageproxy", params={"url": "https://siit.example.com/x.png"})
            assert resp.status_code == 400  # would be 200 if _ip_allowed wrongly permitted it
            assert "Address not allowed" in resp.json()["detail"]
            mock_client.send.assert_not_called()  # rejected before any upstream fetch
            assert ("siit.example.com", 443) not in imageproxy._resolve_cache  # reject ⇒ not cached
        finally:
            if original_client is not None:
                app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_siit_translated_ipv4_public_allowed(client):
    """The SIIT unwrap must ALLOW a form whose embedded v4 is public — locks the
    ``int(ip) & 0xFFFFFFFF`` derivation on the allow side so an over-broad reject
    (or a bad mask) can't silently block legitimate SIIT/dual-stack traffic with
    no failing test (proposal 109 allow-side coverage). ``::ffff:0:808:808`` is
    the IPv4-translated form of public 8.8.8.8."""
    addr = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:0:808:808", 443, 0, 0))]
    image_bytes = b"\x00" * 16

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/png"}

    async def _aiter_bytes():
        yield image_bytes

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    with patch("project.api.routes.imageproxy.socket.getaddrinfo", return_value=addr):
        try:
            resp = await client.get("/api/imageproxy", params={"url": "https://siitpub.example.com/x.png"})
            assert resp.status_code == 200
            assert resp.content == image_bytes
        finally:
            if original_client is not None:
                app.state.http_client = original_client


@pytest.mark.asyncio
async def test_imageproxy_serves_normalized_mime(client):
    """The 200 path serves the normalized essence type (`mime`), stripping
    attacker-controlled content-type parameter junk / leading whitespace / casing
    rather than echoing the raw upstream header (proposal 105d)."""
    image_bytes = b"\x89PNG\r\n\x1a\n"

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "IMAGE/PNG; charset=utf-8 "}

    async def _aiter_bytes():
        yield image_bytes

    mock_response.aiter_bytes = _aiter_bytes
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/x.png"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"  # not the raw "IMAGE/PNG; charset=utf-8 "
        assert resp.content == image_bytes
    finally:
        if original_client is not None:
            app.state.http_client = original_client


def test_resolve_cache_eviction():
    """The validated-host cache is bounded — past `_RESOLVE_CACHE_MAX` entries the
    oldest are evicted FIFO, so attacker-driven distinct-host churn can't grow it
    without bound (proposal 105c). Replaces 101's unbounded shared-cache keys."""
    from project.api.routes import imageproxy

    imageproxy._reset_resolve_cache()
    try:
        for i in range(imageproxy._RESOLVE_CACHE_MAX + 50):
            imageproxy._resolve_cache_put((f"h{i}.example.com", 443))
        assert len(imageproxy._resolve_cache) == imageproxy._RESOLVE_CACHE_MAX
        # Earliest insertions evicted; the most recent are retained.
        assert imageproxy._resolve_cache_get(("h0.example.com", 443)) is False
        last = imageproxy._RESOLVE_CACHE_MAX + 49
        assert imageproxy._resolve_cache_get((f"h{last}.example.com", 443)) is True
    finally:
        imageproxy._reset_resolve_cache()


def test_resolve_cache_expiry():
    """A validated-host entry past its TTL is treated as a miss and lazily
    dropped from the cache (proposal 105c)."""
    import time as _time

    from project.api.routes import imageproxy

    imageproxy._reset_resolve_cache()
    try:
        # Seed an already-expired entry directly (expires_at in the past).
        imageproxy._resolve_cache[("stale.example.com", 443)] = _time.monotonic() - 1
        assert imageproxy._resolve_cache_get(("stale.example.com", 443)) is False
        assert ("stale.example.com", 443) not in imageproxy._resolve_cache
        # A fresh put is a hit.
        imageproxy._resolve_cache_put(("fresh.example.com", 443))
        assert imageproxy._resolve_cache_get(("fresh.example.com", 443)) is True
    finally:
        imageproxy._reset_resolve_cache()
