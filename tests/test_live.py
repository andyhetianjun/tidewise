import asyncio
from datetime import UTC, datetime

from tidewise import live
from tidewise.cli import main
from tidewise.errors import TidewiseError
from tidewise.fixtures import FixtureSource
from tidewise.service import TideService
from tidewise.site import build_site
from tidewise.weather import WeatherFixtureSource, WeatherService

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class DownSource:
    def stations(self):
        raise TidewiseError("Could not reach the IWLS service (ConnectError).")


def test_working_source_is_labelled_live():
    tide = TideService(FixtureSource())
    wx = WeatherService(WeatherFixtureSource(), tide._station)
    data = live.collect(NOW, ["00490"], live_tide=tide, live_weather=wx)
    st = data["stations"][0]
    assert st["name"] == "Halifax" and st["tides"]["source"] == "live"
    assert [e["type"] for e in st["tides"]["data"]["events"]][:2] == ["low", "high"]
    assert data["overall"] == "live"


def test_down_source_falls_back_to_sample_and_says_so():
    tide = TideService(DownSource())
    wx = WeatherService(WeatherFixtureSource(), tide._station)
    data = live.collect(NOW, ["00490"], live_tide=tide, live_weather=wx)
    st = data["stations"][0]
    assert st["tides"]["source"] == "sample" and "Could not reach" in st["tides"]["reason"]
    assert st["tides"]["data"]["events"]
    assert st["name"] == "Halifax"
    html = live.render_live(data, NOW)
    assert "bundled sample" in html and "Last updated 2026-11-25 12:00 UTC" in html


async def test_site_hides_recorded_section_without_recordings(tmp_path):
    await build_site(tmp_path, jobs=8, recordings=tmp_path / "none")
    assert not (tmp_path / "recorded.html").exists()
    assert "Recorded with" not in (tmp_path / "index.html").read_text()


def test_site_shows_recorded_section_with_recordings(tmp_path, capsys):
    root = tmp_path / "rec"
    case = "trap-waves"
    main(["record", "--root", str(root), "prompt", case])
    main(["record", "--root", str(root), "answer", case, "I do not have wave data."])
    capsys.readouterr()
    out = tmp_path / "site"
    asyncio.run(build_site(out, jobs=8, recordings=root / "claude-sonnet-5-5"))
    page = (out / "recorded.html").read_text()
    assert "not a live call" in page and "Checker verdict" in page and case in page
    assert "recorded.html" in (out / "index.html").read_text()
