"""Offline data source: a small committed sample of real IWLS responses."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .config import DATA_DIR
from .errors import TidewiseError
from .models import Reading, Station


class FixtureSource:
    def __init__(self, directory: Path = DATA_DIR) -> None:
        self._dir = directory
        raw = json.loads((directory / "stations.json").read_text())
        self._stations = [Station.from_iwls(r) for r in raw["stations"]]

    def stations(self) -> list[Station]:
        return self._stations

    def series(
        self, station: Station, code: str, start: datetime, end: datetime, resolution: int
    ) -> list[Reading]:
        path = self._dir / "series" / f"{station.code}.{code}.json"
        if not path.exists():
            if code in station.series:
                # Same wording as the live case where the station is listed but silent.
                return []
            raise TidewiseError(
                f"The offline sample has no {code} data for station {station.code} "
                f"({station.name}). Offline mode only includes a few stations."
            )
        payload = json.loads(path.read_text())
        rows = [
            Reading(datetime.fromisoformat(t).astimezone(UTC), float(v)) for t, v in payload["rows"]
        ]
        if not rows:
            return []
        if end <= rows[0].time or start > rows[-1].time:
            raise TidewiseError(
                f"Offline mode only has {code} data from {rows[0].time:%Y-%m-%d %H:%M}Z to "
                f"{rows[-1].time:%Y-%m-%d %H:%M}Z for {station.name}. "
                "Ask about a time inside that window."
            )
        if code != "wlp-hilo" and resolution == 60:
            rows = [r for r in rows if r.time.minute == 0]
        return [r for r in rows if start <= r.time <= end]
