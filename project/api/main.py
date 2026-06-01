import json
import logging
import logging.config
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

import asyncio

from .routes import router
from .routes.ui import periodic_sitemap_warm
from .. import apps_canonical, cache
from ..categories import CATEGORY_TREE
from ..config import settings
from ..db import crud
from ..db.session import AsyncSessionLocal, engine
from ..hafsql import shutdown as hafsql_shutdown

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "default"}},
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
        "uvicorn.access": {"level": "INFO", "propagate": True},
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

_SEEDS_FILE = Path("/combflow/seeds/centroids.json")


def _make_http_client() -> httpx.AsyncClient:
    """Build the shared outbound HTTP client.

    Defaults to ``follow_redirects=False`` (SSRF hardening, proposal 101 M1):
    callers that need redirect-following opt in explicitly (e.g.
    ``apps_canonical.refresh_from_upstream``). ``max_redirects`` is inert while
    redirects are disabled but kept harmless. Extracted so the redirect default
    is unit-testable without driving the full lifespan.
    """
    return httpx.AsyncClient(
        follow_redirects=False,  # SSRF hardening (proposal 101): callers opt in explicitly
        max_redirects=5,         # inert while follow_redirects=False; kept harmless
        timeout=httpx.Timeout(10.0),
        headers={"User-Agent": "CombFlow/1.0"},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed the category hierarchy.
    try:
        async with AsyncSessionLocal() as session:
            await crud.seed_category_tree(session, CATEGORY_TREE)
    except SQLAlchemyError as exc:
        logger.warning("Could not seed category tree: %s", exc)

    # Load centroids into memory (from DB or seeds file).
    centroids: dict = {}
    try:
        async with AsyncSessionLocal() as session:
            centroids = await crud.get_centroids(session)
        if centroids:
            logger.info("Loaded %d centroids from pgvector", len(centroids))
    except SQLAlchemyError as exc:
        logger.warning("Could not load centroids from DB: %s", exc)

    if not centroids and _SEEDS_FILE.exists():
        try:
            data = json.loads(_SEEDS_FILE.read_text())
            centroids = data.get("centroids", {})
            if centroids:
                logger.info("Loaded %d centroids from seeds file", len(centroids))
                try:
                    async with AsyncSessionLocal() as session:
                        await crud.save_centroids(session, centroids, data.get("metadata", {}))
                    logger.info("Persisted seeds to pgvector")
                except SQLAlchemyError as exc:
                    logger.warning("Could not persist seeds to pgvector: %s", exc)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read seeds file: %s", exc)

    if centroids:
        app.state.centroids = {cat: np.array(vec) for cat, vec in centroids.items()}
        logger.info("Centroids active: %s", ", ".join(centroids))
    else:
        app.state.centroids = {}
        logger.warning("No centroids — worker handles classification")

    # Pre-warm expensive caches so the first browser request is fast.
    # /api/languages and /api/stats otherwise do full table scans of posts
    # with cold PG buffers, costing 5-10s on the first hit after a rebuild.
    # Warm languages before stats: get_overview_stats reads the "languages"
    # cache to skip its own full table scan.
    try:
        async with AsyncSessionLocal() as session:
            langs = await crud.get_available_languages(session)
            cache.put("languages", {"languages": langs}, ttl=3600)

            stats = await crud.get_overview_stats(session)
            stats["api_base_url"] = settings.api_base_url
            cache.put("overview_stats", stats, ttl=300)

            comms = await crud.get_available_communities(session)
            cache.put("communities", {"communities": comms}, ttl=300)
        logger.info("pre-warmed languages/stats/communities caches")
    except SQLAlchemyError as exc:
        logger.warning("cache pre-warm failed: %s", exc)

    app.state.http_client = _make_http_client()

    # Pre-warm the sitemap cache, then re-warm every 12h in the background.
    # The first request after startup gets cached XML; the cache never goes
    # cold under live traffic. Don't await — let startup finish immediately.
    sitemap_warm_task = asyncio.create_task(periodic_sitemap_warm(AsyncSessionLocal))

    # Refresh the shared apps-canonical list daily. APP_CANONICAL_URLS starts
    # empty and is populated only by a successful refresh — pages rendered
    # before the first refresh simply skip the canonical tag.
    async def _refresh_apps_canonical():
        while True:
            await apps_canonical.refresh_from_upstream(app.state.http_client)
            try:
                await asyncio.sleep(86400)  # daily
            except asyncio.CancelledError:
                return
    apps_canonical_task = asyncio.create_task(_refresh_apps_canonical())

    logger.info("startup complete")
    yield
    apps_canonical_task.cancel()
    sitemap_warm_task.cancel()
    await app.state.http_client.aclose()
    hafsql_shutdown()
    await engine.dispose()
    logger.info("shutdown complete")


app = FastAPI(
    title="CombFlow Discovery Engine",
    description=(
        "Semantic post discovery for the Hive blockchain.\n\n"
        "Browse and filter posts by category, language, and sentiment."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=bool(settings.cors_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(router)

_STATIC_DIR = Path(__file__).resolve().parent / "templates" / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/health", tags=["ops"], summary="Liveness check")
async def health():
    return {"status": "ok"}


@app.get("/categories", tags=["categories"], summary="Full category tree")
async def category_tree():
    """Return the 2-level category hierarchy."""
    try:
        cached = cache.get("category_tree")
        if cached is not None:
            return cached
        async with AsyncSessionLocal() as session:
            tree = await crud.get_category_tree(session)
        result = {"categories": tree}
        cache.put("category_tree", result, ttl=86400)
        return result
    except SQLAlchemyError:
        return {
            "categories": [
                {"name": parent, "children": [
                    {"name": c} for c in children if c != parent
                ]}
                for parent, children in CATEGORY_TREE.items()
            ]
        }

