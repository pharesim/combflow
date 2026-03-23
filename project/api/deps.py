import os
import secrets as _secrets

import jwt
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.session import AsyncSessionLocal

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)

JWT_COOKIE_NAME = "hivecomb_jwt"


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def require_api_key(api_key: str = Security(_API_KEY_HEADER)) -> str:
    # Read at call time so test overrides via os.environ work correctly.
    expected = os.environ.get("API_KEY", "")
    if not expected:
        raise RuntimeError("API_KEY is not configured")
    if not api_key or not _secrets.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return api_key


def _jwt_secret() -> str:
    """Return the HMAC secret for JWT operations."""
    return settings.jwt_secret or settings.api_key


async def require_jwt(
    request: Request,
    creds: HTTPAuthorizationCredentials = Security(_bearer),
) -> str:
    """Extract and validate JWT, return username.

    Reads the token from the httpOnly cookie first, then falls back to
    the Authorization Bearer header for non-browser API clients.
    """
    token = request.cookies.get(JWT_COOKIE_NAME)
    if not token and creds:
        token = creds.credentials
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET (or API_KEY) is not configured")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
        return sub
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
