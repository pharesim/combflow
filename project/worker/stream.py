"""Live Hive blockchain stream processing."""
import logging
import threading
import time
from datetime import datetime, timezone

from ..hafsql import get_reputations
from .blacklist import check_authors
from .bridge import _set_cursor
from .classify import _classify_and_save, MIN_AUTHOR_REPUTATION
from .health import touch_heartbeat

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10
_BATCH_TIMEOUT = 3.0  # seconds
_STREAM_TIMEOUT = 120  # seconds — Hive produces a block every 3s
_CURSOR_UPDATE_INTERVAL = 50
_CURSOR_KEY = "live_worker"


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
    batch: list[dict], db, embedder, centroids, threshold: float,
    pos_anchor, neg_anchor, label: str,
) -> int:
    """Check reputations in batch and classify eligible posts. Returns count processed."""
    if not batch:
        return 0

    unique_authors = list({op["author"] for op in batch})
    blacklisted = check_authors(unique_authors)
    reps = get_reputations(unique_authors)
    hafsql_available = len(reps) > 0 or len(unique_authors) == 0

    processed = 0
    for op in batch:
        author = op["author"]
        if author in blacklisted:
            continue
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
    blockchain, hive_instance, db,
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
                touch_heartbeat()

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
