"""GET requests that are cached on disk, rate limited and retried.

Shared by the tide source (IWLS) and the weather source (MSC GeoMet).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from .cache import DiskCache, RateLimiter
from .errors import TidewiseError


class CachedClient:
    def __init__(
        self,
        name: str,
        client: httpx.Client,
        cache: DiskCache,
        *,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 3,
    ) -> None:
        self._name = name
        self._client = client
        self._cache = cache
        self._limiter = limiter or RateLimiter(1.0)
        self._sleep = sleep
        self._retries = retries

    def get(self, path: str, params: dict[str, str], ttl: float) -> Any:
        key = f"{self._client.base_url}{path}?" + "&".join(
            f"{k}={params[k]}" for k in sorted(params)
        )
        cached = self._cache.get(key, ttl)
        if cached is not None:
            return cached
        delay = 1.0
        for attempt in range(self._retries):
            self._limiter.wait()
            try:
                resp = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == self._retries - 1:
                    raise TidewiseError(
                        f"Could not reach the {self._name} service ({type(exc).__name__}). "
                        "Try again in a moment."
                    ) from exc
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    self._cache.put(key, data)
                    return data
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise TidewiseError(
                        f"{self._name} returned HTTP {resp.status_code} for {path}. "
                        "Check the station and the time range."
                    )
                if attempt == self._retries - 1:
                    raise TidewiseError(
                        f"{self._name} is busy (HTTP {resp.status_code}). Try again in a moment."
                    )
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            self._sleep(delay)
            delay *= 2
        raise TidewiseError(f"{self._name} request failed.")  # pragma: no cover
