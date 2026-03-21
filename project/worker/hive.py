"""Hive blockchain worker — streams posts, classifies in-process, saves to DB.

Two modes run concurrently:
  1. LIVE stream: follows the head of the chain, classifies new posts as they appear
  2. BACKFILL thread: walks backwards through HAFSQL, classifying older posts

Pipeline per post:
  1. Check author reputation via HAFSQL (≥ 20)
  2. Clean post body (strip markdown noise)
  3. Classify against category centroids (sentence-transformers)
  4. Detect language (langdetect)
  5. Compute sentiment via embedding similarity
  6. Save classification to local PostgreSQL (no chain data stored)
"""
import asyncio
import functools
import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from nectar import Hive
from nectar.blockchain import Blockchain

from ..categories import CATEGORY_TREE
from ..config import settings
from ..db import crud
from ..db.session import WorkerSessionLocal as AsyncSessionLocal, worker_engine as engine
from ..hafsql import get_reputation, get_reputations, get_community, _raw_rep_to_score, build_dsn
from ..text import clean_post_body as _clean_post_body

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

_RECONNECT_DELAY = 10
_CURSOR_KEY = "live_worker"
_BACKFILL_CURSOR_KEY = "backfill_worker"
_CURSOR_UPDATE_INTERVAL = 50
_CATCHUP_THRESHOLD = 200
_SEEDS_FILE = Path("/combflow/seeds/centroids.json")

_BACKFILL_BATCH = 100
_BACKFILL_PAUSE = 2  # seconds between batches
_MIN_CLEAN_BODY = 80  # minimum chars after stripping markdown/HTML/URLs

MIN_AUTHOR_REPUTATION = 20.0
_REP_CACHE_MAX = 50_000

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


# ── Async bridge ──────────────────────────────────────────────────────────────

class _DB:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()

    def run(self, coro):
        with self._lock:
            return self._loop.run_until_complete(coro)

    def close(self):
        with self._lock:
            self._loop.run_until_complete(engine.dispose())
            self._loop.close()


# ── Reputation (via HAFSQL) ──────────────────────────────────────────────────

@functools.lru_cache(maxsize=_REP_CACHE_MAX)
def _get_author_rep(author: str) -> float:
    return get_reputation(author)


# ── Community resolution ─────────────────────────────────────────────────────

import re as _re

_MD_IMG_RE = _re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
_HTML_IMG_RE = _re.compile(r'<img\s[^>]*?src=["\']?([^"\'\s>]+)', _re.IGNORECASE)
_YT_RE = _re.compile(r'(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)')
_3SPEAK_RE = _re.compile(r'3speak\.tv/watch\?v=[\w.-]+/([\w-]+)')

_COMMUNITY_PATTERN = _re.compile(r"^hive-\d+$")
_COMMUNITY_BOOST = 0.08
_COMMUNITY_MAP_THRESHOLD = 0.40

# community_id -> (category_slug | None, community_name, score)
_community_cache: dict[str, tuple[str | None, str, float]] = {}
_persisted_communities: set[str] = set()  # tracks which mappings have been written to DB


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

    # Fetch community metadata from HAFSQL.
    meta = get_community(community_id)
    name = ""
    if meta:
        name = meta.get("title") or ""
        about = meta.get("about") or ""
    else:
        about = ""

    result: tuple[str | None, str, float] = (None, name, 0.0)

    if embedder and centroids and (name or about):
        text = f"{name} {about}".strip()
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
    db: _DB, community_id: str, category_slug: str | None,
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


def _extract_community_id(parent_permlink: str | None) -> str | None:
    """Extract community ID from parent_permlink if it matches hive-NNNNNN."""
    if parent_permlink and _COMMUNITY_PATTERN.match(parent_permlink):
        return parent_permlink
    return None


# ── Language detection ────────────────────────────────────────────────────────

_LANG_PROB_THRESHOLD = 0.25


def _detect_languages(text: str, meta_langs: list[str] | None = None) -> list[str]:
    """Detect languages from text + json_metadata.

    Returns a deduplicated list.  json_metadata languages are trusted;
    langdetect probabilistic results above the threshold are added.
    """
    langs: list[str] = []

    # 1. Trust app-provided languages from json_metadata.
    if meta_langs:
        for code in meta_langs:
            c = str(code).strip().lower()[:10]
            if c and c not in langs:
                langs.append(c)

    # 2. Probabilistic detection from text.
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


# ── Classifier + sentiment ────────────────────────────────────────────────────

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


