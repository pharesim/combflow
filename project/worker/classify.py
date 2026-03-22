"""Classification, sentiment analysis, and language detection for the Hive worker."""
import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np

from ..db import crud
from ..db.session import WorkerSessionLocal as AsyncSessionLocal
from ..hafsql import get_post_body
from ..text import clean_post_body as _clean_post_body
from .bridge import _save_post
from .community import (
    _extract_community_id, _resolve_community, _persist_community_mapping,
    _persisted_communities, _COMMUNITY_BOOST, _COMMUNITY_MAP_THRESHOLD,
)

logger = logging.getLogger(__name__)

_MIN_CLEAN_BODY = 80
_EMBEDDING_DIM = 384
_SEEDS_FILE = Path("/combflow/seeds/centroids.json")

MIN_AUTHOR_REPUTATION = 20.0

# Sentiment anchor phrases.
_POSITIVE_ANCHORS = [
    "I love this, it's wonderful and amazing",
    "Great experience, highly recommend, fantastic",
    "This is excellent, beautiful, inspiring work",
    "Happy, grateful, excited, wonderful news",
]
_NEGATIVE_ANCHORS = [
    "I hate this, it's terrible and awful",
    "Bad experience, do not recommend, horrible",
    "This is disappointing, ugly, frustrating",
    "Sad, angry, disgusted, terrible news",
]

_LANG_PROB_THRESHOLD = 0.25


def _load_embedder():
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model ...")
        return SentenceTransformer("all-MiniLM-L6-v2")
    except ImportError:
        logger.warning("sentence-transformers not installed — classification disabled")
        return None


def _build_sentiment_anchors(embedder) -> tuple[np.ndarray, np.ndarray]:
    pos = embedder.encode(_POSITIVE_ANCHORS, normalize_embeddings=True).mean(axis=0)
    pos /= np.linalg.norm(pos)
    neg = embedder.encode(_NEGATIVE_ANCHORS, normalize_embeddings=True).mean(axis=0)
    neg /= np.linalg.norm(neg)
    return pos, neg


def _load_centroids(db) -> dict[str, np.ndarray]:
    centroids: dict[str, list[float]] = {}
    try:
        async def _get():
            async with AsyncSessionLocal() as session:
                return await crud.get_centroids(session)
        centroids = db.run(_get())
        if centroids:
            logger.info("Loaded %d centroids from pgvector", len(centroids))
    except Exception as exc:
        logger.warning("Could not load centroids from DB: %s", exc)

    if not centroids and _SEEDS_FILE.exists():
        try:
            data = json.loads(_SEEDS_FILE.read_text())
            centroids = data.get("centroids", {})
            logger.info("Loaded %d centroids from seeds file", len(centroids))
        except Exception as exc:
            logger.warning("Could not read seeds file: %s", exc)

    if not centroids:
        logger.warning("No centroids — posts saved without categories")

    return {cat: np.array(vec) for cat, vec in centroids.items()}


def _detect_languages(text: str, meta_langs: list[str] | None = None) -> list[str]:
    """Detect languages from text + json_metadata.

    Returns a deduplicated list.  json_metadata languages are trusted;
    langdetect probabilistic results above the threshold are added.
    """
    langs: list[str] = []

    if meta_langs:
        for code in meta_langs:
            c = str(code).strip().lower()[:10]
            if c and c not in langs:
                langs.append(c)

    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        for result in detect_langs(text[:2000]):
            code = result.lang.lower()
            if result.prob >= _LANG_PROB_THRESHOLD and code not in langs:
                langs.append(code)
    except Exception:
        pass

    return langs


def _classify_from_embedding(
    emb: np.ndarray, centroids: dict[str, np.ndarray], threshold: float,
    boost_category: str | None = None, boost_amount: float = 0.0,
) -> list[str]:
    """Classify a pre-computed embedding against centroids.

    Optionally applies a community-based boost to one category's score.
    """
    if not centroids:
        return []
    scores = [(cat, float(np.dot(emb, centroid))) for cat, centroid in centroids.items()]
    if boost_category:
        scores = [
            (cat, s + boost_amount) if cat == boost_category else (cat, s)
            for cat, s in scores
        ]
    scores.sort(key=lambda x: x[1], reverse=True)

    top_score = scores[0][1]
    if top_score < threshold:
        return []

    result = []
    for cat, score in scores:
        if score < threshold:
            break
        if score >= top_score - 0.03 and len(result) < 3:
            result.append(cat)
        else:
            break
    return result


