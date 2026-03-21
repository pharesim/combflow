"""Lightweight in-process TTL cache for near-static data."""
import time

_store: dict[str, tuple[float, object]] = {}  # key -> (expires_at, value)


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


def clear() -> None:
    _store.clear()