def _load_centroids(db: _DB) -> dict[str, np.ndarray]:
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


def _classify(text: str, embedder, centroids: dict[str, np.ndarray], threshold: float) -> list[str]:
    """Classify text against centroids. Kept for test compatibility."""
    if not embedder or not centroids:
        return []
    emb = embedder.encode(text, normalize_embeddings=True)
    return _classify_from_embedding(emb, centroids, threshold)


def _classify_from_embedding(
    emb: np.ndarray, centroids: dict[str, np.ndarray], threshold: float
) -> list[str]:
    """Classify a pre-computed embedding against centroids."""
    if not centroids:
        return []
    scores = [(cat, float(np.dot(emb, centroid))) for cat, centroid in centroids.items()]
    scores.sort(key=lambda x: x[1], reverse=True)

    if not scores:
        return []

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


def _classify_from_embedding_with_boost(
    emb: np.ndarray, centroids: dict[str, np.ndarray], threshold: float,
    boost_category: str, boost_amount: float,
) -> list[str]:
    """Classify with a community-based boost to one category's score."""
    if not centroids:
        return []
    scores = [(cat, float(np.dot(emb, centroid))) for cat, centroid in centroids.items()]
    # Apply boost.
    scores = [
        (cat, score + boost_amount) if cat == boost_category else (cat, score)
        for cat, score in scores
    ]
    scores.sort(key=lambda x: x[1], reverse=True)

    if not scores:
        return []

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


def _analyze_sentiment(
    text: str, embedder, pos_anchor: np.ndarray, neg_anchor: np.ndarray
) -> tuple[str, float]:
    """Analyze sentiment from text. Kept for test compatibility."""
    emb = embedder.encode(text[:500], normalize_embeddings=True)
    return _sentiment_from_embedding(emb, pos_anchor, neg_anchor)


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


# ── Shared classification pipeline ──────────────────────────────────────────

