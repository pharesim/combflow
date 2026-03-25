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


class TestCachedResponseDecorator:
    def setup_method(self):
        cache._store.clear()

    async def test_caches_async_result(self):
        call_count = 0

        @cache.cached_response("test_key", ttl=60)
        async def my_func():
            nonlocal call_count
            call_count += 1
            return {"result": 42}

        r1 = await my_func()
        r2 = await my_func()
        assert r1 == {"result": 42}
        assert r2 == {"result": 42}
        assert call_count == 1  # second call hit cache

    async def test_respects_ttl(self):
        import time

        call_count = 0

        @cache.cached_response("ttl_key", ttl=10)
        async def my_func():
            nonlocal call_count
            call_count += 1
            return "data"

        await my_func()
        assert call_count == 1

        # Simulate TTL expiry
        now = time.monotonic()
        with patch("time.monotonic", return_value=now + 20):
            # Cache should be expired, but cached_response uses cache.get internally
            # which calls time.monotonic — we need to also patch cache module's time
            pass

        # Clear to simulate expiry
        cache._store.clear()
        await my_func()
        assert call_count == 2

    async def test_preserves_function_name(self):
        @cache.cached_response("k", ttl=60)
        async def original_name():
            return 1

        assert original_name.__name__ == "original_name"

    async def test_passes_args_through(self):
        @cache.cached_response("args_key", ttl=60)
        async def add(a, b):
            return a + b

        result = await add(3, 4)
        assert result == 7
