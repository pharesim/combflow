"""HAFSQL backfill thread — walks backwards through older posts."""
import logging
import threading
import time
from datetime import datetime, timezone

from ..hafsql import _raw_rep_to_score, build_dsn
from .blacklist import is_blacklisted
from .bridge import _get_cursor, _set_cursor, _existing_author_permlinks
from .classify import _classify_and_save, MIN_AUTHOR_REPUTATION
from .health import touch_heartbeat

logger = logging.getLogger(__name__)

_BACKFILL_BATCH = 100
_CATCHUP_BATCH = 1000
_BACKFILL_PAUSE = 2  # seconds between batches
_BACKFILL_CURSOR_KEY = "backfill_worker"
_BACKOFF_MIN = 10    # seconds
_BACKOFF_MAX = 300   # 5 minutes


def _backfill_thread(
    db, embedder, centroids, threshold: float,
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

    def _connect():
        nonlocal conn
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        conn = psycopg2.connect(build_dsn())
        conn.autocommit = True

    # Retry initial connection with exponential backoff.
    while not stop_event.is_set():
        try:
            _connect()
            break
        except Exception as exc:
            delay = min(_BACKOFF_MIN * (2 ** retries), _BACKOFF_MAX)
            retries += 1
            logger.warning("BACKFILL cannot connect to HAFSQL (retry %d, next in %ds): %s",
                           retries, delay, exc)
            stop_event.wait(delay)
    else:
        return
    retries = 0

    # The frontier is the farthest-back timestamp we've fully processed.
    saved_ts = _get_cursor(db, _BACKFILL_CURSOR_KEY)
    if saved_ts:
        frontier = datetime.fromtimestamp(saved_ts, tz=timezone.utc)
    else:
        frontier = None  # first run — no frontier yet

    # Always start scanning from NOW so recent posts are classified first.
    #
    # Proposal 102 F8: paginate by the row-value keyset (created, author,
    # permlink), not by ``created`` alone. Hive blocks are 3s and many
    # top-level posts share a created-second; with a bare ``created < cursor``
    # filter, whenever LIMIT bisected a same-second group the remainder was
    # excluded forever (strict ``<`` on the next round skipped the whole
    # second). The tuple tiebreaker walks *within* a second without dropping
    # rows. ``cursor_author``/``cursor_permlink`` start empty so the first
    # query is exactly ``created < NOW`` (empty strings sort lowest, so no real
    # row equals the seed) — identical to the previous first-batch behaviour.
    cursor_dt = datetime.now(timezone.utc)
    cursor_author = ""
    cursor_permlink = ""
    catching_up = frontier is not None
    if catching_up:
        logger.info("BACKFILL catch-up: NOW -> %s, then explore further back",
                     frontier.isoformat())
    else:
        logger.info("BACKFILL first run: starting from NOW")

    while not stop_event.is_set():
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(
                    """
                    SELECT c.author, c.permlink, c.title, c.body, c.created,
                           c.json_metadata, c.parent_permlink,
                           r.reputation
                    FROM hafsql.comments c
                    LEFT JOIN hafsql.reputations r ON c.author = r.account_name
                    WHERE c.parent_author = ''
                      AND c.deleted = false
                      AND LENGTH(c.body) >= 80
                      AND (c.created, c.author, c.permlink) < (%s, %s, %s)
                    ORDER BY c.created DESC, c.author DESC, c.permlink DESC
                    LIMIT %s
                    """,
                    (cursor_dt, cursor_author, cursor_permlink,
                     _CATCHUP_BATCH if catching_up else _BACKFILL_BATCH),
                )
                rows = cur.fetchall()
            finally:
                cur.close()
            retries = 0
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            delay = min(_BACKOFF_MIN * (2 ** retries), _BACKOFF_MAX)
            retries += 1
            logger.warning("BACKFILL connection failed (retry %d, next in %ds): %s",
                           retries, delay, exc)
            stop_event.wait(delay)
            try:
                _connect()
            except Exception:
                pass
            continue
        except Exception as exc:
            retries += 1
            if retries > 3:
                logger.error("BACKFILL permanent error after %d retries: %s", retries, exc)
                raise
            delay = min(_BACKOFF_MIN * (2 ** retries), _BACKOFF_MAX)
            logger.warning("BACKFILL error (retry %d): %s — waiting %.0fs", retries, exc, delay)
            stop_event.wait(delay)
            continue

        if not rows:
            logger.info("BACKFILL reached end of HAFSQL posts — done")
            break

        # Advance the keyset cursor to the oldest post in this batch (the tail,
        # since rows are ordered created/author/permlink DESC).
        oldest = rows[-1].get("created")
        if isinstance(oldest, datetime):
            if not oldest.tzinfo:
                oldest = oldest.replace(tzinfo=timezone.utc)
            cursor_dt = oldest
            cursor_author = rows[-1].get("author") or ""
            cursor_permlink = rows[-1].get("permlink") or ""
        else:
            # Skip batch if we can't extract a valid cursor
            logger.warning("BACKFILL batch has non-datetime 'created': %r — skipping", oldest)
            break

        # Check if we've reached the frontier (catch-up complete).
        if catching_up and frontier and cursor_dt <= frontier:
            catching_up = False
            logger.info("BACKFILL catch-up complete — switching to explore")

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

            # Blacklist filter.
            if is_blacklisted(row["author"]):
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

        # Save cursor AFTER batch processing to avoid data loss on crash.
        # The persisted frontier stays a plain timestamp (the stream_cursors
        # column is an integer): it's only the catch-up stop boundary, never the
        # explore resume point. Each restart re-walks from NOW back to the
        # frontier with the keyset tuple above and dedups via
        # _existing_author_permlinks, so the author/permlink tiebreakers don't
        # need persisting — the in-memory tuple already prevents same-second
        # gaps within and across runs. Reading the legacy int as (ts, '', '')
        # (proposal 102 F8 lock) needs no migration.
        _set_cursor(db, _BACKFILL_CURSOR_KEY, int(cursor_dt.timestamp()))

        phase = "CATCHUP" if catching_up else "BACKFILL"
        logger.info("%s cursor=%s total=%d batch=%d skipped=%d",
                     phase, cursor_dt.isoformat(), total, batch_processed, skipped)
        touch_heartbeat()

        # During catch-up, skip pause when entire batch was already known.
        if catching_up and batch_processed == 0:
            continue
        time.sleep(_BACKFILL_PAUSE)

    try:
        conn.close()
    except Exception:
        pass

    logger.info("BACKFILL finished — %d posts classified, %d skipped", total, skipped)
