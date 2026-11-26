"""Tests for the grounding checker.

LABELLED holds hand-labelled answers: True means every claim in the answer is
supported by the tool outputs below, False means at least one claim is made up.
I wrote the labels by reading the outputs, before looking at what the checker said
about them, but they were written by the same person who wrote the checker.
"""

import json

import pytest

from tidewise.grounding import abstains, check_grounding
from tidewise.service import TideService
from tidewise.weather import WeatherService


@pytest.fixture
def outputs(service: TideService) -> list[str]:
    from tidewise.weather import WeatherFixtureSource

    weather = WeatherService(WeatherFixtureSource(), service._station)
    tides = service.next_high_low_tide("Halifax", "2026-11-26", 6)
    wind = weather.get_hourly_weather("Halifax", "2026-11-26T06:00", "2026-11-26T11:00")
    marine = weather.get_marine_forecast("Vancouver")
    compare = service.compare_observed_vs_predicted("Saint John", "2026-11-24", "2026-11-24")
    return [json.dumps(x, indent=2) for x in (tides, wind, marine, compare)] + [
        "Error executing tool next_high_low_tide: Offline mode only has wlp-hilo data from "
        "2026-11-24 00:00Z to 2026-11-29 00:00Z for Halifax. Ask about a time inside that window."
    ]


LABELLED = [
    # (answer, grounded?)
    ("High tide at Halifax is at 08:36 AST (1.977 m).", True),
    ("High tide at Halifax is at 8:36 AM, about 1.98 m.", True),
    ("The evening high tide is at 9:25 PM.", True),
    ("The evening high tide is at 21:25 local time.", True),
    ("The high tide is at 10:15.", False),
    ("High tide in Halifax is at 08:36 and 2.4 m.", False),
    ("Low tide at 02:47 (0.35 m).", True),
    ("Low tide at 02:47 (0.77 m).", False),
    ("Wind up to 5 km/h, about 3 knots.", True),
    ("Wind up to 12 km/h.", False),
    ("Winds stay under 3 knots all morning near Halifax.", True),
    ("Gusts could reach 28 knots.", False),
    ("The temperature is between -3 and 4 °C.", True),
    ("It will be 12 °C.", False),
    ("Storm warning in effect for the Strait of Georgia.", True),
    ("Wind southeast 40 knots after midnight.", True),
    ("Wind southeast 27 knots.", False),
    ("Wind of 50 knots is about 93 km/h.", True),
    ("Winds of 100 km/h.", False),
    ("The tide rises from 0.353 m to 1.977 m, a range of 1.62 m.", True),
    ("The tide rises from 0.353 m to 1.977 m, a range of 2.5 m.", False),
    ("At Saint John, observed water was 0.014 m below the prediction.", True),
    ("At Saint John, observed water was 0.014 m above the prediction.", False),
    ("The largest gap was -0.181 m at 2026-11-24 20:15.", True),
    ("The station is 00490 (Halifax).", True),
    ("The station is 00999 (Halifax).", False),
    ("I used the Lunenburg station.", False),
    ("I used the Halifax station.", True),
    ("Tides on 2026-11-26: low at 02:47.", True),
    ("Tides on 2026-11-30: low at 02:47.", False),
    ("I don't have data for that date. The data runs from 2026-11-24 to 2026-11-29.", True),
    ("Sunrise is at 07:25 AST.", True),
    ("Sunrise is at 06:10 AST.", False),
    ("Why is the sky blue? Because of how light scatters.", True),
    ("The weather is light and calm.", True),  # no number to check: the checker cannot object
]


@pytest.mark.parametrize("answer,expected", LABELLED)
def test_labelled_answers(outputs, answer, expected):
    report = check_grounding(answer, outputs, "Is it a good time at Halifax?")
    assert report.grounded is expected, [c.to_dict() for c in report.unsupported]


def test_labelled_accuracy_is_100_percent(outputs):
    hits = sum(check_grounding(a, outputs, "q").grounded is expected for a, expected in LABELLED)
    assert hits == len(LABELLED)


def test_utc_and_local_are_kept_apart(outputs):
    # 08:36 local is 12:36 UTC; saying 08:36 UTC is wrong.
    assert check_grounding("High tide at 12:36 UTC.", outputs).grounded
    assert not check_grounding("High tide at 08:36 UTC.", outputs).grounded


def test_a_value_from_the_question_is_not_flagged():
    report = check_grounding("At 14:30 tomorrow I cannot say.", [], "What is the tide at 14:30?")
    assert report.grounded


def test_a_place_only_in_the_question_is_not_a_claim_to_flag_alone():
    report = check_grounding(
        "I could not find a station called Atlantis.", ["No stations"], "Atlantis?"
    )
    assert report.grounded


def test_made_up_numbers_for_a_missing_station_are_caught():
    report = check_grounding(
        "At Atlantis, high tide is at 10:15 (2.1 m).", ["Error: No stations found."], "Atlantis?"
    )
    assert {c.kind for c in report.unsupported} == {"time", "quantity"}


def test_unit_conversion_and_rounding():
    out = ['{"height_m": 1.977, "wind_kmh": 40, "wind_knots": 22}']
    assert check_grounding("1.98 m", out).grounded
    assert check_grounding("198 cm", out).grounded
    assert check_grounding("about 6.5 ft", out).grounded
    assert not check_grounding("2.5 m", out).grounded
    assert check_grounding("22 knots", out).grounded
    assert check_grounding("40 km/h", out).grounded


def test_abstains():
    assert abstains("I could not get that: no data.")
    assert abstains("I don't know that.")
    assert not abstains("High tide is at 08:36.")
