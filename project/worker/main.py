"""Worker orchestrator — starts live stream and backfill threads."""
import logging
import signal
import threading
import time

import numpy as np
from nectar import Hive
from nectar.blockchain import Blockchain

from .backfill import _backfill_thread
from .bridge import _DB, _get_cursor, _seed_categories
from .classify import _load_embedder, _load_centroids, _build_sentiment_anchors, _EMBEDDING_DIM
from .stream import _stream_range

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

_RECONNECT_DELAY = 10
_CURSOR_KEY = "live_worker"
_CATCHUP_THRESHOLD = 200


def _stream() -> None:
    db = _DB()
    _seed_categories(db)

    embedder = _load_embedder()
    centroids = _load_centroids(db)
    threshold = 0.38

    if embedder:
        pos_anchor, neg_anchor = _build_sentiment_anchors(embedder)
    else:
        pos_anchor = neg_anchor = np.zeros(_EMBEDDING_DIM)

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
