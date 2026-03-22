"""HAFSQL backfill thread — walks backwards through older posts."""
import logging
import threading
import time
from datetime import datetime, timezone

from ..hafsql import _raw_rep_to_score, build_dsn
from .bridge import _get_cursor, _set_cursor, _existing_author_permlinks
from .classify import _classify_and_save, MIN_AUTHOR_REPUTATION

logger = logging.getLogger(__name__)

_BACKFILL_BATCH = 100
_BACKFILL_PAUSE = 2  # seconds between batches
_BACKFILL_CURSOR_KEY = "backfill_worker"


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
