"""The MCP server. Each tool is a thin wrapper over TideService."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config
from .cache import DiskCache
from .errors import TidewiseError
from .fixtures import FixtureSource
from .iwls import IwlsSource
from .service import DataSource, TideService

INSTRUCTIONS = (
    "Tide and water level data from Fisheries and Oceans Canada (IWLS). "
    "Start with find_stations unless you already know a station code. "
    "Heights are metres above chart datum. Times are returned in UTC and in the "
    "station's local time. A date with no time, like 2026-11-26, means that whole "
    "local day."
)


def default_source() -> DataSource:
    if config.is_offline():
        return FixtureSource()
    return IwlsSource(DiskCache(config.cache_dir()))


def create_server(source: DataSource | None = None) -> MCPServer:
    service = TideService(source or default_source())
    mcp = MCPServer("tidewise", instructions=INSTRUCTIONS, log_level="WARNING")

    def run(fn, **kwargs):  # noqa: ANN001, ANN003, ANN202
        try:
            return fn(**kwargs)
        except TidewiseError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def find_stations(
        query: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        limit: int = 5,
        max_distance_km: float | None = None,
        include_inactive: bool = False,
    ) -> dict:
        """Find tide gauge stations by place name, or the nearest ones to a point.

        Give `query` (for example "Halifax" or "Saint John"), or both `latitude`
        and `longitude`. With coordinates, results are sorted by distance. Each
        result has the station `code` to use in the other tools, its time zone,
        and which data series it has: wlo (observed water level), wlp (predicted
        water level), wlp-hilo (predicted high and low tides).

        Args:
            query: Part of a station name. Case does not matter.
            latitude: Degrees north, -90 to 90. Needs longitude.
            longitude: Degrees east, -180 to 180 (west is negative). Needs latitude.
            limit: How many stations to return, 1 to 20.
            max_distance_km: Drop stations farther than this from the point.
            include_inactive: Also list stations that are not operating.
        """
        return run(
            service.find_stations,
            query=query,
            latitude=latitude,
            longitude=longitude,
            limit=limit,
            max_distance_km=max_distance_km,
            include_inactive=include_inactive,
        )

    @mcp.tool()
    def get_water_levels(station: str, start: str, end: str, resolution_minutes: int = 60) -> dict:
        """Get observed (measured) water levels for a station over a time range.

        Returns a summary (highest, lowest, mean) and the points. Some stations
        do not report observed levels, and a station can go quiet for a while;
        the error says so. For the future, use get_tide_predictions instead.

        Args:
            station: Station code like "07735" or a name like "Vancouver".
            start: Start time. "2026-11-24" (whole local day), "2026-11-24T08:00"
                (station local time), "2026-11-24T16:00:00Z" (UTC) or "now".
            end: End time, same formats. A date alone means the end of that day.
                At most 7 days after start.
            resolution_minutes: 15 or 60. At most 200 points are returned.
        """
        return run(
            service.get_water_levels,
            station=station,
            start=start,
            end=end,
            resolution_minutes=resolution_minutes,
        )

    @mcp.tool()
    def get_tide_predictions(
        station: str, start: str, end: str, resolution_minutes: int = 60
    ) -> dict:
        """Get predicted water levels (the tide table curve) for a station.

        Same inputs and output shape as get_water_levels. Predictions come from
        harmonic analysis and do not include weather. To find just the next high
        and low tides, next_high_low_tide is shorter.

        Args:
            station: Station code like "00490" or a name like "Halifax".
            start: Start time. "2026-11-26" (whole local day), "2026-11-26T08:00"
                (station local time), "2026-11-26T12:00:00Z" (UTC) or "now".
            end: End time, same formats. A date alone means the end of that day.
                At most 7 days after start.
            resolution_minutes: 15 or 60. At most 200 points are returned.
        """
        return run(
            service.get_tide_predictions,
            station=station,
            start=start,
            end=end,
            resolution_minutes=resolution_minutes,
        )

    @mcp.tool()
    def next_high_low_tide(station: str, after: str = "now", count: int = 4) -> dict:
        """List the next predicted high and low tides at a station.

        Each event says whether it is a high or a low, the time (UTC and local)
        and the height. To answer "when is high tide tomorrow", pass
        after="<tomorrow's date>" and read the events that fall on that day;
        ask for enough events (count=6 or more) to cover the whole day.

        Args:
            station: Station code like "00490" or a name like "Halifax".
            after: Where to start looking: "now", a date, or a date and time.
            count: How many events to return, 1 to 12.
        """
        return run(service.next_high_low_tide, station=station, after=after, count=count)

    @mcp.tool()
    def compare_observed_vs_predicted(station: str, start: str, end: str) -> dict:
        """Compare measured water levels with the predicted tide.

        Returns the average difference, root-mean-square difference and the
        largest gap (observed minus predicted). Large gaps usually come from
        storm surge, wind or river flow. Needs a station with both observed
        and predicted series, and a time range in the past.

        Args:
            station: Station code or name.
            start: Start time (same formats as get_water_levels).
            end: End time. At most 7 days after start.
        """
        return run(service.compare_observed_vs_predicted, station=station, start=start, end=end)

    return mcp


def main() -> None:
    create_server().run()
