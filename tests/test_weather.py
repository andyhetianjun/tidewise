import json

import httpx
import pytest

from tidewise.cache import DiskCache, RateLimiter
from tidewise.errors import TidewiseError
from tidewise.httpcache import CachedClient
from tidewise.weather import (
    EcccSource,
    WeatherFixtureSource,
    WeatherService,
    _inside,
    _km_to_ring,
    pick_area,
)


@pytest.fixture
def weather(service) -> WeatherService:
    return WeatherService(WeatherFixtureSource(), service._station)


def test_marine_forecast_inside_an_area(weather):
    d = weather.get_marine_forecast("Halifax")
    assert d["marine_area"] == "Halifax Harbour and Approaches"
    assert d["area_match"] == "station is inside this area"
    assert "light" in d["forecast"][0]["wind"]
    assert d["warnings"] == []


def test_marine_forecast_reports_warnings_and_nearest_area(weather):
    d = weather.get_marine_forecast("Vancouver")
    assert d["marine_area"] == "Strait of Georgia"
    assert d["area_match"].startswith("nearest area")
    assert {w["warning"] for w in d["warnings"]} >= {"Storm warning", "Gale warning"}


def test_hourly_weather_window_and_summary(weather):
    d = weather.get_hourly_weather("Halifax", "2026-11-26T06:00", "2026-11-26T11:00")
    assert d["summary"]["hours"] == 6
    assert d["summary"]["max_wind_kmh"] == 5
    assert d["hours"][0]["time_local"].startswith("2026-11-26 06:00")
    assert d["summary"]["sunrise_local"].startswith("2026-11-26 07:25")


def test_hourly_weather_outside_the_forecast_says_what_it_covers(weather):
    with pytest.raises(TidewiseError, match="only covers 2026-11-25 22:00 AST to 2026-11-26 21:00"):
        weather.get_hourly_weather("Halifax", "2026-12-05", "2026-12-05")


def test_station_without_sample_weather(service):
    from tidewise.stations import usable

    other = next(
        s
        for s in service._source.stations()
        if usable(s) and s.code not in {"00490", "07735", "00065", "01700", "08615"}
    )
    weather = WeatherService(WeatherFixtureSource(), service._station)
    with pytest.raises(TidewiseError, match="no weather for station"):
        weather.get_marine_forecast(other.code)


def test_polygon_helpers():
    square = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    assert _inside(5, 5, square)
    assert not _inside(15, 5, square)
    assert _km_to_ring(10, 5, square) == pytest.approx(0, abs=1e-6)
    # 1 degree east of the edge at the equator is about 111 km
    assert _km_to_ring(11, 5, square) == pytest.approx(111.3 * 0.996, rel=0.02)


def test_no_area_within_range_is_a_clear_error(service):
    st = service._station("Halifax")
    far = {
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "properties": {},
    }
    assert pick_area([far], st) is None


def test_live_source_sends_a_bbox_and_trims(service, tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        feature = {
            "id": "m1",
            "properties": {
                "area": {"value": {"en": "X", "fr": "X"}},
                "regularForecast": {"locations": []},
                "warnings": {"locations": []},
                "junk": 1,
            },
            "geometry": {"type": "Polygon", "coordinates": [[[0.123456, 1], [1, 1], [1, 2]]]},
        }
        return httpx.Response(200, json={"features": [feature]})

    client = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(handler))
    http = CachedClient("MSC GeoMet", client, DiskCache(tmp_path), limiter=RateLimiter(0))
    source = EcccSource(DiskCache(tmp_path), client=http)
    areas = source.marine_areas(service._station("Halifax"))
    assert "bbox" in seen[0] and seen[0]["limit"] == "50"
    assert "junk" not in areas[0]["properties"]
    assert areas[0]["geometry"]["coordinates"][0][0][0] == 0.123
    source.marine_areas(service._station("Halifax"))
    assert len(seen) == 1  # second call came from the cache


def test_sample_files_are_small_and_valid():
    from tidewise.config import DATA_DIR

    files = list((DATA_DIR / "weather").glob("*.json"))
    assert len(files) == 5
    assert sum(f.stat().st_size for f in files) < 300_000
    for f in files:
        assert set(json.loads(f.read_text())) == {"marine_areas", "city_points"}
