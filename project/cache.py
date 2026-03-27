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


def cached_response(key: str, ttl: int):
    """Decorator that caches endpoint return values."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            result = get(key)
            if result is not None:
                return result
            if key not in _locks:
                _locks[key] = asyncio.Lock()
            async with _locks[key]:
                result = get(key)  # Double-check after lock
                if result is not None:
                    return result
                result = await func(*args, **kwargs)
                put(key, result, ttl=ttl)
                return result
        return wrapper
    return decorator


def clear() -> None:
    _store.clear()
    _locks.clear()