def _sentiment_from_embedding(
    emb: np.ndarray, pos_anchor: np.ndarray, neg_anchor: np.ndarray
) -> tuple[str, float]:
    """Compute sentiment from a pre-computed embedding."""
    pos_sim = float(np.dot(emb, pos_anchor))
    neg_sim = float(np.dot(emb, neg_anchor))
    raw = pos_sim - neg_sim
    score = round(max(-1.0, min(1.0, raw * 4)), 3)
    if score > 0.05:
        label = "positive"
    elif score < -0.05:
        label = "negative"
    else:
        label = "neutral"
    return label, score


def _classify_and_save(
    db, embedder, centroids, threshold: float,
    pos_anchor, neg_anchor,
    author: str, permlink: str, title: str, body: str,
    json_metadata: str | dict | None = None,
    created: datetime | None = None,
    label: str = "",
    parent_permlink: str | None = None,
) -> None:
    """Full classification pipeline: clean, classify, detect lang, sentiment, save."""
    if body.lstrip().startswith("@@"):
        return

    # Parse json_metadata early — needed for cross-post detection.
    tags_hint = ""
    meta = json_metadata
    try:
        if isinstance(meta, str) and meta:
            meta = json.loads(meta)
        if isinstance(meta, dict):
            tags = meta.get("tags", [])
            if tags:
                tags_hint = " ".join(tags)
    except Exception:
        pass

    # Cross-post detection: classify using original post's body.
    cross_post_key = None
    if isinstance(meta, dict):
        cross_post_key = meta.get("cross_post_key")
    if cross_post_key and "/" in cross_post_key:
        cp_author, cp_permlink = cross_post_key.split("/", 1)
        original_body = get_post_body(cp_author, cp_permlink)
        if original_body:
            body = original_body

    clean_body = _clean_post_body(body)
    if len(clean_body) < _MIN_CLEAN_BODY:
        return

    # Skip bot/templated posts with very low text-to-markup ratio.
    alpha_chars = sum(c.isalpha() for c in clean_body)
    if alpha_chars / len(clean_body) < 0.50:
        return

    # Extract metadata languages from json_metadata.
    meta_langs: list[str] = []
    try:
        if isinstance(meta, dict):
            ml = meta.get("language")
            if isinstance(ml, str) and ml:
                meta_langs = [x.strip() for x in ml.split(",") if x.strip()]
            elif isinstance(ml, list):
                meta_langs = [str(x) for x in ml if x]
    except Exception:
        pass

    classify_text = f"{title} {clean_body} {tags_hint}".strip()[:2000]

    # Extract community ID from parent_permlink.
    community_id = _extract_community_id(parent_permlink)

    # Embed once, reuse for both classification and sentiment.
    categories = []
    sentiment, sentiment_score = ("neutral", 0.0)
    if embedder:
        emb = embedder.encode(classify_text, normalize_embeddings=True)

        # Apply community boost if the post belongs to a topic-specific community.
        if community_id and centroids:
            comm_cat, comm_name, comm_score = _resolve_community(community_id, embedder, centroids)
            # Persist mapping to DB on first resolve (cache means this runs once per community).
            if community_id not in _persisted_communities:
                _persist_community_mapping(db, community_id, comm_cat, comm_name, comm_score)
                _persisted_communities.add(community_id)
            if comm_cat and comm_score >= _COMMUNITY_MAP_THRESHOLD:
                categories = _classify_from_embedding(
                    emb, centroids, threshold, comm_cat, _COMMUNITY_BOOST,
                )
            else:
                categories = _classify_from_embedding(emb, centroids, threshold)
        else:
            categories = _classify_from_embedding(emb, centroids, threshold)

        sentiment, sentiment_score = _sentiment_from_embedding(emb, pos_anchor, neg_anchor)

    languages = _detect_languages(clean_body, meta_langs)

    _save_post(db, {
        "author": author,
        "permlink": permlink,
        "created": created,
        "categories": categories,
        "languages": languages,
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "community_id": community_id,
    })
    logger.info("%s processed %s/%s langs=%s sentiment=%s cats=%s community=%s",
                label, author, permlink, languages, sentiment, categories, community_id)
