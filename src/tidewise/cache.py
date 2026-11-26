"""A tiny JSON disk cache and a polite rate limiter for the live API."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class DiskCache:
    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self.directory = directory
        self._clock = clock

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")

    def get(self, key: str, ttl: float) -> Any | None:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if self._clock() - entry["saved_at"] > ttl:
            return None
        return entry["data"]

    def put(self, key: str, data: Any) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(json.dumps({"saved_at": self._clock(), "data": data}))
        except OSError:
            pass  # a cache that cannot write should never break a request


class RateLimiter:
    """Allow one request every `min_interval` seconds."""

    def __init__(
        self,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            if self._last is not None:
                remaining = self.min_interval - (self._clock() - self._last)
                if remaining > 0:
                    self._sleep(remaining)
            self._last = self._clock()
