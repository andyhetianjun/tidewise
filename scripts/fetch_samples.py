"""Download the small offline sample from the IWLS API into src/tidewise/data/.

Run:  uv run python scripts/fetch_samples.py

The window is fixed so the committed files and the eval cases stay in step.
Re-running it later gives newer data, so the expected answers in
evals/cases.json would need to be checked again.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from tidewise.cache import DiskCache
from tidewise.iwls import IwlsSource
from tidewise.stations import usable

DATA = Path(__file__).resolve().parents[1] / "src" / "tidewise" / "data"
STATIONS = ["00490", "07735", "00065", "01700", "08615"]  # Halifax, Vancouver, ...
PRED_FROM = datetime(2026, 11, 24, tzinfo=UTC)
PRED_TO = datetime(2026, 11, 29, tzinfo=UTC)
OBS_TO = datetime(2026, 11, 25, 12, tzinfo=UTC)


def dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(f"wrote {path.relative_to(DATA.parents[2])} ({path.stat().st_size // 1024} KB)")


def main() -> None:
    source = IwlsSource(DiskCache(Path(tempfile.mkdtemp())))
    all_stations = source.stations()

    keep = [s for s in all_stations if usable(s)]
    dump(
        DATA / "stations.json",
        {
            "fetched": "2026-11-26",
            "stations": [
                {
                    "code": s.code,
                    "id": s.id,
                    "officialName": s.name,
                    "latitude": s.latitude,
                    "longitude": s.longitude,
                    "provinceCode": s.province,
                    "type": s.kind,
                    "operating": s.operating,
                    "timeSeries": [{"code": c} for c in s.series],
                }
                for s in keep
            ],
        },
    )

    by_code = {s.code: s for s in all_stations}
    for code in STATIONS:
        station = by_code[code]
        plan = [
            ("wlp", PRED_FROM, PRED_TO, 15),
            ("wlp-hilo", PRED_FROM, PRED_TO, 0),
            ("wlo", PRED_FROM, OBS_TO, 15),
        ]
        for series, start, end, res in plan:
            if series not in station.series:
                continue
            rows = source.series(station, series, start, end, res)
            if not rows:
                print(f"{code} {series}: no data returned, skipping")
                continue
            dump(
                DATA / "series" / f"{code}.{series}.json",
                {
                    "station": code,
                    "series": series,
                    "resolution_minutes": res or None,
                    "rows": [
                        [r.time.strftime("%Y-%m-%dT%H:%M:%SZ"), round(r.value, 3)] for r in rows
                    ],
                },
            )


if __name__ == "__main__":
    main()
