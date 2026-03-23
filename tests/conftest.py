"""Shared test fixtures — Alembic-migrated DB per session, truncate per test, seeded data."""
import os
import subprocess
import sys

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://combflow:change_me@db/combflow_test")
os.environ.setdefault("API_KEY", "test-secret-key-long-enough-for-hs256")

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from project.db.models import Base
from project.api.main import app
from project.api.deps import get_db, _jwt_secret, JWT_COOKIE_NAME
from project.categories import CATEGORY_TREE
from project import cache
from project.api.routes.auth import _challenge_limiter, _verify_limiter, _challenges

_PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")



def make_jwt(username: str, expired: bool = False) -> str:
    """Create a valid (or expired) JWT for testing."""
    secret = _jwt_secret()
    if expired:
        exp = datetime.now(timezone.utc) - timedelta(hours=1)
    else:
        exp = datetime.now(timezone.utc) + timedelta(days=7)
    return pyjwt.encode({"sub": username, "exp": exp}, secret, algorithm="HS256")


def jwt_headers(username: str) -> dict[str, str]:
    """Return Authorization Bearer header for the given user."""
    return {"Authorization": f"Bearer {make_jwt(username)}"}


def jwt_cookies(username: str) -> dict[str, str]:
    """Return cookie dict for the given user."""
    return {JWT_COOKIE_NAME: make_jwt(username)}

_test_engine = create_async_engine(os.environ["DATABASE_URL"], echo=False, poolclass=NullPool)
_TestSession = sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    async with _TestSession() as session:
        yield session


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations():
    """Run Alembic migrations once so test DB schema matches production."""
    # Downgrade first to ensure clean slate (ignore errors if DB is already clean)
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=_PROJECT_ROOT, capture_output=True,
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_PROJECT_ROOT, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stderr.decode()}")
    yield
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=_PROJECT_ROOT, capture_output=True,
    )


_ALL_TABLES = [
    "post_category", "post_language",
    "stream_cursors", "category_centroids", "community_mappings",
    "posts", "categories",
]


@pytest.fixture(autouse=True)
async def setup_db(_apply_migrations):
    """Truncate all tables before each test for isolation."""
    cache.clear()
    _challenge_limiter._log.clear()
    _verify_limiter._log.clear()
    _challenges.clear()
    async with _test_engine.begin() as conn:
        await conn.execute(text("TRUNCATE " + ", ".join(_ALL_TABLES) + " CASCADE"))
    yield
    cache.clear()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def db_session():
    """Raw DB session for direct CRUD calls in tests."""
    async with _TestSession() as session:
        yield session


@pytest.fixture
async def seeded_db(setup_db):
    """DB with category tree + sample posts for integration tests."""
    async with _TestSession() as session:
        # Seed category tree.
        from project.db.crud import seed_category_tree, create_post
        await seed_category_tree(session, CATEGORY_TREE)

        # Find a known parent + leaf for test assertions.
        parent_name = list(CATEGORY_TREE.keys())[0]
        leaf_name = CATEGORY_TREE[parent_name][0]

        # Insert test posts.
        await create_post(session, {
            "author": "alice",
            "permlink": "test-post-one",
            "created": datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
            "categories": [leaf_name],
            "languages": ["en"],
            "sentiment": "positive",
            "sentiment_score": 0.7,
        })
        await create_post(session, {
            "author": "bob",
            "permlink": "test-post-two",
            "created": datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc),
            "categories": [leaf_name],
            "languages": ["en", "es"],
            "sentiment": "negative",
            "sentiment_score": -0.5,
        })
        await create_post(session, {
            "author": "carol",
            "permlink": "test-post-three",
            "created": datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc),
            "categories": [CATEGORY_TREE[list(CATEGORY_TREE.keys())[1]][0]],
            "languages": ["fr"],
            "sentiment": "neutral",
            "sentiment_score": 0.0,
        })

    yield {"parent_name": parent_name, "leaf_name": leaf_name}
