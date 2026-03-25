"""Tests for misclassification reporting (proposal 044, hardened by 049)."""
import contextlib
import time
from unittest.mock import AsyncMock, patch

import pytest

from project.api.hive_auth import verify_hive_signature
from project.api.routes import reports as _reports_module


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear rate limit state between tests."""
    _reports_module._report_counts.clear()
    yield
    _reports_module._report_counts.clear()


@contextlib.contextmanager
def _report_mocks():
    """Context manager that mocks signature verification and reputation for report tests."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock, return_value=["STM7abc123"]),
        patch("project.api.routes.reports.verify_hive_signature", return_value=True),
        patch("project.api.routes.reports.get_reputation_via_api", new_callable=AsyncMock, return_value=50.0),
    ):
        yield


# ── Submit report ─────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_success(client):
    """Valid report with mocked signature verification returns 201."""
    with _report_mocks():
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
    with _report_mocks():
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
    with _report_mocks():
        resp = await client.post(
            "/api/posts/nobody/no-such-post/report",
            json={"username": "dave", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )

    assert resp.status_code == 404


@pytest.mark.parametrize("payload,desc", [
    ({"username": "INVALID!", "reason": "Wrong", "signature": "1f" + "ab" * 64}, "invalid-username"),
    ({"username": "dave", "reason": "   ", "signature": "1f" + "ab" * 64}, "empty-reason"),
    ({"username": "dave", "reason": "x" * 1001, "signature": "1f" + "ab" * 64}, "reason-too-long"),
], ids=["invalid-username", "empty-reason", "reason-too-long"])
@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_validation_422(client, payload, desc):
    """Invalid input returns 422."""
    resp = await client.post("/api/posts/alice/test-post-one/report", json=payload)
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
    with _report_mocks():
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
    with _report_mocks():
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
    with _report_mocks():
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
    with _report_mocks():
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
    with _report_mocks():
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


@pytest.mark.parametrize("sig,desc", [
    ("aabb", "wrong-length"),
    ("not-hex", "invalid-hex"),
    ("ff" + "00" * 64, "bad-recovery-flag"),
], ids=["wrong-length", "invalid-hex", "bad-recovery-flag"])
def test_verify_hive_signature_rejects_invalid(sig, desc):
    """Invalid signatures return False."""
    assert verify_hive_signature("test", sig, ["STM7abc"]) is False


# ── Reputation check (proposal 049) ──────────────────────────────────────────


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_low_reputation_rejected(client):
    """Reporter with reputation <= 25 is rejected with 403."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock, return_value=["STM7abc123"]),
        patch("project.api.routes.reports.verify_hive_signature", return_value=True),
        patch("project.api.routes.reports.get_reputation_via_api", new_callable=AsyncMock, return_value=20.0),
    ):
        resp = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "lowrep", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient reputation"


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_reputation_none_allowed(client):
    """When reputation API returns None, report is allowed (fail-open)."""
    with (
        patch("project.api.routes.reports.fetch_posting_keys", new_callable=AsyncMock, return_value=["STM7abc123"]),
        patch("project.api.routes.reports.verify_hive_signature", return_value=True),
        patch("project.api.routes.reports.get_reputation_via_api", new_callable=AsyncMock, return_value=None),
    ):
        resp = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "unknownrep", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
    assert resp.status_code == 201


# ── Rate limiting (proposal 049) ─────────────────────────────────────────────


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_rate_limit_exceeded(client):
    """6th report within 60s returns 429."""
    with _report_mocks():
        for i in range(5):
            resp = await client.post(
                f"/api/posts/alice/test-post-one/report",
                json={"username": f"user{i}", "reason": f"Wrong {i}", "signature": "1f" + "ab" * 64},
            )
            # Each is a different reporter on same post — may get 201 or 409
            # We just need to use the same username to hit rate limit.
        # Now use a single user that already has 5 entries.

    # Fill rate limit for a single user.
    _reports_module._report_counts["ratelimited"] = [time.monotonic() for _ in range(5)]

    with _report_mocks():
        resp = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "ratelimited", "reason": "One more", "signature": "1f" + "ab" * 64},
        )
    assert resp.status_code == 429
    assert resp.json()["detail"] == "Rate limit exceeded"


@pytest.mark.usefixtures("seeded_db")
async def test_submit_report_rate_limit_different_users(client):
    """Rate limits are per-user, not global."""
    # Fill rate limit for user "full"
    _reports_module._report_counts["full"] = [time.monotonic() for _ in range(5)]

    # Different user should not be rate limited.
    with _report_mocks():
        resp = await client.post(
            "/api/posts/alice/test-post-one/report",
            json={"username": "fresh", "reason": "Wrong", "signature": "1f" + "ab" * 64},
        )
    assert resp.status_code == 201
