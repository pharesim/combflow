"""Check authors against the Global Blacklist API (blacklist.usehive.com)."""
import json
import logging
import threading
import time
from collections import OrderedDict
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

# In-memory cache: author -> (is_blacklisted, timestamp). OrderedDict for FIFO eviction.
_cache: OrderedDict[str, tuple[bool, float]] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_TTL = 3600 * 6  # 6 hours
_MAX_CACHE = 100_000
_TIMEOUT = 5  # seconds
_SWEEP_INTERVAL = 86400  # 24 hours between sweeps
_SWEEP_RATE_LIMIT = 0.5  # seconds between API calls during sweep


def is_blacklisted(author: str) -> bool:
    """Check if an author is on any blacklist. Returns False on API errors (fail open)."""
    now = time.monotonic()
    cached = _cache.get(author)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    try:
        req = Request(
            f"https://blacklist.usehive.com/user/{author}",
            headers={"User-Agent": "CombFlow/1.0"},
        )
        with urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        # API returns a JSON array of blacklist entries. Empty array = not blacklisted.
        try:
            data = json.loads(body)
            blacklisted = isinstance(data, list) and len(data) > 0
        except (json.JSONDecodeError, ValueError):
            # API returned non-JSON (error page, etc.) — fail open
            logger.warning("Blacklist API returned non-JSON for %s", author)
            blacklisted = False
        with _cache_lock:
            _cache[author] = (blacklisted, now)
            _cache.move_to_end(author)
            while len(_cache) > _MAX_CACHE:
                _cache.popitem(last=False)
        if blacklisted:
            logger.info("BLACKLIST author %s is blacklisted: %s", author, body.strip()[:200])
        return blacklisted
    except (URLError, OSError, ValueError) as exc:
        logger.debug("BLACKLIST API error for %s: %s — failing open", author, exc)
        return False


def check_authors(authors: list[str]) -> set[str]:
    """Return the set of blacklisted authors from the given list."""
    return {a for a in authors if is_blacklisted(a)}


def sweep_thread(db, stop_event: threading.Event) -> None:
    """Daily re-check of all authors in the DB against the blacklist.

    Newly blacklisted authors have their posts deleted.
    """
    from .bridge import _get_distinct_authors, _delete_posts_by_author

    # Wait before first sweep to let the worker start up.
    if stop_event.wait(60):
        return

    while not stop_event.is_set():
        try:
            # Prune expired cache entries before sweep.
            with _cache_lock:
                now = time.monotonic()
                expired = [k for k, (_, ts) in _cache.items() if now - ts >= _CACHE_TTL]
                for k in expired:
                    del _cache[k]

            authors: list[str] = []
            offset = 0
            while True:
                batch = _get_distinct_authors(db, limit=10_000, offset=offset)
                if not batch:
                    break
                authors.extend(batch)
                offset += len(batch)
            logger.info("BLACKLIST sweep: checking %d authors", len(authors))
            removed = 0
            for author in authors:
                if stop_event.is_set():
                    break
                if is_blacklisted(author):
                    count = _delete_posts_by_author(db, author)
                    if count > 0:
                        logger.info("BLACKLIST sweep: deleted %d posts by %s", count, author)
                        removed += count
                time.sleep(_SWEEP_RATE_LIMIT)
            logger.info("BLACKLIST sweep complete: removed %d posts", removed)
        except Exception:
            logger.exception("BLACKLIST sweep error")

        stop_event.wait(_SWEEP_INTERVAL)
