"""Lightweight in-process TTL cache for near-static data."""
import asyncio
import functools
import threading
import time

_store: dict[str, tuple[float, object]] = {}  # key -> (expires_at, value)
_locks: dict[str, asyncio.Lock] = {}

# Proposal 111: ``_store`` is mutated from more than one OS thread. ``get_or_compute``
# runs on the event loop, but ``get_top_comments`` (hafsql.py) calls ``get``/``put``
# directly from ``asyncio.to_thread`` workers (ui.py post path). The per-key
# ``asyncio.Lock`` in ``_locks`` is event-loop-scoped and does NOT guard cross-thread
# access, so ``_evict``'s iteration of ``_store`` (added by 110 B4) could race a
# concurrent ``put``/``get`` from another thread and raise ``RuntimeError: dictionary
# changed size during iteration``. ``_store_lock`` (a real ``threading.Lock``) serialises
# every *synchronous* ``_store``/``_locks`` mutation across threads. It is non-reentrant
# and is NEVER held across an ``await`` — see ``get_or_compute``.
_store_lock = threading.Lock()

# Proposal 110 B4: bound the store. Most keys are fixed (languages, stats,
# category tree) or bounded (per-filter browse counts), but a few are keyed on
# unvalidated path params — per-post ``top_comments:{author}/{permlink}`` and
# per-author summaries — so without a cap the dict grows for the worker's
# lifetime. Cap + expired-then-FIFO eviction keeps it bounded.
_MAX_ENTRIES = 4096


def get(key: str) -> object | None:
    with _store_lock:
        entry = _store.get(key)
        if entry is None:
            return None
        if entry[0] > time.monotonic():
            return entry[1]
        del _store[key]
        return None


def _evict() -> None:
    """Reclaim space: drop expired entries first, then oldest-inserted (FIFO).

    Caller MUST hold ``_store_lock`` (only ever called from ``put``, which already
    holds it). ``_store_lock`` is non-reentrant, so ``_evict`` must NOT re-acquire it.
    """
    now = time.monotonic()
    for k in [k for k, (exp, _) in _store.items() if exp <= now]:
        del _store[k]
        _locks.pop(k, None)
    # Plain dict preserves insertion order, so the first key is the oldest.
    while len(_store) > _MAX_ENTRIES:
        oldest = next(iter(_store))
        del _store[oldest]
        _locks.pop(oldest, None)


def put(key: str, value: object, ttl: float) -> None:
    with _store_lock:
        _store[key] = (time.monotonic() + ttl, value)
        if len(_store) > _MAX_ENTRIES:
            _evict()


def invalidate(key: str) -> None:
    with _store_lock:
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
    with _store_lock:
        _store.clear()
        _locks.clear()
