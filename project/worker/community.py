"""Community → category resolution for the Hive worker."""
import logging
import re

import numpy as np

from ..db import crud
from ..db.session import WorkerSessionLocal as AsyncSessionLocal
from ..hafsql import get_community

logger = logging.getLogger(__name__)

_COMMUNITY_PATTERN = re.compile(r"^hive-\d+$")
_COMMUNITY_BOOST = 0.08
_COMMUNITY_MAP_THRESHOLD = 0.40

_HIVE_PLATFORM_RE = re.compile(
    r'\b(?:hive|hivean|hiveans|hiver|hivers|hivian|hivians)\b',
    re.IGNORECASE,
)


def _strip_hive_words(text: str) -> str:
    """Remove Hive platform name from community text so it doesn't bias embedding."""
    return _HIVE_PLATFORM_RE.sub('', text).strip()

# community_id -> (category_slug | None, community_name, score)
_community_cache: dict[str, tuple[str | None, str, float]] = {}
_persisted_communities: set[str] = set()


def _extract_community_id(parent_permlink: str | None) -> str | None:
    """Extract community ID from parent_permlink if it matches hive-NNNNNN."""
    if parent_permlink and _COMMUNITY_PATTERN.match(parent_permlink):
        return parent_permlink
    return None


def _resolve_community(
    community_id: str, embedder, centroids: dict[str, "np.ndarray"]
) -> tuple[str | None, str, float]:
    """Resolve a community to its best-matching category.

    Returns (category_slug | None, community_name, score).
    Results are cached for the worker's lifetime.
    """
    cached = _community_cache.get(community_id)
    if cached is not None:
        return cached

    meta = get_community(community_id)
    name = ""
    if meta:
        name = meta.get("title") or ""
        about = meta.get("about") or ""
    else:
        about = ""

    result: tuple[str | None, str, float] = (None, name, 0.0)

    text = ""
    if embedder and centroids and (name or about):
        text = _strip_hive_words(f"{name} {about}".strip())
    if text:
        emb = embedder.encode(text, normalize_embeddings=True)
        scores = [(cat, float(np.dot(emb, centroid))) for cat, centroid in centroids.items()]
        if scores:
            scores.sort(key=lambda x: x[1], reverse=True)
            best_cat, best_score = scores[0]
            if best_score >= _COMMUNITY_MAP_THRESHOLD:
                result = (best_cat, name, best_score)
            else:
                result = (None, name, best_score)

    _community_cache[community_id] = result
    logger.info("community %s resolved: category=%s name=%r score=%.3f",
                community_id, result[0], result[1], result[2])
    return result


def _persist_community_mapping(
    db, community_id: str, category_slug: str | None,
    community_name: str, score: float,
) -> None:
    """Write community→category mapping to DB so the API can serve suggestions."""
    async def _do():
        async with AsyncSessionLocal() as session:
            await crud.upsert_community_mapping(
                session, community_id, category_slug, community_name, score,
            )
    try:
        db.run(_do())
    except Exception as exc:
        logger.warning("failed to persist community mapping for %s: %s", community_id, exc)
