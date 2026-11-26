"""Download a small weather sample from Environment and Climate Change Canada (MSC GeoMet).

Run:  uv run python scripts/fetch_weather_samples.py

For each offline station this saves the marine forecast areas and the city
forecast points near it, in src/tidewise/data/weather/<station code>.json.
The French text and the parts the tools do not read are dropped to keep files small.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from tidewise.weather import BASE_URL, USER_AGENT, trim

DATA = Path(__file__).resolve().parents[1] / "src" / "tidewise" / "data"
STATIONS = ["00490", "07735", "00065", "01700", "08615"]


def main() -> None:
    stations = {s["code"]: s for s in json.loads((DATA / "stations.json").read_text())["stations"]}
    out_dir = DATA / "weather"
    out_dir.mkdir(exist_ok=True)
    with httpx.Client(base_url=BASE_URL, timeout=60, headers={"User-Agent": USER_AGENT}) as client:
        for code in STATIONS:
            st = stations[code]
            lat, lon = st["latitude"], st["longitude"]
            bbox = f"{lon - 0.5},{lat - 0.5},{lon + 0.5},{lat + 0.5}"
            payload = {}
            for key, collection in (
                ("marine_areas", "marineweather-realtime"),
                ("city_points", "citypageweather-realtime"),
            ):
                resp = client.get(
                    f"/collections/{collection}/items",
                    params={"f": "json", "bbox": bbox, "limit": 50},
                )
                resp.raise_for_status()
                payload[key] = [trim(key, f) for f in resp.json()["features"]]
            path = out_dir / f"{code}.json"
            path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
            print(f"wrote {path.name} ({path.stat().st_size // 1024} KB) for {st['officialName']}")


if __name__ == "__main__":
    main()
