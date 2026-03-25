"""Tests for misclassification reporting (proposal 044)."""
from unittest.mock import AsyncMock, patch

import pytest


# ── Submit report ─────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_success(client):
    """Valid report with mocked signature verification returns 201."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        resp = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={
                "username": "dave",
                "reason": "This post is about photography, not technology",
                "signature": "1f" + "ab" * 64,
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["reporter"] == "dave"
    assert data["reason"] == "This post is about photography, not technology"
    assert "id" in data
    assert "created_at" in data


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_invalid_signature(client):
    """Invalid signature returns 403."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = False

        resp = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={
                "username": "dave",
                "reason": "Wrong category",
                "signature": "deadbeef",
            },
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Signature verification failed"


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_no_posting_keys(client):
    """User with no posting keys returns 403."""
    with patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys:
        mock_keys.return_value = []

        resp = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={
                "username": "dave",
                "reason": "Wrong category",
                "signature": "deadbeef",
            },
        )

    assert resp.status_code == 403


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_duplicate(client):
    """Second report by same user on same post returns 409."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        resp1 = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "dave", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
        assert resp1.status_code == 201

        resp2 = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "dave", "reason": "Still wrong", "signature": "1f" + "cd" * 64},
        )

    assert resp2.status_code == 409
    assert resp2.json()["detail"] == "You have already reported this post"


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_nonexistent_post(client):
    """Report on a post that doesn't exist returns 404."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        resp = await client.post(
            "/api/posts/nobody/no-such-post/report",
            json={"username": "dave", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )

    assert resp.status_code == 404


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_invalid_username(client):
    """Invalid username format returns 422."""
    resp = await client.post(
        "/api/posts/alice/test-post-one/report",
        json={"username": "INVALID!", "reason": "Wrong", "signature": "1f" + "ab" * 64},
    )
    assert resp.status_code == 422


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_empty_reason(client):
    """Empty reason returns 422."""
    resp = await client.post(
        "/api/posts/alice/test-post-one/report",
        json={"username": "dave", "reason": "   ", "signature": "1f" + "ab" * 64},
    )
    assert resp.status_code == 422


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_reason_too_long(client):
    """Reason over 1000 chars returns 422."""
    resp = await client.post(
        "/api/posts/alice/test-post-one/report",
        json={"username": "dave", "reason": "x" * 1001, "signature": "1f" + "ab" * 64},
    )
    assert resp.status_code == 422


# ── List reports ──────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("seeded_db")
async def test_list_reports_empty(client):
    """No reports returns empty list."""
    resp = await client.get("/api/reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reports"] == []
    assert data["total"] == 0


@pytest.mark.usefixtures("seeded_db")
async def test_list_reports_with_data(client):
    """Reports are returned with post context."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "dave", "reason": "Wrong category", "signature": "1f" + "ab" * 64},
        )

    resp = await client.get("/api/reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    report = data["reports"][0]
    assert report["reporter"] == "dave"
    assert report["reason"] == "Wrong category"
    assert report["post"]["author"] == "alice"
    assert report["post"]["permlink"] == "test-post-one"
    assert isinstance(report["post"]["categories"], list)


@pytest.mark.usefixtures("seeded_db")
async def test_list_reports_filter_by_reporter(client):
    """Filter reports by reporter username."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "dave", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
        await client.post(
            "/api/posts/bob/test-post-two/report",
            json={"username": "eve", "reason": "Also wrong", "signature": "1f" + "cd" * 64},
        )

    resp = await client.get("/api/reports?reporter=dave")
    data = resp.json()
    assert data["total"] == 1
    assert data["reports"][0]["reporter"] == "dave"


@pytest.mark.usefixtures("seeded_db")
async def test_list_reports_filter_by_post(client):
    """Filter reports by post author and permlink."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "dave", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
        await client.post(
            "/api/posts/bob/test-post-two/report",
            json={"username": "dave", "reason": "Also wrong", "signature": "1f" + "cd" * 64},
        )

    resp = await client.get("/api/reports?post_author=alice&post_permlink=test-post-one")
    data = resp.json()
    assert data["total"] == 1
    assert data["reports"][0]["post"]["author"] == "alice"


@pytest.mark.usefixtures("seeded_db")
async def test_list_reports_pagination(client):
    """Pagination limits and offsets work."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "dave", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
        await client.post(
            "/api/posts/bob/test-post-two/report",
            json={"username": "eve", "reason": "Also wrong", "signature": "1f" + "cd" * 64},
        )

    resp = await client.get("/api/reports?limit=1&offset=0")
    data = resp.json()
    assert data["total"] == 2
    assert len(data["reports"]) == 1
    assert data["limit"] == 1
    assert data["offset"] == 0

    resp2 = await client.get("/api/reports?limit=1&offset=1")
    data2 = resp2.json()
    assert data2["total"] == 2
    assert len(data2["reports"]) == 1
    assert data2["reports"][0]["reporter"] != data["reports"][0]["reporter"]


# ── Different users can report same post ──────────────────────────────────────


@pytest.mark.usefixtures("seeded_db")
async def test_different_users_report_same_post(client):
    """Multiple users can report the same post."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock) as mock_keys,
        patch("project.api.routes.reports.verify_hive_signature") as mock_verify,
    ):
        mock_keys.return_value = ["STM7abc123"]
        mock_verify.return_value = True

        resp1 = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "dave", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
        assert resp1.status_code == 201

        resp2 = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "eve", "reason": "Also wrong", "signature": "1f" + "cd" * 64},
        )
        assert resp2.status_code == 201

    resp = await client.get("/api/reports?post_author=alice&post_permlink=test-post-one")
    assert resp.json()["total"] == 2


# ── Signature verification unit tests ─────────────────────────────────────────


def test_verify_hive_signature_wrong_length():
    """Signature with wrong byte count returns False."""
    from project.api.hive_auth import verify_hive_signature
    assert verify_hive_signature("test", "aabb", ["STM7abc"]) is False


def test_verify_hive_signature_invalid_hex():
    """Non-hex signature returns False."""
    from project.api.hive_auth import verify_hive_signature
    assert verify_hive_signature("test", "not-hex", ["STM7abc"]) is False


def test_verify_hive_signature_bad_recovery_flag():
    """Invalid recovery flag returns False."""
    from project.api.hive_auth import verify_hive_signature
    sig = "ff" + "00" * 64  # recovery flag 0xff is invalid
    assert verify_hive_signature("test", sig, ["STM7abc"]) is False
