"""The tool logic. Plain methods that return JSON-friendly dicts.

server.py wraps these as MCP tools. Keeping them separate makes them easy to
test without a running server.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import sqrt
from typing import Protocol

from . import config, stations
from .errors import TidewiseError
from .models import Reading, Station
from .timeutil import check_range, iso_local, iso_utc, parse_time

RESOLUTIONS = (15, 60)
MAX_POINTS = 200


class DataSource(Protocol):
    def stations(self) -> list[Station]: ...

    def series(
        self, station: Station, code: str, start: datetime, end: datetime, resolution: int
    ) -> list[Reading]: ...


class TideService:
    def __init__(self, source: DataSource) -> None:
        self._source = source

    # ---- helpers -------------------------------------------------------

    def _station(self, ref: str) -> Station:
        return stations.resolve(self._source.stations(), ref)

    def _range(self, st: Station, start: str, end: str) -> tuple[datetime, datetime]:
        now = config.now()
        s = parse_time(start, st.tz, now=now)
        e = parse_time(end, st.tz, now=now, end=True)
        check_range(s, e)
        return s, e

    @staticmethod
    def _resolution(minutes: int) -> int:
        if minutes not in RESOLUTIONS:
            raise TidewiseError(f"resolution_minutes must be 15 or 60, not {minutes}.")
        return minutes

    @staticmethod
    def _need_series(st: Station, code: str, what: str) -> None:
        if code not in st.series:
            have = ", ".join(st.series) or "none"
            raise TidewiseError(
                f"{st.name} ({st.code}) has no {what}. Time series it does have: {have}. "
                "Use find_stations to look for a nearby station that has it."
            )

    @staticmethod
    def _point(r: Reading, st: Station) -> dict:
        return {
            "time_utc": iso_utc(r.time),
            "time_local": iso_local(r.time, st.tz),
            "height_m": round(r.value, 3),
        }

    def _levels(self, st: Station, code: str, start: str, end: str, res: int, what: str) -> dict:
        res = self._resolution(res)
        s, e = self._range(st, start, end)
        self._need_series(st, code, what)
        rows = self._source.series(st, code, s, e, res)
        if not rows:
            raise TidewiseError(
                f"No {what} came back for {st.name} between {iso_utc(s)} and {iso_utc(e)}. "
                "The station may be offline, or the range may be outside what it reports."
            )
        if len(rows) > MAX_POINTS:
            raise TidewiseError(
                f"That would return {len(rows)} points (the limit is {MAX_POINTS}). "
                "Use a shorter range or resolution_minutes=60."
            )
        hi = max(rows, key=lambda r: r.value)
        lo = min(rows, key=lambda r: r.value)
        return {
            "station": st.to_dict(),
            "series": code,
            "unit": "metres above chart datum",
            "count": len(rows),
            "summary": {
                "max_m": round(hi.value, 3),
                "max_at_local": iso_local(hi.time, st.tz),
                "min_m": round(lo.value, 3),
                "min_at_local": iso_local(lo.time, st.tz),
                "mean_m": round(sum(r.value for r in rows) / len(rows), 3),
            },
            "points": [self._point(r, st) for r in rows],
        }

    # ---- tools ---------------------------------------------------------

    def find_stations(
        self,
        query: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        limit: int = 5,
        max_distance_km: float | None = None,
        include_inactive: bool = False,
    ) -> dict:
        if query is None and latitude is None and longitude is None:
            raise TidewiseError("Give a name in `query`, or both `latitude` and `longitude`.")
        if (latitude is None) != (longitude is None):
            raise TidewiseError("`latitude` and `longitude` must be given together.")
        if latitude is not None and not -90 <= latitude <= 90:
            raise TidewiseError(f"latitude must be between -90 and 90, not {latitude}.")
        if longitude is not None and not -180 <= longitude <= 180:
            raise TidewiseError(f"longitude must be between -180 and 180, not {longitude}.")
        if not 1 <= limit <= 20:
            raise TidewiseError("limit must be between 1 and 20.")
        if max_distance_km is not None and max_distance_km <= 0:
            raise TidewiseError("max_distance_km must be positive.")
        found = stations.search(
            self._source.stations(),
            query=query,
            lat=latitude,
            lon=longitude,
            limit=limit,
            max_distance_km=max_distance_km,
            include_inactive=include_inactive,
        )
        results = []
        for st, dist in found:
            item = st.to_dict()
            if dist is not None:
                item["distance_km"] = round(dist, 1)
            results.append(item)
        if not results:
            raise TidewiseError(
                "No stations found. Try a shorter name, a larger max_distance_km, "
                "or include_inactive=true."
            )
        return {"count": len(results), "stations": results}

    def get_water_levels(
        self, station: str, start: str, end: str, resolution_minutes: int = 60
    ) -> dict:
        st = self._station(station)
        return self._levels(st, "wlo", start, end, resolution_minutes, "observed water levels")

    def get_tide_predictions(
        self, station: str, start: str, end: str, resolution_minutes: int = 60
    ) -> dict:
        st = self._station(station)
        return self._levels(st, "wlp", start, end, resolution_minutes, "tide predictions")

    def next_high_low_tide(self, station: str, after: str = "now", count: int = 4) -> dict:
        if not 1 <= count <= 12:
            raise TidewiseError("count must be between 1 and 12.")
        st = self._station(station)
        self._need_series(st, "wlp-hilo", "high/low tide predictions")
        t0 = parse_time(after, st.tz, now=config.now())
        # Start a bit early so the first event can be labelled high or low
        # by comparing it with the one before it.
        rows = self._source.series(
            st, "wlp-hilo", t0 - timedelta(hours=14), t0 + timedelta(days=5), 0
        )
        if len(rows) < 2:
            raise TidewiseError(f"No tide predictions found for {st.name} after {iso_utc(t0)}.")
        events = []
        for i, r in enumerate(rows):
            other = rows[1] if i == 0 else rows[i - 1]
            events.append((r, "high" if r.value > other.value else "low"))
        upcoming = [(r, k) for r, k in events if r.time >= t0][:count]
        if not upcoming:
            raise TidewiseError(f"No tide predictions found for {st.name} after {iso_utc(t0)}.")
        return {
            "station": st.to_dict(),
            "after_utc": iso_utc(t0),
            "unit": "metres above chart datum",
            "events": [{"type": k, **self._point(r, st)} for r, k in upcoming],
        }

    def compare_observed_vs_predicted(self, station: str, start: str, end: str) -> dict:
        st = self._station(station)
        s, e = self._range(st, start, end)
        self._need_series(st, "wlo", "observed water levels")
        self._need_series(st, "wlp", "tide predictions")
        obs = self._source.series(st, "wlo", s, e, 15)
        pred = {r.time: r.value for r in self._source.series(st, "wlp", s, e, 15)}
        pairs = [(r.time, r.value, pred[r.time]) for r in obs if r.time in pred]
        if not pairs:
            raise TidewiseError(
                f"{st.name} has no overlapping observed and predicted values between "
                f"{iso_utc(s)} and {iso_utc(e)}."
            )
        diffs = [(t, o - p) for t, o, p in pairs]
        worst = max(diffs, key=lambda d: abs(d[1]))
        mean = sum(d for _, d in diffs) / len(diffs)
        rmse = sqrt(sum(d * d for _, d in diffs) / len(diffs))
        return {
            "station": st.to_dict(),
            "unit": "metres",
            "count": len(diffs),
            "mean_difference_m": round(mean, 3),
            "rmse_m": round(rmse, 3),
            "largest_difference_m": round(worst[1], 3),
            "largest_difference_at_local": iso_local(worst[0], st.tz),
            "note": (
                "difference = observed minus predicted. Positive means the water was "
                "higher than predicted (weather and river flow both matter)."
            ),
        }
