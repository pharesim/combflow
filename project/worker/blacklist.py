"""Check authors against the Global Blacklist API (blacklist.usehive.com)."""
import logging
import time
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

# In-memory cache: author -> (is_blacklisted, timestamp)
_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 3600 * 6  # 6 hours
_TIMEOUT = 5  # seconds


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
        blacklisted = body.strip() not in ("[]", "")
        _cache[author] = (blacklisted, now)
        if blacklisted:
            logger.info("BLACKLIST author %s is blacklisted: %s", author, body.strip()[:200])
        return blacklisted
    except (URLError, OSError, ValueError) as exc:
        logger.debug("BLACKLIST API error for %s: %s — failing open", author, exc)
        return False


def check_authors(authors: list[str]) -> set[str]:
    """Return the set of blacklisted authors from the given list."""
    return {a for a in authors if is_blacklisted(a)}
