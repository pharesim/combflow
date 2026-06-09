"""Lightweight in-process TTL cache for near-static data."""
import asyncio
import functools
import time

_store: dict[str, tuple[float, object]] = {}  # key -> (expires_at, value)
_locks: dict[str, asyncio.Lock] = {}


def get(key: str) -> object | None:
    entry = _store.get(key)
    if entry is None:
        return None
    if entry[0] > time.monotonic():
        return entry[1]
    del _store[key]
    return None


def put(key: str, value: object, ttl: float) -> None:
    _store[key] = (time.monotonic() + ttl, value)


def invalidate(key: str) -> None:
    _store.pop(key, None)


async def get_or_compute(key: str, ttl: float, producer):
    """Double-checked, lock-guarded cache fill.

    Returns the cached value when present; otherwise runs ``producer`` (an async
    zero-arg callable) under a per-key lock so a cold-cache stampede — N
    concurrent callers all missing at once — collapses into a single
    ``producer()`` call followed by N cache hits, rather than N parallel
    round-trips to PG/HAFSQL.

    A ``producer`` that returns ``None`` is treated as "nothing to cache": the
    value isn't stored (caching ``None`` is indistinguishable from a miss via
    ``get`` anyway, so it would force every caller to recompute). Callers that
    legitimately need to cache an "absent" result must use a non-``None``
    sentinel (e.g. ``""``). This mirrors the ``cached_response`` contract, whose
    endpoint results are always non-None dicts.
    """
    result = get(key)
    if result is not None:
        return result
    lock = _locks.get(key)
    if lock is None:
        # No await between get() and setdefault(), so the single-threaded event
        # loop can't race here; setdefault is belt-and-suspenders.
        lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        result = get(key)  # Double-check after acquiring the lock.
        if result is not None:
            return result
        result = await producer()
        if result is not None:
            put(key, result, ttl=ttl)
        return result


def cached_response(key: str, ttl: int):
    """Decorator that caches endpoint return values (double-checked lock)."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await get_or_compute(key, ttl, lambda: func(*args, **kwargs))
        return wrapper
    return decorator


def clear() -> None:
    _store.clear()
    _locks.clear()
