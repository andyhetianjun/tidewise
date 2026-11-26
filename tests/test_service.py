import pytest

from tidewise.errors import TidewiseError
from tidewise.models import Station
from tidewise.service import TideService


def test_find_by_name(service):
    out = service.find_stations(query="halifax")
    assert out["stations"][0]["code"] == "00490"
    assert out["stations"][0]["time_zone"] == "America/Halifax"


def test_find_saint_john_spellings(service):
    for q in ("Saint John", "St. John", "saint john, NB"):
        assert service.find_stations(query=q)["stations"][0]["code"] == "00065"


def test_find_near_point_sorted_by_distance(service):
    out = service.find_stations(latitude=44.6, longitude=-63.6, limit=3)
    distances = [s["distance_km"] for s in out["stations"]]
    assert out["stations"][0]["name"] == "Halifax"
    assert distances == sorted(distances)


def test_find_max_distance(service):
    out = service.find_stations(latitude=44.6, longitude=-63.6, max_distance_km=20)
    assert all(s["distance_km"] <= 20 for s in out["stations"])


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "query"),
        ({"latitude": 44.0}, "together"),
        ({"latitude": 95.0, "longitude": 0.0}, "between -90 and 90"),
        ({"latitude": 0.0, "longitude": 200.0}, "between -180 and 180"),
        ({"query": "x", "limit": 0}, "limit"),
        ({"query": "Atlantis"}, "No stations found"),
    ],
)
def test_find_stations_errors(service, kwargs, message):
    with pytest.raises(TidewiseError, match=message):
        service.find_stations(**kwargs)


def test_next_high_low_labels_alternate(service):
    out = service.next_high_low_tide("Halifax", after="2026-11-26", count=6)
    kinds = [e["type"] for e in out["events"]]
    assert kinds == ["low", "high", "low", "high", "low", "high"]
    high = next(e for e in out["events"] if e["type"] == "high")
    assert high["time_local"] == "2026-11-26 08:36 AST"
    assert high["height_m"] == pytest.approx(1.977)


def test_next_high_low_defaults_to_now(service):
    out = service.next_high_low_tide("00490")
    assert out["after_utc"] == "2026-11-25T12:00:00Z"
    assert len(out["events"]) == 4
    assert out["events"][0]["time_utc"] >= "2026-11-25T12:00:00Z"


def test_predictions_summary(service):
    out = service.get_tide_predictions("Saint John", "2026-11-26", "2026-11-26")
    assert out["series"] == "wlp"
    assert out["summary"]["max_m"] == pytest.approx(8.426)
    assert out["count"] == 25  # hourly, inclusive of both midnights


def test_resolution_15_minutes(service):
    out = service.get_tide_predictions(
        "Saint John", "2026-11-26T00:00", "2026-11-26T06:00", resolution_minutes=15
    )
    assert out["count"] == 25


def test_observed_levels(service):
    out = service.get_water_levels("Vancouver", "2026-11-24", "2026-11-24")
    assert out["series"] == "wlo"
    assert out["summary"]["max_m"] == pytest.approx(5.167)


def test_observed_gap_gives_helpful_error(service):
    with pytest.raises(TidewiseError, match="station may be offline"):
        service.get_water_levels("Halifax", "2026-11-24", "2026-11-24")


def test_too_many_points(service):
    with pytest.raises(TidewiseError, match="limit is 200"):
        service.get_tide_predictions("Halifax", "2026-11-24", "2026-11-28", resolution_minutes=15)


@pytest.mark.parametrize(
    "call, message",
    [
        (
            lambda s: s.get_tide_predictions("Halifax", "2026-11-26", "2026-11-25"),
            "after the start",
        ),
        (lambda s: s.get_tide_predictions("Halifax", "2026-11-20", "2026-12-20"), "at most 7 days"),
        (lambda s: s.get_tide_predictions("Halifax", "tomorrow", "2026-11-26"), "ISO 8601"),
        (lambda s: s.get_tide_predictions("Halifax", "2026-11-26", "2026-11-26", 30), "15 or 60"),
        (
            lambda s: s.get_tide_predictions("Nowhereville", "2026-11-26", "2026-11-26"),
            "No station matches",
        ),
        (
            lambda s: s.get_tide_predictions("Halifax", "2027-03-01", "2027-03-02"),
            "Offline mode only has",
        ),
        (lambda s: s.next_high_low_tide("Halifax", count=0), "count"),
    ],
)
def test_bad_inputs(service, call, message):
    with pytest.raises(TidewiseError, match=message):
        call(service)


def test_station_without_observed_series():
    class Stub:
        def stations(self):
            return [
                Station("1", "id", "Predictionville", 1.0, 1.0, "NS", "PERMANENT", True, ("wlp",))
            ]

        def series(self, *args):
            raise AssertionError("should not be called")

    with pytest.raises(TidewiseError, match="has no observed water levels"):
        TideService(Stub()).get_water_levels("Predictionville", "2026-11-24", "2026-11-25")


def test_compare(service):
    out = service.compare_observed_vs_predicted("Saint John", "2026-11-24", "2026-11-24")
    assert out["count"] == 97
    assert out["rmse_m"] == pytest.approx(0.079, abs=0.001)
    assert abs(out["mean_difference_m"]) < 0.05


def test_compare_needs_observed(service):
    with pytest.raises(TidewiseError, match="No observed|no observed|offline|overlapping"):
        service.compare_observed_vs_predicted("Halifax", "2026-11-24", "2026-11-24")
