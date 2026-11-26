"""Finding stations by name or by position."""

from __future__ import annotations

import re

from .errors import TidewiseError
from .geo import haversine_km
from .models import Station


def normalise(text: str) -> str:
    text = text.lower().split(",")[0]
    text = re.sub(r"\bst\b\.?", "saint", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def name_score(station: Station, query: str) -> int | None:
    """Lower is better. None means no match."""
    q = normalise(query)
    if not q:
        return None
    name = normalise(station.name)
    if station.code == query.strip():
        return 0
    if name == q:
        return 1
    if name.startswith(q + " ") or name.startswith(q):
        return 2
    if q in name.split() or f" {q} " in f" {name} ":
        return 3
    if q in name:
        return 4
    return None


def usable(station: Station) -> bool:
    return station.operating and ("wlp" in station.series or "wlo" in station.series)


def _rank(station: Station) -> tuple[int, int, int]:
    return (
        0 if station.operating else 1,
        0 if "wlp" in station.series else 1,
        0 if station.kind == "PERMANENT" else 1,
    )


def search(
    stations: list[Station],
    *,
    query: str | None,
    lat: float | None,
    lon: float | None,
    limit: int,
    max_distance_km: float | None,
    include_inactive: bool,
) -> list[tuple[Station, float | None]]:
    pool = stations if include_inactive else [s for s in stations if usable(s)]
    scored: list[tuple[Station, int]] = []
    for s in pool:
        if query:
            score = name_score(s, query)
            if score is None:
                continue
        else:
            score = 0
        scored.append((s, score))
    results: list[tuple[Station, float | None]] = []
    for s, _ in scored:
        dist = haversine_km(lat, lon, s.latitude, s.longitude) if lat is not None else None
        if max_distance_km is not None and dist is not None and dist > max_distance_km:
            continue
        results.append((s, dist))
    if lat is not None:
        results.sort(key=lambda r: (r[1], _rank(r[0])))
    else:
        scores = {s.code: sc for s, sc in scored}
        results.sort(key=lambda r: (scores[r[0].code], _rank(r[0]), r[0].name))
    return results[:limit]


def resolve(stations: list[Station], ref: str) -> Station:
    """Turn a station code or name into one station, or explain why not."""
    ref = ref.strip()
    if not ref:
        raise TidewiseError("Give a station code (like '00490') or a name (like 'Halifax').")
    matches = []
    for s in stations:
        score = name_score(s, ref)
        if score is not None:
            matches.append((score, _rank(s), s))
    if not matches:
        raise TidewiseError(
            f"No station matches {ref!r}. Use find_stations with a place name, "
            "or with latitude and longitude, to see what exists."
        )
    matches.sort(key=lambda m: (m[0], m[1], m[2].name))
    best = matches[0]
    ties = [m for m in matches if m[:2] == best[:2]]
    if len(ties) > 1:
        options = ", ".join(f"{m[2].name} ({m[2].code})" for m in ties[:6])
        raise TidewiseError(
            f"{ref!r} matches several stations: {options}. Call again with the station code."
        )
    return best[2]
