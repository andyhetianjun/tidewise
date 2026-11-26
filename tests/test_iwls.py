import json
from datetime import UTC, datetime

import httpx
import pytest

from tidewise.cache import DiskCache, RateLimiter
from tidewise.errors import TidewiseError
from tidewise.iwls import BASE_URL, IwlsSource
from tidewise.models import Station

STATION = Station("00490", "abc123", "Halifax", 44.6, -63.6, "NS", "PERMANENT", True, ("wlp",))
START = datetime(2026, 11, 26, tzinfo=UTC)
END = datetime(2026, 11, 27, tzinfo=UTC)


def make_source(handler, tmp_path, retries=3):
    sleeps: list[float] = []
    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    source = IwlsSource(
        DiskCache(tmp_path),
        client=client,
        limiter=RateLimiter(0, sleep=sleeps.append),
        sleep=sleeps.append,
        retries=retries,
    )
    return source, sleeps


def test_series_parses_rows_and_sends_expected_query(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=[{"eventDate": "2026-11-26T00:00:00Z", "value": 1.5, "qcFlagCode": "2"}]
        )

    source, _ = make_source(handler, tmp_path)
    rows = source.series(STATION, "wlp", START, END, 60)
    assert rows[0].value == 1.5 and rows[0].time == START
    params = dict(seen[0].url.params)
    assert seen[0].url.path.endswith("/stations/abc123/data")
    assert params["time-series-code"] == "wlp"
    assert params["resolution"] == "SIXTY_MINUTES"
    assert params["from"] == "2026-11-26T00:00:00Z"


def test_second_identical_request_is_served_from_cache(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    source, _ = make_source(handler, tmp_path)
    source.series(STATION, "wlp", START, END, 60)
    source.series(STATION, "wlp", START, END, 60)
    assert calls == 1
    source.series(STATION, "wlp", START, END, 15)  # different query, new request
    assert calls == 2


def test_retries_on_429_then_succeeds(tmp_path):
    codes = iter([429, 503, 200])

    def handler(request):
        code = next(codes)
        return httpx.Response(code, json=[], headers={"Retry-After": "3"} if code == 429 else {})

    source, sleeps = make_source(handler, tmp_path)
    assert source.series(STATION, "wlp", START, END, 60) == []
    assert sleeps[0] == 3.0  # honoured Retry-After
    assert sleeps[1] == 6.0  # then backed off


def test_gives_up_with_friendly_error(tmp_path):
    source, _ = make_source(lambda r: httpx.Response(503), tmp_path, retries=2)
    with pytest.raises(TidewiseError, match="busy"):
        source.series(STATION, "wlp", START, END, 60)


def test_client_error_is_not_retried(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    source, _ = make_source(handler, tmp_path)
    with pytest.raises(TidewiseError, match="HTTP 404"):
        source.series(STATION, "wlp", START, END, 60)
    assert calls == 1


def test_stations_are_parsed_and_remembered(tmp_path):
    raw = [
        {
            "code": "00490",
            "id": "abc123",
            "officialName": "Halifax",
            "latitude": 44.6,
            "longitude": -63.6,
            "provinceCode": "NS",
            "type": "PERMANENT",
            "operating": True,
            "timeSeries": [{"code": "wlp"}, {"code": "wlp-hilo"}],
        }
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=json.dumps(raw))

    source, _ = make_source(handler, tmp_path)
    assert source.stations()[0].series == ("wlp", "wlp-hilo")
    source.stations()
    assert calls == 1


def test_rate_limiter_spaces_requests():
    now = [0.0]
    slept: list[float] = []
    limiter = RateLimiter(1.0, clock=lambda: now[0], sleep=lambda s: slept.append(s))
    limiter.wait()
    now[0] = 0.25
    limiter.wait()
    assert slept == [0.75]


def test_cache_expires(tmp_path):
    t = [1000.0]
    cache = DiskCache(tmp_path, clock=lambda: t[0])
    cache.put("k", {"a": 1})
    assert cache.get("k", ttl=60) == {"a": 1}
    t[0] += 61
    assert cache.get("k", ttl=60) is None
