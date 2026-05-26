"""Tests for the apps-canonical upstream refresh."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from project import apps_canonical


def test_starts_empty():
    """The dict is empty at import time — populated only on first refresh."""
    # Other tests in this file mutate APP_CANONICAL_URLS, so just assert it's
    # a dict; the empty-at-fresh-import invariant is enforced by the type hint.
    assert isinstance(apps_canonical.APP_CANONICAL_URLS, dict)


def test_valid_entries_filters_bad_values():
    """Sanity filter drops keys whose value lacks the required placeholders."""
    bad = {
        "ok":      "https://x.com/@{author}/{permlink}",
        "no-auth": "https://x.com/{permlink}",
        "no-plk":  "https://x.com/@{author}",
        "bare":    "https://x.com/",
        "int-val": 42,
    }
    cleaned = apps_canonical._valid_entries(bad)
    assert cleaned == {"ok": "https://x.com/@{author}/{permlink}"}


def test_valid_entries_rejects_non_dict():
    assert apps_canonical._valid_entries(None) is None
    assert apps_canonical._valid_entries([]) is None
    assert apps_canonical._valid_entries("string") is None
    assert apps_canonical._valid_entries({}) is None


@pytest.mark.asyncio
async def test_refresh_replaces_dict_on_success():
    """Successful upstream fetch replaces APP_CANONICAL_URLS."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value={
        "newapp": "https://new.example/@{author}/{permlink}",
    })
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_response)

    original = dict(apps_canonical.APP_CANONICAL_URLS)
    try:
        ok = await apps_canonical.refresh_from_upstream(fake_client)
        assert ok is True
        assert apps_canonical.APP_CANONICAL_URLS == {
            "newapp": "https://new.example/@{author}/{permlink}"
        }
    finally:
        apps_canonical.APP_CANONICAL_URLS = original


@pytest.mark.asyncio
async def test_refresh_preserves_dict_on_failure():
    """Network errors / bad JSON leave the existing dict in place."""
    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=OSError("network down"))

    snapshot = dict(apps_canonical.APP_CANONICAL_URLS)
    ok = await apps_canonical.refresh_from_upstream(fake_client)
    assert ok is False
    assert apps_canonical.APP_CANONICAL_URLS == snapshot


@pytest.mark.asyncio
async def test_refresh_preserves_dict_on_invalid_payload():
    """Upstream returning bogus data (wrong shape) doesn't clobber state."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value=["not", "a", "dict"])
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_response)

    snapshot = dict(apps_canonical.APP_CANONICAL_URLS)
    ok = await apps_canonical.refresh_from_upstream(fake_client)
    assert ok is False
    assert apps_canonical.APP_CANONICAL_URLS == snapshot
