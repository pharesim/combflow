import json
import logging
import logging.config
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from .routes import router
from .. import cache
from ..categories import CATEGORY_TREE
from ..config import settings
from ..db import crud
from ..db.session import AsyncSessionLocal, engine

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed the category hierarchy.
    try:
        async with AsyncSessionLocal() as session:
            await crud.seed_category_tree(session, CATEGORY_TREE)
    except Exception as exc:
        logger.warning("Could not seed category tree: %s", exc)

    # Load centroids into memory (from DB or seeds file).
    centroids: dict = {}
    try:
        async with AsyncSessionLocal() as session:
            centroids = await crud.get_centroids(session)
        if centroids:
            logger.info("Loaded %d centroids from pgvector", len(centroids))
    except Exception as exc:
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
                except Exception as exc:
                    logger.warning("Could not persist seeds to pgvector: %s", exc)
        except Exception as exc:
            logger.warning("Could not read seeds file: %s", exc)

    if centroids:
        app.state.centroids = {cat: np.array(vec) for cat, vec in centroids.items()}
        logger.info("Centroids active: %s", ", ".join(centroids))
    else:
        app.state.centroids = {}
        logger.warning("No centroids — worker handles classification")

    logger.info("startup complete")
    yield
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
    allow_origins=settings.cors_origins or ["*"],
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
    except Exception:
        return {
            "categories": [
                {"name": parent, "children": [
                    {"name": c} for c in children if c != parent
                ]}
                for parent, children in CATEGORY_TREE.items()
            ]
        }

