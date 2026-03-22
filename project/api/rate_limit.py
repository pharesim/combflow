"""Sliding-window in-memory rate limiter."""
import time
from collections import deque

from fastapi import HTTPException

_MAX_TRACKED = 50_000


class RateLimiter:
    """Per-key sliding window rate limiter with periodic stale-key purge."""

    def __init__(self, window_seconds: int, max_requests: int, status_code: int = 429):
        self.window = window_seconds
        self.max_requests = max_requests
        self.status_code = status_code
        self._log: dict[str, deque] = {}
        self._purge_counter = 0

    def check(self, key: str, detail: str = "Rate limited"):
        now = time.monotonic()
        dq = self._log.setdefault(key, deque())
        # Expire old entries.
        while dq and dq[0] < now - self.window:
            dq.popleft()
        if len(dq) >= self.max_requests:
            raise HTTPException(status_code=self.status_code, detail=detail)
        dq.append(now)
        # Periodic purge of stale keys.
        self._purge_counter += 1
        if self._purge_counter >= 1000:
            self._purge_counter = 0
            cutoff = now - self.window
            stale = [k for k, v in self._log.items() if not v or v[-1] < cutoff]
            for k in stale:
                del self._log[k]
            if len(self._log) > _MAX_TRACKED:
                self._log.clear()
