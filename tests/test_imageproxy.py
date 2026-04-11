"""Tests for GET /api/imageproxy endpoint."""
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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
    mock_response.headers = {"content-type": "image/png"}

    async def _astream_bytes():
        yield image_bytes

    mock_response.astream_bytes = _astream_bytes
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
        assert "non-image" in resp.json()["detail"]
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
    mock_response.headers = {"content-type": "image/jpeg"}

    async def _astream_bytes():
        for _ in range(60):  # 60 MB total
            yield chunk

    mock_response.astream_bytes = _astream_bytes
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
