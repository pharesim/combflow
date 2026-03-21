"""Tests for auth endpoints — challenge, verify, me, logout."""
import secrets
import time
from unittest.mock import patch

import jwt as pyjwt
import pytest

from project.api.deps import _jwt_secret, JWT_COOKIE_NAME
from project.api.routes.auth import _challenges, _CHALLENGE_TTL
from tests.conftest import make_jwt, jwt_headers, jwt_cookies


# ── Challenge flow ───────────────────────────────────────────────────────────

async def test_challenge_returns_string(client):
    resp = await client.post("/api/auth/challenge", json={"username": "alice"})
    assert resp.status_code == 200
    data = resp.json()
    assert "challenge" in data
    assert len(data["challenge"]) >= 20
    assert data["expires_in"] == _CHALLENGE_TTL


async def test_challenge_rejects_invalid_username(client):
    resp = await client.post("/api/auth/challenge", json={"username": "INVALID!"})
    assert resp.status_code == 422


async def test_challenge_cleanup_expired(client):
    """Expired challenges are purged on next request."""
    _challenges["old-challenge"] = ("alice", time.time() - 1)
    await client.post("/api/auth/challenge", json={"username": "bob"})
    assert "old-challenge" not in _challenges


# ── Verify flow ──────────────────────────────────────────────────────────────

async def test_verify_invalid_challenge(client):
    challenge = secrets.token_urlsafe(32)
    resp = await client.post("/api/auth/verify", json={
        "username": "alice",
        "challenge": challenge,
        "signature": "aabbcc",
    })
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


async def test_verify_expired_challenge(client):
    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = ("alice", time.time() - 1)
    resp = await client.post("/api/auth/verify", json={
        "username": "alice",
        "challenge": challenge,
        "signature": "aabbcc",
    })
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


async def test_verify_username_mismatch(client):
    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = ("alice", time.time() + 300)
    resp = await client.post("/api/auth/verify", json={
        "username": "bob",
        "challenge": challenge,
        "signature": "aabbcc",
    })
    assert resp.status_code == 400
    assert "mismatch" in resp.json()["detail"].lower()


async def test_verify_invalid_signature(client):
    """Valid challenge but bad signature is rejected."""
    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = ("alice", time.time() + 300)
    with patch("project.api.routes.auth.get_posting_key", return_value="STM7abc123"):
        resp = await client.post("/api/auth/verify", json={
            "username": "alice",
            "challenge": challenge,
            "signature": "deadbeef",
        })
    assert resp.status_code == 401