def _classify_and_save(
    db: _DB, embedder, centroids, threshold: float,
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

    clean_body = _clean_post_body(body)
    if len(clean_body) < _MIN_CLEAN_BODY:
        return

    # Skip bot/templated posts with very low text-to-markup ratio.
    alpha_chars = sum(c.isalpha() for c in clean_body)
    if len(clean_body) > 0 and alpha_chars / len(clean_body) < 0.50:
        return

    tags_hint = ""
    try:
        meta = json_metadata
        if isinstance(meta, str) and meta:
            meta = json.loads(meta)
        if isinstance(meta, dict):
            tags = meta.get("tags", [])
            if tags:
                tags_hint = " ".join(tags)
    except Exception:
        pass

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
                categories = _classify_from_embedding_with_boost(
                    emb, centroids, threshold, comm_cat, _COMMUNITY_BOOST,
                )
            else:
                categories = _classify_from_embedding(emb, centroids, threshold)
        else:
            categories = _classify_from_embedding(emb, centroids, threshold)

        sentiment, sentiment_score = _sentiment_from_embedding(emb, pos_anchor, neg_anchor)

    languages = _detect_languages(clean_body, meta_langs)

    # Extract thumbnail URL from json_metadata or body.
    thumbnail_url = ""
    try:
        if isinstance(meta, dict):
            images = meta.get("image", [])
            if images and isinstance(images, list):
                thumbnail_url = _re.sub(r'[\])].*$', '', str(images[0]).replace("&amp;", "&"))
    except Exception:
        pass

    if not thumbnail_url and body:
        md_match = _MD_IMG_RE.search(body)
        if md_match:
            thumbnail_url = md_match.group(1)
        else:
            html_img_match = _HTML_IMG_RE.search(body)
            if html_img_match:
                thumbnail_url = html_img_match.group(1)
            else:
                yt_match = _YT_RE.search(body)
                if yt_match:
                    thumbnail_url = f"https://img.youtube.com/vi/{yt_match.group(1)}/hqdefault.jpg"
                else:
                    ts_match = _3SPEAK_RE.search(body)
                    if ts_match:
                        thumbnail_url = f"https://images.3speak.tv/images/{ts_match.group(1)}.webp"

    if not thumbnail_url and body:
        cp_match = _re.search(r'cross post of \[@([\w.-]+)/([\w-]+)\]', body)
        if cp_match:
            try:
                import httpx
                resp = httpx.post("https://api.hive.blog", json={
                    "jsonrpc": "2.0",
                    "method": "bridge.get_post",
                    "params": {"author": cp_match.group(1), "permlink": cp_match.group(2)},
                    "id": 1,
                }, timeout=5)
                orig = resp.json().get("result", {})
                orig_images = orig.get("json_metadata", {}).get("image", [])
                if orig_images:
                    thumbnail_url = _re.sub(r'[\])].*$', '', str(orig_images[0]).replace("&amp;", "&"))
            except Exception:
                pass

    _save_post(db, {
        "author": author,
        "permlink": permlink,
        "created": created,
        "categories": categories,
        "languages": languages,
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "community_id": community_id,
        "title": title,
        "thumbnail_url": thumbnail_url or None,
    })
    logger.info("%s processed %s/%s langs=%s sentiment=%s cats=%s community=%s",
                label, author, permlink, languages, sentiment, categories, community_id)


# ── DB operations ─────────────────────────────────────────────────────────────

def _seed_categories(db: _DB) -> None:
    async def _seed():
        async with AsyncSessionLocal() as session:
            await crud.seed_category_tree(session, CATEGORY_TREE)
    try:
        db.run(_seed())
    except Exception as exc:
        logger.warning("Could not seed category tree: %s", exc)


def _save_post(db: _DB, data: dict) -> None:
    async def _do():
        async with AsyncSessionLocal() as session:
            await crud.create_post(session, data)
    db.run(_do())


def _get_cursor(db: _DB, key: str) -> int | None:
    async def _do():
        async with AsyncSessionLocal() as session:
            return await crud.get_cursor(session, key)
    return db.run(_do())


def _set_cursor(db: _DB, key: str, block_num: int) -> None:
    async def _do():
        async with AsyncSessionLocal() as session:
            await crud.set_cursor(session, key, block_num)
    db.run(_do())


def _existing_author_permlinks(db: _DB, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    async def _do():
        async with AsyncSessionLocal() as session:
            return await crud.existing_author_permlinks(session, pairs)
    return db.run(_do())


# ── Backfill thread (HAFSQL) ────────────────────────────────────────────────

def _backfill_thread(
    db: _DB, embedder, centroids, threshold: float,
    pos_anchor, neg_anchor,
    stop_event: threading.Event,
) -> None:
    """Walk backwards through HAFSQL, classifying older posts.

    Two phases:
      1. CATCH-UP: start from NOW, work backwards to the saved frontier.
         This covers any posts missed while the worker was offline.
         Skips faster when batches are all duplicates.
      2. EXPLORE: continue from the frontier into older history.
         The frontier cursor is only advanced during this phase.
    """
    import psycopg2
    import psycopg2.extras

    logger.info("BACKFILL starting — classifying older posts via HAFSQL")
    conn = None
    total = 0
    skipped = 0
    retries = 0
    max_retries = 10

    def _connect():
        nonlocal conn
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        conn = psycopg2.connect(build_dsn())
        conn.autocommit = True

    try:
        _connect()
    except Exception as exc:
        logger.error("BACKFILL cannot connect to HAFSQL: %s", exc)
        return

    # The frontier is the farthest-back timestamp we've fully processed.
    saved_ts = _get_cursor(db, _BACKFILL_CURSOR_KEY)
    if saved_ts:
        frontier = datetime.fromtimestamp(saved_ts, tz=timezone.utc)
    else:
        frontier = None  # first run — no frontier yet

    # Always start scanning from NOW so recent posts are classified first.
    cursor_dt = datetime.now(timezone.utc)
    catching_up = frontier is not None
    if catching_up:
        logger.info("BACKFILL catch-up: NOW -> %s, then explore further back",
                     frontier.isoformat())
    else:
        logger.info("BACKFILL first run: starting from NOW")

    while not stop_event.is_set():
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT c.author, c.permlink, c.title, c.body, c.created,
                       c.json_metadata, c.parent_permlink,
                       r.reputation
                FROM hafsql.comments c
                LEFT JOIN hafsql.reputations r ON c.author = r.account_name
                WHERE c.parent_author = ''
                  AND LENGTH(c.body) >= 80
                  AND c.created < %s
                ORDER BY c.created DESC
                LIMIT %s
                """,
                (cursor_dt, _BACKFILL_BATCH),
            )
            rows = cur.fetchall()
            cur.close()
            retries = 0
        except Exception as exc:
            retries += 1
            logger.warning("BACKFILL query failed (attempt %d/%d): %s",
                           retries, max_retries, exc)
            if retries >= max_retries:
                logger.error("BACKFILL max retries — stopping")
                break
            time.sleep(5)
            try:
                _connect()
            except Exception:
                pass
            continue

        if not rows:
            logger.info("BACKFILL reached end of HAFSQL posts — done")
            break

        # Advance cursor to the oldest post in this batch.
        oldest = rows[-1].get("created")
        if isinstance(oldest, datetime):
            if not oldest.tzinfo:
                oldest = oldest.replace(tzinfo=timezone.utc)
            cursor_dt = oldest

        # Check if we've reached the frontier (catch-up complete).
        if catching_up and frontier and cursor_dt <= frontier:
            catching_up = False
            logger.info("BACKFILL catch-up complete — switching to explore")

        # Only save the frontier when exploring past it.
        if not catching_up:
            _set_cursor(db, _BACKFILL_CURSOR_KEY, int(cursor_dt.timestamp()))

        # Check which posts we already have.
        pairs = [(row["author"], row["permlink"]) for row in rows]
        existing = _existing_author_permlinks(db, pairs)

        batch_processed = 0
        for row in rows:
            if stop_event.is_set():
                break

            if (row["author"], row["permlink"]) in existing:
                skipped += 1
                continue

            # Reputation filter.
            raw_rep = int(row.get("reputation") or 0)
            rep_score = _raw_rep_to_score(raw_rep)
            if rep_score < MIN_AUTHOR_REPUTATION:
                continue

            body = (row.get("body") or "").strip()
            created = row.get("created")
            if isinstance(created, datetime) and not created.tzinfo:
                created = created.replace(tzinfo=timezone.utc)

            try:
                _classify_and_save(
                    db, embedder, centroids, threshold,
                    pos_anchor, neg_anchor,
                    author=row["author"],
                    permlink=row["permlink"],
                    title=(row.get("title") or "").strip(),
                    body=body,
                    json_metadata=row.get("json_metadata"),
                    created=created,
                    label="CATCHUP" if catching_up else "BACKFILL",
                    parent_permlink=row.get("parent_permlink"),
                )
                batch_processed += 1
                total += 1
            except Exception:
                logger.exception("BACKFILL error on %s/%s", row["author"], row["permlink"])

        phase = "CATCHUP" if catching_up else "BACKFILL"
        logger.info("%s cursor=%s total=%d batch=%d skipped=%d",
                     phase, cursor_dt.isoformat(), total, batch_processed, skipped)

        # During catch-up, skip pause when entire batch was already known.
        if catching_up and batch_processed == 0:
            continue
        time.sleep(_BACKFILL_PAUSE)

    try:
        conn.close()
    except Exception:
        pass

    logger.info("BACKFILL finished — %d posts classified, %d skipped", total, skipped)


# ── Live stream ──────────────────────────────────────────────────────────────

_BATCH_SIZE = 10
_BATCH_TIMEOUT = 3.0  # seconds
_STREAM_TIMEOUT = 120  # seconds — Hive produces a block every 3s


def _parse_op_timestamp(op: dict) -> datetime | None:
    ts = op.get("timestamp")
    if not ts:
        return None
    try:
        if isinstance(ts, str):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def _process_batch(
    batch: list[dict], db: _DB, embedder, centroids, threshold: float,
    pos_anchor, neg_anchor, label: str,
) -> int:
    """Check reputations in batch and classify eligible posts. Returns count processed."""
    if not batch:
        return 0

    unique_authors = list({op["author"] for op in batch})
    reps = get_reputations(unique_authors)
    hafsql_available = len(reps) > 0 or len(unique_authors) == 0

    processed = 0
    for op in batch:
        author = op["author"]
        if author in reps:
            rep = reps[author]
        elif hafsql_available:
            rep = 0.0  # author not in reputations table
        else:
            # HAFSQL unreachable — skip rep check, classify anyway.
            rep = MIN_AUTHOR_REPUTATION
            logger.debug("HAFSQL unavailable — skipping rep check for %s", author)

        if rep < MIN_AUTHOR_REPUTATION:
            continue

        try:
            _classify_and_save(
                db, embedder, centroids, threshold,
                pos_anchor, neg_anchor,
                author=author,
                permlink=op.get("permlink", ""),
                title=op.get("title", ""),
                body=op.get("body", ""),
                json_metadata=op.get("json_metadata"),
                created=op.get("_created"),
                label=label,
                parent_permlink=op.get("parent_permlink"),
            )
            processed += 1
        except Exception:
            logger.exception("error on %s/%s", author, op.get("permlink"))
    return processed


def _stream_range(
    blockchain: Blockchain, hive_instance: Hive, db: _DB,
    embedder, centroids, threshold: float,
    pos_anchor, neg_anchor,
    start: int, stop: int | None, label: str,
    stop_event: threading.Event | None = None,
) -> None:
    post_count = 0
    last_block = start
    batch: list[dict] = []
    batch_start = time.monotonic()
    last_activity = time.monotonic()

    logger.info("%s streaming from block %d%s", label, start,
                f" to {stop}" if stop else " (live)")

    # Watchdog thread to detect hung streams (live mode only).
    watchdog_stop = threading.Event()
    if stop is None:  # live mode
        def _watchdog():
            while not watchdog_stop.is_set():
                if time.monotonic() - last_activity > _STREAM_TIMEOUT:
                    logger.error("Stream appears hung (%ds no activity) — forcing reconnect",
                                 _STREAM_TIMEOUT)
                    try:
                        hive_instance.rpc.close()
                    except Exception:
                        pass
                    return
                if stop_event and stop_event.is_set():
                    return
                watchdog_stop.wait(10)

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()

    def _flush():
        nonlocal batch, batch_start, post_count
        count = _process_batch(
            batch, db, embedder, centroids, threshold,
            pos_anchor, neg_anchor, label,
        )
        post_count += count
        batch = []
        batch_start = time.monotonic()

    try:
        for op in blockchain.stream(opNames=["comment"], start=start, stop=stop):
            last_activity = time.monotonic()

            if stop_event and stop_event.is_set():
                logger.info("%s stopping due to shutdown signal", label)
                break

            if op.get("parent_author") != "":
                continue

            block_num = op.get("block_num") or op.get("block") or last_block
            last_block = block_num
            op["_created"] = _parse_op_timestamp(op)
            batch.append(op)

            if len(batch) >= _BATCH_SIZE or (time.monotonic() - batch_start) >= _BATCH_TIMEOUT:
                _flush()

                if post_count % _CURSOR_UPDATE_INTERVAL == 0 and post_count > 0:
                    _set_cursor(db, _CURSOR_KEY, last_block)
    finally:
        watchdog_stop.set()

    # Flush remaining.
    if batch:
        _flush()

    # Save cursor on exit.
    if last_block > start:
        _set_cursor(db, _CURSOR_KEY, last_block)
    if stop is not None:
        logger.info("%s catch-up complete (%d posts)", label, post_count)


# ── Main ──────────────────────────────────────────────────────────────────────

def _stream() -> None:
    db = _DB()
    _seed_categories(db)

    embedder = _load_embedder()
    centroids = _load_centroids(db)
    threshold = 0.38

    if embedder:
        pos_anchor, neg_anchor = _build_sentiment_anchors(embedder)
    else:
        pos_anchor = neg_anchor = np.zeros(384)

    # Graceful shutdown via SIGTERM.
    stop_event = threading.Event()

    def _handle_sigterm(signum, frame):
        logger.info("SIGTERM received — shutting down gracefully")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Start backfill thread.
    backfill = threading.Thread(
        target=_backfill_thread,
        args=(db, embedder, centroids, threshold, pos_anchor, neg_anchor, stop_event),
        daemon=True,
    )
    backfill.start()

    # Run the live stream in the main thread.
    # Skip block-by-block CATCHUP — the backfill thread's catch-up phase
    # (starting from NOW, working backwards) already covers missed posts.
    try:
        hive = Hive()
        blockchain = Blockchain(hive_instance=hive)
        head_block = blockchain.get_current_block_num()
        last_cursor = _get_cursor(db, _CURSOR_KEY)

        if last_cursor and (head_block - last_cursor) >= _CATCHUP_THRESHOLD:
            logger.info(
                "Gap: cursor=%d  head=%d  (%d blocks behind) — skipping to head, backfill covers gaps",
                last_cursor, head_block, head_block - last_cursor,
            )
            head_block = blockchain.get_current_block_num()

        if not stop_event.is_set():
            _stream_range(
                blockchain, hive, db, embedder, centroids, threshold,
                pos_anchor, neg_anchor,
                head_block, None, "LIVE",
                stop_event=stop_event,
            )
    finally:
        stop_event.set()
        backfill.join(timeout=10)
        db.close()


def run() -> None:
    while True:
        try:
            _stream()
        except Exception as exc:
            logger.error("stream disconnected: %s — reconnecting in %ds", exc, _RECONNECT_DELAY)
            time.sleep(_RECONNECT_DELAY)


if __name__ == "__main__":
    run()
