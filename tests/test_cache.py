"""Tests for project.cache — in-process TTL cache."""
import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
        call_count = 0

        @cache.cached_response("ttl_key", ttl=10)
        async def my_func():
            nonlocal call_count
            call_count += 1
            return "data"

        with patch("project.cache.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            await my_func()
            assert call_count == 1

            # Before expiry — should still be cached.
            mock_time.monotonic.return_value = 1009.0
            await my_func()
            assert call_count == 1

            # After expiry — cache should miss, function called again.
            mock_time.monotonic.return_value = 1011.0
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


class TestGetOrCompute:
    """The double-checked-lock cache fill behind the cache-stampede fix
    (proposal 104 #3). Tested here, at the helper, rather than via concurrent
    asyncpg connections in test_crud (which destabilises the session-scoped loop):
    get_recent_posts_for_seo routes through this."""

    def setup_method(self):
        cache.clear()

    async def test_collapses_concurrent_misses(self):
        """N concurrent cold-cache callers run the producer exactly once, then all
        observe the same cached value — the stampede collapses to one round-trip."""
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)  # yield so all 5 callers pile up on the lock
            return {"n": calls}

        results = await asyncio.gather(
            *[cache.get_or_compute("stampede", 60, producer) for _ in range(5)]
        )
        assert calls == 1                       # one producer call, not five
        assert all(r == {"n": 1} for r in results)

    async def test_hits_cache_without_relocking(self):
        """A warm cache returns the stored value without invoking the producer."""
        cache.put("warm", {"v": 1}, ttl=60)
        called = False

        async def producer():
            nonlocal called
            called = True
            return {"v": 2}

        assert await cache.get_or_compute("warm", 60, producer) == {"v": 1}
        assert called is False

    async def test_does_not_cache_none(self):
        """A producer returning None isn't cached (None reads back as a miss), so
        the next caller recomputes — callers needing to cache 'absent' use a
        non-None sentinel."""
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            return None

        await cache.get_or_compute("nullable", 60, producer)
        await cache.get_or_compute("nullable", 60, producer)
        assert calls == 2


class TestCacheBound:
    """B4 (proposal 110): the store is capped so unbounded keys (per-post
    top_comments, per-author summaries) can't grow for the worker's lifetime."""

    def setup_method(self):
        cache._store.clear()
        cache._locks.clear()

    def test_caps_entry_count(self):
        from project.cache import _MAX_ENTRIES
        for i in range(_MAX_ENTRIES + 500):
            cache.put(f"k{i}", i, ttl=3600)
        assert len(cache._store) <= _MAX_ENTRIES
        # FIFO drops the oldest-inserted; the newest key survives.
        assert cache.get(f"k{_MAX_ENTRIES + 499}") == _MAX_ENTRIES + 499

    def test_evict_reclaims_expired_first(self):
        # _evict purges expired entries before resorting to FIFO eviction.
        now = time.monotonic()
        cache._store["old"] = (now - 1, "x")     # already expired
        cache._store["live"] = (now + 100, "y")  # still valid
        cache._evict()
        assert "old" not in cache._store
        assert "live" in cache._store


class TestCacheConcurrency:
    """Proposal 111: ``_store`` is mutated off the event loop. ``get_top_comments``
    (hafsql.py) runs ``cache.get``/``cache.put`` directly inside ``asyncio.to_thread``
    workers on the post path (ui.py), concurrently with the event-loop thread. Once the
    store is over ``_MAX_ENTRIES`` — the steady state under a crawler sweep, i.e. exactly
    the condition 110 B4's cap was added for — every ``put`` runs ``_evict``, whose
    iteration of ``_store`` races those cross-thread mutations. Without the
    ``threading.Lock`` guard this raises ``RuntimeError: dictionary changed size during
    iteration`` (and occasionally ``KeyError`` from a doubly-deleted key), which propagates
    to an HTTP 500 on the SEO crawler path. Mutation-verified: deleting ``_store_lock``
    (or its ``with`` guards) makes this test fail."""

    def setup_method(self):
        cache.clear()
        # Force frequent thread switches so _evict's iteration of _store is reliably
        # preempted mid-pass by another thread's mutation. Otherwise CPython's GIL can
        # run the whole sub-millisecond iteration within a single time slice and the
        # race never surfaces — making the mutation-verification flaky. Restored in
        # teardown so the rest of the suite keeps the default scheduling.
        self._switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)

    def teardown_method(self):
        sys.setswitchinterval(self._switch_interval)
        cache.clear()

    def test_concurrent_put_get_over_cap_never_raises(self):
        """Many threads hammering put/get while the store sits over the cap must never
        raise, and the cap must hold throughout."""
        from project.cache import _MAX_ENTRIES

        # Prime the store to the cap with live entries, so every worker put tips len
        # past _MAX_ENTRIES and triggers _evict's full-store iteration — the exact
        # window that races a concurrent put/get from another thread.
        for i in range(_MAX_ENTRIES):
            cache.put(f"seed{i}", i, ttl=3600)

        workers, iterations = 8, 300
        barrier = threading.Barrier(workers)
        errors: list[BaseException] = []

        def hammer(worker_id: int) -> None:
            barrier.wait()  # release all workers at once to maximise overlap
            try:
                for n in range(iterations):
                    key = f"w{worker_id}:{n}"
                    cache.put(key, n, ttl=3600)
                    cache.get(key)
                    cache.get(f"seed{n % _MAX_ENTRIES}")
            except BaseException as exc:  # noqa: BLE001 — capture for the assertion
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in [pool.submit(hammer, w) for w in range(workers)]:
                future.result()  # re-raise anything hammer didn't catch

        assert not errors, (
            f"concurrent cache access raised {type(errors[0]).__name__}: {errors[0]}"
        )
        # Eviction kept the store bounded throughout the concurrent hammering.
        assert len(cache._store) <= _MAX_ENTRIES