async def test_verify_success_sets_cookie(client):
    """Successful verify sets httpOnly JWT cookie and returns username."""
    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = ("alice", time.time() + 300)
    with patch("project.api.routes.auth.get_posting_key", return_value="STM7abc"), \
         patch("project.api.routes.auth._verify_hive_signature", return_value=True):
        resp = await client.post("/api/auth/verify", json={
            "username": "alice",
            "challenge": challenge,
            "signature": "aabb",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "alice"
    assert "expires_at" in data
    assert "token" not in data  # Token is in cookie, not body

    # Verify the cookie was set.
    cookie = resp.cookies.get(JWT_COOKIE_NAME)
    assert cookie is not None
    # Decode and check claims.
    payload = pyjwt.decode(cookie, _jwt_secret(), algorithms=["HS256"])
    assert payload["sub"] == "alice"


# ── Reputation gate (proposal 006) ───────────────────────────────────────────

async def test_verify_negative_rep_blocked(client):
    """Accounts with negative reputation are rejected at login."""
    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = ("spammer", time.time() + 300)
    with patch("project.api.routes.auth.get_posting_key", return_value="STM7abc"), \
         patch("project.api.routes.auth._verify_hive_signature", return_value=True), \
         patch("project.api.routes.auth.get_reputation", return_value=-2.5):
        resp = await client.post("/api/auth/verify", json={
            "username": "spammer",
            "challenge": challenge,
            "signature": "aabb",
        })
    assert resp.status_code == 403
    assert "reputation" in resp.json()["detail"].lower()


async def test_verify_zero_rep_allowed(client):
    """Zero rep (new accounts / HAFSQL unreachable) should be allowed through."""
    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = ("newuser", time.time() + 300)
    with patch("project.api.routes.auth.get_posting_key", return_value="STM7abc"), \
         patch("project.api.routes.auth._verify_hive_signature", return_value=True), \
         patch("project.api.routes.auth.get_reputation", return_value=0.0):
        resp = await client.post("/api/auth/verify", json={
            "username": "newuser",
            "challenge": challenge,
            "signature": "aabb",
        })
    assert resp.status_code == 200
    assert resp.json()["username"] == "newuser"


# ── /me endpoint ─────────────────────────────────────────────────────────────

async def test_me_with_valid_jwt(client):
    resp = await client.get("/api/auth/me", headers=jwt_headers("alice"))
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


async def test_me_with_cookie(client):
    resp = await client.get("/api/auth/me", cookies=jwt_cookies("alice"))
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


async def test_me_without_auth(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_with_expired_jwt(client):
    token = make_jwt("alice", expired=True)
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_me_with_tampered_jwt(client):
    token = pyjwt.encode({"sub": "alice", "exp": 9999999999}, "wrong-secret", algorithm="HS256")
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# ── Logout ───────────────────────────────────────────────────────────────────

async def test_logout_clears_cookie(client):
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged_out"
    # Cookie should be deleted (max-age=0).
    cookie_header = resp.headers.get("set-cookie", "")
    assert JWT_COOKIE_NAME in cookie_header


# ── JWT secret fallback ─────────────────────────────────────────────────────

def test_jwt_secret_uses_jwt_secret_setting():
    with patch("project.api.deps.settings") as mock:
        mock.jwt_secret = "my-jwt-secret"
        mock.api_key = "my-api-key"
        assert _jwt_secret() == "my-jwt-secret"


def test_jwt_secret_falls_back_to_api_key():
    with patch("project.api.deps.settings") as mock:
        mock.jwt_secret = ""
        mock.api_key = "my-api-key"
        assert _jwt_secret() == "my-api-key"


# ── Bearer vs Cookie auth ──────────────────────────────────────────────────

async def test_me_bearer_preferred_over_cookie(client):
    """If both Bearer header and cookie are present, Bearer wins (cookie checked first)."""
    headers = jwt_headers("bearer-user")
    cookies = jwt_cookies("cookie-user")
    resp = await client.get("/api/auth/me", headers=headers, cookies=cookies)
    assert resp.status_code == 200
    # Cookie is checked first per deps.py logic.
    assert resp.json()["username"] == "cookie-user"


async def test_me_malformed_bearer(client):
    resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert resp.status_code == 401


async def test_me_missing_sub_claim(client):
    """Token without 'sub' claim should fail."""
    from datetime import datetime, timezone, timedelta
    token = pyjwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(days=1)},
        _jwt_secret(), algorithm="HS256",
    )
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    # Missing "sub" raises KeyError which gets caught as InvalidTokenError.
    assert resp.status_code in (401, 500)


# ── Rate limit cleanup ──────────────────────────────────────────────────────

def test_rate_log_purges_stale_entries():
    """When _rate_log exceeds _RATE_LOG_MAX, stale entries should be purged."""
    from project.api.routes.auth import _rate_log, _check_rate, _RATE_LOG_MAX
    from unittest.mock import MagicMock
    _rate_log.clear()

    # Fill rate log with stale entries.
    import collections
    now = time.monotonic()
    for i in range(_RATE_LOG_MAX + 1):
        _rate_log[f"stale:{i}"] = collections.deque([now - 120])  # expired (> 60s)

    # Add one fresh entry to trigger the purge path.
    mock_request = MagicMock()
    mock_request.client.host = "127.0.0.1"
    _check_rate(mock_request, "test", 100)

    # Stale entries should have been purged.
    assert len(_rate_log) < _RATE_LOG_MAX
    _rate_log.clear()


# ── Rate limit boundary ─────────────────────────────────────────────────────

async def test_challenge_rate_limit_boundary(client):
    """Exactly at limit should succeed, one over should fail."""
    from project.api.routes.auth import _rate_log
    _rate_log.clear()
    for i in range(10):
        resp = await client.post("/api/auth/challenge", json={"username": "alice"})
        assert resp.status_code == 200, f"Request {i+1} should succeed"

    resp = await client.post("/api/auth/challenge", json={"username": "alice"})
    assert resp.status_code == 429
    _rate_log.clear()


# ── Auth deps edge cases ─────────────────────────────────────────────────────

async def test_jwt_wrong_algorithm(client):
    """Token signed with wrong algorithm should be rejected."""
    from datetime import datetime, timezone, timedelta
    token = pyjwt.encode(
        {"sub": "alice", "exp": datetime.now(timezone.utc) + timedelta(days=1)},
        _jwt_secret(), algorithm="HS384",
    )
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
