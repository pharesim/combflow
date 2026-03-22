"""Auth routes — Hive Keychain challenge-response authentication."""

import secrets
import time
from binascii import hexlify, unhexlify
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...hafsql import get_posting_key, get_reputation
from ..deps import JWT_COOKIE_NAME, _jwt_secret, require_jwt
from ..rate_limit import RateLimiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _is_secure(request: Request) -> bool:
    """Check if the original client connection was HTTPS.

    Handles both direct HTTPS and reverse-proxy setups (Caddy, nginx, etc.)
    that set X-Forwarded-Proto.
    """
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"

# In-memory challenge store: { challenge_string: (username, expires_at) }
_challenges: dict[str, tuple[str, float]] = {}

_CHALLENGE_TTL = 300  # 5 minutes
_CHALLENGE_MAX = 10_000
_TOKEN_LIFETIME_DAYS = 7

# Per-IP rate limiting for auth endpoints.
_challenge_limiter = RateLimiter(window_seconds=60, max_requests=10)
_verify_limiter = RateLimiter(window_seconds=60, max_requests=5)


# ── Models ───────────────────────────────────────────────────────────────────


class ChallengeRequest(BaseModel):
    username: str = Field(
        ..., min_length=1, max_length=16, pattern=r"^[a-z0-9][a-z0-9.\-]{0,15}$"
    )


class ChallengeResponse(BaseModel):
    challenge: str
    expires_in: int = _CHALLENGE_TTL


class VerifyAuthRequest(BaseModel):
    username: str = Field(
        ..., min_length=1, max_length=16, pattern=r"^[a-z0-9][a-z0-9.\-]{0,15}$"
    )
    challenge: str = Field(..., min_length=20, max_length=120)
    signature: str = Field(..., min_length=1, max_length=256)


class AuthResponse(BaseModel):
    username: str
    expires_at: str


# ── Signature verification ───────────────────────────────────────────────────


def _verify_hive_signature(
    message: str, signature: str, expected_pub_key: str
) -> bool:
    """Verify a Hive Keychain signBuffer signature.

    Keychain's ``requestSignBuffer`` signs SHA-256(message).  The nectar
    ``verify_message`` also hashes its input, so we pass the raw message
    string — NOT a pre-hash — to avoid double-hashing.

    We recover the public key from the signature and compare it against the
    on-chain posting key fetched from HAFSQL (not a client-provided key).

    Based on the pattern from pharesim/hiveinvite.
    """
    try:
        from nectargraphenebase.account import PublicKey
        from nectargraphenebase.ecdsasig import verify_message

        # Pass raw message — verify_message hashes it internally with SHA-256.
        recovered_bytes = verify_message(message, unhexlify(signature))
        # verify_message returns compressed public key bytes.
        # Convert hex to STM-prefixed public key string via PublicKey.
        recovered_hex = hexlify(recovered_bytes).decode("ascii")
        recovered_pk = str(PublicKey(recovered_hex, prefix="STM"))
        return secrets.compare_digest(recovered_pk, str(expected_pub_key))
    except Exception:
        return False


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/challenge")
async def create_challenge(payload: ChallengeRequest, request: Request) -> ChallengeResponse:
    ip = request.client.host if request.client else "unknown"
    _challenge_limiter.check(f"challenge:{ip}", "Too many requests, try again later")
    now = time.time()
    # Purge expired challenges
    expired = [k for k, (_, exp) in _challenges.items() if exp < now]
    for k in expired:
        del _challenges[k]

    if len(_challenges) >= _CHALLENGE_MAX:
        raise HTTPException(503, "Too many pending challenges, try again shortly")

    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = (payload.username, now + _CHALLENGE_TTL)
    return ChallengeResponse(challenge=challenge)


@router.post("/verify")
async def verify_signature(payload: VerifyAuthRequest, request: Request) -> AuthResponse:
    ip = request.client.host if request.client else "unknown"
    _verify_limiter.check(f"verify:{ip}", "Too many requests, try again later")
    # 1. Validate challenge
    entry = _challenges.pop(payload.challenge, None)
    if not entry:
        raise HTTPException(
            400,
            "Challenge expired or server restarted. Please try again.",
            headers={"X-Retry": "true"},
        )
    expected_user, expires_at = entry
    if time.time() > expires_at:
        raise HTTPException(400, "Challenge expired")
    if payload.username != expected_user:
        raise HTTPException(400, "Username mismatch")

    # 2. Get posting key from HAFSQL and verify signature
    pub_key = get_posting_key(payload.username)
    if not pub_key:
        raise HTTPException(503, "Authentication service temporarily unavailable — please retry")

    if not _verify_hive_signature(payload.challenge, payload.signature, pub_key):
        raise HTTPException(401, "Invalid signature")

    # 3. Block negative-reputation accounts (proposal 006)
    rep = get_reputation(payload.username)
    if rep < 0:
        raise HTTPException(403, "Account reputation too low to log in")

    # 4. Issue JWT as httpOnly cookie
    secret = _jwt_secret()
    if not secret:
        raise HTTPException(500, "Server authentication not configured")
    exp = datetime.now(timezone.utc) + timedelta(days=_TOKEN_LIFETIME_DAYS)
    token = jwt.encode(
        {"sub": payload.username, "exp": exp},
        secret,
        algorithm="HS256",
    )
    max_age = _TOKEN_LIFETIME_DAYS * 86400
    response = JSONResponse(
        content=AuthResponse(
            username=payload.username,
            expires_at=exp.isoformat(),
        ).model_dump()
    )
    secure = _is_secure(request)
    response.set_cookie(
        key=JWT_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=secure,
        samesite="strict" if secure else "lax",
        path="/api",
        max_age=max_age,
    )
    return response


@router.get("/me")
async def get_me(username: str = Depends(require_jwt)):
    return {"username": username}


@router.post("/logout")
async def logout(request: Request):
    secure = _is_secure(request)
    response = JSONResponse(content={"status": "logged_out"})
    response.delete_cookie(
        key=JWT_COOKIE_NAME,
        httponly=True,
        secure=secure,
        samesite="strict" if secure else "lax",
        path="/api",
    )
    return response
