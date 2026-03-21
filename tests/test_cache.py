"""Tests for project.cache — in-process TTL cache."""
import time
from unittest.mock import patch

from project import cache


class TestCache:
    def setup_method(self):
        cache._store.clear()

    def test_get_missing_key(self):
        assert cache.get("nonexistent") is None

    def test_put_and_get(self):
        cache.put("key", {"data": 42}, ttl=60)
        assert cache.get("key") == {"data": 42}

    def test_expired_returns_none(self):
        now = time.monotonic()
        cache.put("key", "value", ttl=10)
        # Simulate time advancing past expiry.
        with patch("time.monotonic", return_value=now + 20):
            assert cache.get("key") is None

    def test_invalidate(self):
        cache.put("key", "value", ttl=60)
        cache.invalidate("key")
        assert cache.get("key") is None

    def test_invalidate_missing_key(self):
        cache.invalidate("nope")  # Should not raise.
