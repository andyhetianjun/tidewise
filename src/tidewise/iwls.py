"""Live data source: the DFO Integrated Water Level System (IWLS) public API."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from .cache import DiskCache, RateLimiter
from .httpcache import CachedClient
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
        http = client or httpx.Client(
            base_url=BASE_URL, timeout=30, headers={"User-Agent": USER_AGENT}
        )
        self._http = CachedClient(
            "IWLS", http, cache, limiter=limiter, sleep=sleep, retries=retries
        )
        self._stations: list[Station] | None = None

    def stations(self) -> list[Station]:
        if self._stations is None:
            raw = self._http.get("/stations", {}, TTL_STATIONS)
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
        rows = self._http.get(f"/stations/{station.id}/data", params, ttl)
        return [
            Reading(
                datetime.fromisoformat(r["eventDate"]).astimezone(UTC),
                float(r["value"]),
            )
            for r in rows
        ]
