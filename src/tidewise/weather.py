"""Weather next to the tides: marine forecasts and hourly forecasts from Environment
and Climate Change Canada (the MSC GeoMet API, no key needed).

Two data sets are used:
- marine forecasts: one text forecast per marine area (a polygon on the water),
  with warnings. Wind is in knots.
- city forecasts: hourly temperature, wind and rain chance at a named place.
  Wind is in km/h.

A station is matched to the marine area that contains it, or the nearest one
within a few kilometres, and to the nearest city forecast point.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import cos, radians
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import config
from .cache import DiskCache
from .errors import TidewiseError
from .geo import haversine_km
from .httpcache import CachedClient
from .models import Station
from .timeutil import check_range, iso_local, iso_utc, parse_time

BASE_URL = "https://api.weather.gc.ca"
USER_AGENT = "tidewise/0.1 (learning project; polite client)"
TTL_FORECAST = 30 * 60
MAX_AREA_KM = 25.0
MAX_CITY_KM = 30.0
KMH_PER_KNOT = 1.852


class WeatherSource(Protocol):
    def marine_areas(self, station: Station) -> list[dict]: ...

    def city_points(self, station: Station) -> list[dict]: ...


# ---- raw data -----------------------------------------------------------


def _strip_fr(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_fr(v) for k, v in value.items() if k != "fr"}
    if isinstance(value, list):
        return [_strip_fr(v) for v in value]
    return value


def _round_coords(value: Any) -> Any:
    if isinstance(value, list):
        if value and isinstance(value[0], (int, float)):
            return [round(v, 3) for v in value]
        return [_round_coords(v) for v in value]
    return value


def trim(kind: str, feature: dict) -> dict:
    """Keep only what the tools read, so the committed sample stays small."""
    props = _strip_fr(feature["properties"])
    if kind == "marine_areas":
        keep = ["area", "lastUpdated", "regularForecast", "warnings"]
    else:
        keep = ["identifier", "name", "region", "lastUpdated", "hourlyForecastGroup", "riseSet"]
    props = {k: props[k] for k in keep if k in props}
    if "riseSet" in props:
        props["riseSet"].pop("disclaimer", None)
    for hour in props.get("hourlyForecastGroup", {}).get("hourlyForecasts", []):
        hour.pop("iconCode", None)
        hour.pop("windChill", None)
    geometry = dict(feature["geometry"])
    geometry["coordinates"] = _round_coords(geometry["coordinates"])
    return {"id": feature["id"], "properties": props, "geometry": geometry}


class EcccSource:
    """Live data from api.weather.gc.ca."""

    def __init__(self, cache: DiskCache, *, client: CachedClient | None = None) -> None:
        self._http = client or CachedClient(
            "MSC GeoMet",
            httpx.Client(base_url=BASE_URL, timeout=30, headers={"User-Agent": USER_AGENT}),
            cache,
        )

    def _features(self, collection: str, st: Station) -> list[dict]:
        box = (st.longitude - 0.5, st.latitude - 0.5, st.longitude + 0.5, st.latitude + 0.5)
        data = self._http.get(
            f"/collections/{collection}/items",
            {"f": "json", "bbox": ",".join(f"{v:.4f}" for v in box), "limit": "50"},
            TTL_FORECAST,
        )
        return [
            trim("marine_areas" if "marine" in collection else "city_points", f)
            for f in data["features"]
        ]

    def marine_areas(self, station: Station) -> list[dict]:
        return self._features("marineweather-realtime", station)

    def city_points(self, station: Station) -> list[dict]:
        return self._features("citypageweather-realtime", station)


class WeatherFixtureSource:
    """Offline: the sample saved by scripts/fetch_weather_samples.py."""

    def __init__(self, directory: Path = config.DATA_DIR / "weather") -> None:
        self._dir = directory

    def _load(self, st: Station) -> dict:
        path = self._dir / f"{st.code}.json"
        if not path.exists():
            raise TidewiseError(
                f"The offline sample has no weather for station {st.code} ({st.name}). "
                "Offline mode only includes weather for the five sample stations."
            )
        return json.loads(path.read_text())

    def marine_areas(self, station: Station) -> list[dict]:
        return self._load(station)["marine_areas"]

    def city_points(self, station: Station) -> list[dict]:
        return self._load(station)["city_points"]


# ---- geometry -----------------------------------------------------------


def _rings(geometry: dict) -> list[list[list[float]]]:
    coords = geometry["coordinates"]
    polygons = [coords] if geometry["type"] == "Polygon" else coords
    return [poly[0] for poly in polygons]


def _inside(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _km_to_ring(lon: float, lat: float, ring: list[list[float]]) -> float:
    """Distance from a point to the edge of a polygon, on a flat local map."""
    kx = 111.32 * cos(radians(lat))
    ky = 110.57
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        ax, ay = (x1 - lon) * kx, (y1 - lat) * ky
        bx, by = (x2 - lon) * kx, (y2 - lat) * ky
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
        best = min(best, ((ax + t * dx) ** 2 + (ay + t * dy) ** 2) ** 0.5)
    return best


def pick_area(areas: list[dict], st: Station) -> tuple[dict, float] | None:
    """The marine area containing the station (distance 0), else the nearest one
    within MAX_AREA_KM."""
    best: tuple[dict, float] | None = None
    for area in areas:
        if not area.get("geometry"):
            continue
        rings = _rings(area["geometry"])
        if any(_inside(st.longitude, st.latitude, r) for r in rings):
            dist = 0.0
        else:
            dist = min(_km_to_ring(st.longitude, st.latitude, r) for r in rings)
        if best is None or dist < best[1]:
            best = (area, dist)
    if best is None or best[1] > MAX_AREA_KM:
        return None
    return best


def pick_city(points: list[dict], st: Station) -> tuple[dict, float] | None:
    best: tuple[dict, float] | None = None
    for point in points:
        lon, lat = point["geometry"]["coordinates"]
        dist = haversine_km(st.latitude, st.longitude, lat, lon)
        if best is None or dist < best[1]:
            best = (point, dist)
    if best is None or best[1] > MAX_CITY_KM:
        return None
    return best


# ---- the tools ----------------------------------------------------------


def _en(value: Any) -> Any:
    return value.get("en") if isinstance(value, dict) else value


def _number(value: Any) -> float:
    """Wind speed can be a number or a word like 'calm'."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class WeatherService:
    def __init__(
        self,
        source: WeatherSource,
        resolve: Callable[[str], Station],
        now: Callable[[], datetime] = config.now,
    ) -> None:
        self._source = source
        self._resolve = resolve
        self._now = now

    def get_marine_forecast(self, station: str) -> dict:
        st = self._resolve(station)
        found = pick_area(self._source.marine_areas(st), st)
        if found is None:
            raise TidewiseError(
                f"No Environment Canada marine forecast area covers {st.name} "
                f"(nothing within {MAX_AREA_KM:.0f} km). Marine forecasts only exist for "
                "coastal waters, the Great Lakes and a few large lakes."
            )
        area, dist = found
        props = area["properties"]
        info = props["area"]
        forecast = props.get("regularForecast", {})
        periods, notes = [], []
        for loc in forecast.get("locations", []):
            if "statusStatement" in loc:
                notes.append(_en(loc["statusStatement"]))
                continue
            cond = loc.get("weatherCondition", {})
            item: dict[str, Any] = {}
            if "name" in loc:
                item["part_of_area"] = _en(loc["name"])
            item["period"] = _en(cond.get("periodOfCoverage"))
            for key, text in cond.items():
                if key != "periodOfCoverage":
                    item[key] = _en(text)
            periods.append(item)
        warnings = [
            {
                "area": _en(loc.get("name")),
                "warning": _en(event.get("name")),
                "status": _en(event.get("status")),
            }
            for loc in props.get("warnings", {}).get("locations", [])
            for event in loc.get("events", [])
        ]
        if not periods:
            raise TidewiseError(
                f"The marine forecast for {_en(info['value'])} has no current forecast"
                + (f" ({notes[0]})" if notes else "")
                + ". Try get_hourly_weather for a nearby town instead."
            )
        issued = forecast.get("issuedDatetimeUTC")
        return {
            "station": st.to_dict(),
            "marine_area": _en(info["value"]),
            "region": _en(info.get("region")),
            "area_match": "station is inside this area"
            if dist == 0
            else f"nearest area, {dist:.0f} km from the station",
            "issued_utc": issued,
            "issued_local": iso_local(datetime.fromisoformat(issued), st.tz) if issued else None,
            "wind_unit": "knots (1 knot = 1.852 km/h)",
            "forecast": periods,
            "warnings": warnings,
            "source": "Environment and Climate Change Canada",
        }

    def get_hourly_weather(self, station: str, start: str = "now", end: str | None = None) -> dict:
        st = self._resolve(station)
        found = pick_city(self._source.city_points(st), st)
        if found is None:
            raise TidewiseError(
                f"No Environment Canada city forecast point within {MAX_CITY_KM:.0f} km of "
                f"{st.name}. Try get_marine_forecast."
            )
        point, dist = found
        props = point["properties"]
        hours = props["hourlyForecastGroup"]["hourlyForecasts"]
        if not hours:
            raise TidewiseError(f"No hourly forecast is published for {_en(props['name'])} now.")
        times = [datetime.fromisoformat(h["timestamp"]).astimezone(UTC) for h in hours]
        first, last = times[0], times[-1]
        now = self._now()
        s = parse_time(start, st.tz, now=now)
        e = parse_time(end, st.tz, now=now, end=True) if end else s + timedelta(hours=24)
        check_range(s, e)
        chosen = [(t, h) for t, h in zip(times, hours, strict=True) if s <= t <= e]
        if not chosen:
            raise TidewiseError(
                f"The hourly forecast for {_en(props['name'])} only covers "
                f"{iso_local(first, st.tz)} to {iso_local(last, st.tz)}. "
                "Ask about a time inside that window."
            )
        points = []
        for t, h in chosen:
            wind = h.get("wind", {})
            kmh = _number(_en(wind.get("speed", {}).get("value")))
            gust = _en(wind.get("gust", {}).get("value")) if wind.get("gust") else None
            points.append(
                {
                    "time_utc": iso_utc(t),
                    "time_local": iso_local(t, st.tz),
                    "condition": _en(h.get("condition")),
                    "temperature_c": _en(h.get("temperature", {}).get("value")),
                    "wind_kmh": round(kmh),
                    "wind_knots": round(kmh / KMH_PER_KNOT),
                    "wind_gust_kmh": round(_number(gust)) if gust is not None else None,
                    "wind_direction": _en(wind.get("direction", {}).get("windDirFull")),
                    "chance_of_precipitation_pct": _en(h.get("lop", {}).get("value")),
                }
            )
        winds = [p["wind_kmh"] for p in points]
        temps = [_number(p["temperature_c"]) for p in points]
        rise = props.get("riseSet", {})
        return {
            "station": st.to_dict(),
            "forecast_point": _en(props["name"]),
            "forecast_point_distance_km": round(dist, 1),
            "issued_utc": props.get("lastUpdated"),
            "covers": {"from_local": iso_local(first, st.tz), "to_local": iso_local(last, st.tz)},
            "summary": {
                "hours": len(points),
                "max_wind_kmh": max(winds),
                "max_wind_knots": round(max(winds) / KMH_PER_KNOT),
                "min_temperature_c": min(temps),
                "max_temperature_c": max(temps),
                "max_chance_of_precipitation_pct": max(
                    _number(p["chance_of_precipitation_pct"]) for p in points
                ),
                "sunrise_local": _local(rise.get("sunrise"), st),
                "sunset_local": _local(rise.get("sunset"), st),
            },
            "hours": points,
            "source": "Environment and Climate Change Canada",
        }


def _local(value: Any, st: Station) -> str | None:
    text = _en(value)
    return iso_local(datetime.fromisoformat(text), st.tz) if text else None
