"""Shared Hive app → canonical-URL list.

Source of truth: https://github.com/pharesim/hive-apps-canonical
Fetched at startup and refreshed daily from the upstream raw JSON. Stays
empty until the first successful refresh — pages rendered before then
simply skip the canonical tag (same fallthrough as for unknown apps).

Each value is a URL template with {author} and {permlink} placeholders.
"""
import logging

import httpx

logger = logging.getLogger(__name__)

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/pharesim/"
    "hive-apps-canonical/main/apps-canonical-list.json"
)

# Module-level dict, replaced wholesale on each successful refresh.
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


async def refresh_from_upstream(client: httpx.AsyncClient) -> bool:
    """Fetch the upstream list; replace APP_CANONICAL_URLS on success."""
    global APP_CANONICAL_URLS
    try:
        # Explicit opt-in: the shared client now defaults to follow_redirects=
        # False (proposal 101). This fetches a hardcoded, trusted URL (no user
        # input → no SSRF concern), so following a GitHub-raw redirect is safe.
        resp = await client.get(UPSTREAM_URL, timeout=10.0, follow_redirects=True)
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
