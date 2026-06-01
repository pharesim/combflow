"""Tests for GET /api/imageproxy endpoint."""
import logging
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
async def test_imageproxy_redirect_not_followed(client):
    """A 30x is not followed; its non-image body is rejected and the redirect
    target is never fetched (M1 — follow_redirects=False).

    `send` is wired with a two-element side_effect: the 302 first, then a *valid*
    image. If the code wrongly followed the redirect it would call `send` again,
    get the image, and return 200 — so asserting 502 + call_count==1 genuinely
    proves the second hop is never issued (proposal 101, 4e.5)."""
    redirect_response = MagicMock(spec=httpx.Response)
    redirect_response.status_code = 302
    redirect_response.headers = {"content-type": "text/html", "location": "https://10.0.0.1/x.png"}
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

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        resp = await client.get("/api/imageproxy", params={"url": "https://example.com/redirector"})
        assert resp.status_code == 502
        assert "Unsupported image type" in resp.json()["detail"]
        assert mock_client.send.call_count == 1  # second hop (image_response) never fetched
        _, kwargs = mock_client.send.call_args
        assert kwargs.get("follow_redirects") is False
        assert kwargs.get("stream") is True
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
async def test_imageproxy_redirect_logs_warning(client, caplog):
    """An un-followed 3xx is logged distinctly so deploy monitoring can tell a
    redirect-host 502 apart from an SVG-rejection 502 (proposal 101, 4d)."""
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 301
    mock_response.headers = {"content-type": "text/html", "location": "https://cdn.example.com/real.png"}
    mock_response.aclose = AsyncMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)

    from project.api.main import app
    original_client = getattr(app.state, "http_client", None)
    app.state.http_client = mock_client
    try:
        with caplog.at_level(logging.WARNING, logger="project.api.routes.imageproxy"):
            resp = await client.get("/api/imageproxy", params={"url": "https://example.com/redirector"})
        assert resp.status_code == 502
        assert any("un-followed redirect" in r.getMessage() for r in caplog.records)
    finally:
        if original_client is not None:
            app.state.http_client = original_client
