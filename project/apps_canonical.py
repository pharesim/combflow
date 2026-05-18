"""Shared Hive app → canonical-URL list.

Source of truth: https://github.com/pharesim/hive-apps-canonical
Refreshed daily from the upstream raw JSON; falls back to the bundled
snapshot in apps_canonical_fallback.json when upstream is unreachable.

Each value is a URL template with {author} and {permlink} placeholders.
"""
import json
import logging
import pathlib

import httpx

logger = logging.getLogger(__name__)

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/pharesim/"
    "hive-apps-canonical/main/apps-canonical-list.json"
)
_FALLBACK_PATH = pathlib.Path(__file__).parent / "apps_canonical_fallback.json"

# Always-populated module-level dict. Loaded synchronously from the bundled
# fallback at import time; replaced by refresh_from_upstream() on first
# successful daily fetch.
APP_CANONICAL_URLS: dict[str, str] = {}


def _valid_entries(data: object) -> dict[str, str] | None:
    """Return data as a dict[str,str] if it passes sanity checks, else None."""
    if not isinstance(data, dict) or not data:
        return None
    cleaned: dict[str, str] = {}
    for k, v in data.items():
        if (
            isinstance(k, str)
            and isinstance(v, str)
            and "{author}" in v
            and "{permlink}" in v
        ):
            cleaned[k] = v
    return cleaned or None


def _load_fallback() -> None:
    """Populate APP_CANONICAL_URLS from the bundled fallback file."""
    global APP_CANONICAL_URLS
    try:
        data = json.loads(_FALLBACK_PATH.read_text())
    except Exception as exc:
        logger.warning("apps-canonical fallback load failed: %s", exc)
        return
    valid = _valid_entries(data)
    if valid is not None:
        APP_CANONICAL_URLS = valid


async def refresh_from_upstream(client: httpx.AsyncClient) -> bool:
    """Fetch the upstream list; replace APP_CANONICAL_URLS on success."""
    global APP_CANONICAL_URLS
    try:
        resp = await client.get(UPSTREAM_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        valid = _valid_entries(data)
        if valid is None:
            raise ValueError("upstream list rejected by sanity check")
        APP_CANONICAL_URLS = valid
        logger.info(
            "apps-canonical refreshed from upstream (%d entries)", len(valid)
        )
        return True
    except Exception as exc:
        logger.warning("apps-canonical upstream refresh failed: %s", exc)
        return False


# Seed at import time so the dict is never empty.
_load_fallback()
