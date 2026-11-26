"""Live data source: the DFO Integrated Water Level System (IWLS) public API."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from .cache import DiskCache, RateLimiter
from .errors import TidewiseError
from .models import Reading, Station
from .timeutil import api_time

BASE_URL = "https://api-iwls.dfo-mpo.gc.ca/api/v1"
USER_AGENT = "tidewise/0.1 (learning project; polite client, 1 request/second)"

RESOLUTION = {15: "FIFTEEN_MINUTES", 60: "SIXTY_MINUTES"}

# How long cached answers stay fresh, in seconds.
TTL_STATIONS = 24 * 3600
TTL_PREDICTED = 24 * 3600
TTL_OBSERVED = 10 * 60


class IwlsSource:
    def __init__(
        self,
        cache: DiskCache,
        *,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 3,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=BASE_URL, timeout=30, headers={"User-Agent": USER_AGENT}
        )
        self._cache = cache
        self._limiter = limiter or RateLimiter(1.0)
        self._sleep = sleep
        self._retries = retries
        self._stations: list[Station] | None = None

    def _get(self, path: str, params: dict[str, str], ttl: float) -> list[dict]:
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
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
                        f"Could not reach the IWLS service ({type(exc).__name__}). "
                        "Try again in a moment."
                    ) from exc
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    self._cache.put(key, data)
                    return data
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise TidewiseError(
                        f"IWLS returned HTTP {resp.status_code} for {path}. "
                        "Check the station and the time range."
                    )
                if attempt == self._retries - 1:
                    raise TidewiseError(
                        f"IWLS is busy (HTTP {resp.status_code}). Try again in a moment."
                    )
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            self._sleep(delay)
            delay *= 2
        raise TidewiseError("IWLS request failed.")  # pragma: no cover

    def stations(self) -> list[Station]:
        if self._stations is None:
            raw = self._get("/stations", {}, TTL_STATIONS)
            self._stations = [Station.from_iwls(r) for r in raw]
        return self._stations

    def series(
        self, station: Station, code: str, start: datetime, end: datetime, resolution: int
    ) -> list[Reading]:
        params = {
            "time-series-code": code,
            "from": api_time(start),
            "to": api_time(end),
        }
        if code != "wlp-hilo":
            params["resolution"] = RESOLUTION[resolution]
        ttl = TTL_OBSERVED if code == "wlo" else TTL_PREDICTED
        rows = self._get(f"/stations/{station.id}/data", params, ttl)
        return [
            Reading(
                datetime.fromisoformat(r["eventDate"]).astimezone(UTC),
                float(r["value"]),
            )
            for r in rows
        ]
