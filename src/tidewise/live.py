"""A "today" view built from live data at site build time.

The demo site is static, so "live" means: a scheduled workflow builds the site every
few hours, and this module fetches current tides and marine weather for a handful of
stations while it does. Nothing is stored in the repo. If a source is down, that
part falls back to the bundled sample and the page says so.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from . import config
from .cache import DiskCache
from .errors import TidewiseError
from .fixtures import FixtureSource
from .iwls import IwlsSource
from .report import _page
from .service import TideService
from .timeutil import iso_utc
from .weather import EcccSource, WeatherFixtureSource, WeatherService

# Halifax, Saint John, Charlottetown, Vancouver, Tofino: the stations in the bundled sample.
STATIONS = ["00490", "00065", "01700", "07735", "08615"]
HOURS_SHOWN = 6


@contextmanager
def _clock(now: datetime | None):  # noqa: ANN202
    """Pin config.now() while a block runs (the sample needs its own date)."""
    old = os.environ.get("TIDEWISE_NOW")
    if now is not None:
        os.environ["TIDEWISE_NOW"] = now.isoformat()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TIDEWISE_NOW", None)
        else:
            os.environ["TIDEWISE_NOW"] = old


def _tides(tide: TideService, code: str, now: datetime) -> dict:
    st = tide._station(code)
    day = now.astimezone(st.tz).strftime("%Y-%m-%d")
    events = tide.next_high_low_tide(code, after=day, count=8)["events"]
    today = [e for e in events if e["time_local"].startswith(day)]
    curve = tide.get_tide_predictions(code, start=day, end=day, resolution_minutes=60)["points"]
    out: dict[str, Any] = {
        "date": day,
        "events": [
            {"type": e["type"], "time_local": e["time_local"], "height_m": e["height_m"]}
            for e in today
        ],
        "curve": [{"time_utc": p["time_utc"], "height_m": p["height_m"]} for p in curve],
        "observed": None,
    }
    try:
        start = iso_utc(now - timedelta(hours=3))
        rows = tide.get_water_levels(code, start=start, end=iso_utc(now), resolution_minutes=15)
        last = rows["points"][-1]
        out["observed"] = {"time_local": last["time_local"], "height_m": last["height_m"]}
        t = datetime.fromisoformat(last["time_utc"].replace("Z", "+00:00"))
        near = tide.get_tide_predictions(
            code,
            start=iso_utc(t - timedelta(minutes=15)),
            end=iso_utc(t + timedelta(minutes=15)),
            resolution_minutes=15,
        )["points"]
        match = [p for p in near if p["time_utc"] == last["time_utc"]]
        if match:
            out["observed"]["predicted_m"] = match[0]["height_m"]
    except TidewiseError:
        pass  # many stations report no observed readings; the page says so
    return out


def _weather(weather: WeatherService, code: str) -> dict:
    marine = weather.get_marine_forecast(code)
    hourly = weather.get_hourly_weather(code, "now")
    return {
        "marine": {
            "area": marine["marine_area"],
            "issued_local": marine["issued_local"],
            "forecast": marine["forecast"],
            "warnings": marine["warnings"],
        },
        "hourly": {
            "point": hourly["forecast_point"],
            "summary": hourly["summary"],
            "hours": hourly["hours"][:HOURS_SHOWN],
        },
    }


def _attempt(fn: Callable[[], dict]) -> tuple[dict | None, str | None]:
    try:
        return fn(), None
    except Exception as exc:  # any failure means "use the sample for this part"
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"


def collect(
    now: datetime | None = None,
    stations: list[str] | None = None,
    *,
    live_tide: TideService | None = None,
    live_weather: WeatherService | None = None,
) -> dict:
    """Fetch the view for each station. Live first, sample for any part that fails."""
    now = now or datetime.now(UTC)
    if live_tide is None:
        cache = DiskCache(config.cache_dir())
        live_tide = TideService(IwlsSource(cache))
        live_weather = WeatherService(EcccSource(cache), live_tide._station)
    assert live_weather is not None
    sample_tide = TideService(FixtureSource())
    sample_weather = WeatherService(WeatherFixtureSource(), sample_tide._station)
    sample_now = config.FIXTURE_NOW

    views = []
    for code in stations or STATIONS:
        view: dict[str, Any] = {"code": code, "name": code}
        for part, live_fn, sample_fn in (
            (
                "tides",
                lambda c=code: _tides(live_tide, c, now),
                lambda c=code: _tides(sample_tide, c, sample_now),
            ),
            (
                "weather",
                lambda c=code: _weather(live_weather, c),
                lambda c=code: _weather(sample_weather, c),
            ),
        ):
            data, err = _attempt(live_fn)
            source, reason = "live", None
            if data is None:
                source, reason = "sample", err
                with _clock(sample_now):
                    data, err2 = _attempt(sample_fn)
                if data is None:
                    source, reason = "none", f"{reason}; sample also failed ({err2})"
            view[part] = {"source": source, "reason": reason, "data": data}
        try:
            view["name"] = live_tide._station(code).name
        except Exception:
            view["name"] = sample_tide._station(code).name
        views.append(view)
    kinds = {v[p]["source"] for v in views for p in ("tides", "weather")}
    return {
        "built_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "overall": "live" if kinds == {"live"} else ("sample" if "live" not in kinds else "mixed"),
        "stations": views,
    }


# ---- page ----------------------------------------------------------------

LIVE_CSS = """
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:1rem;margin:1rem 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem 1rem}
.card h2{margin:.1rem 0 .4rem;font-size:1.15rem}.card h3{font-size:.95rem;margin:.9rem 0 .3rem}
.card ul{margin:.2rem 0;padding-left:1.1rem}
.tag{font-size:.75rem;font-weight:600;border-radius:99px;padding:0 .5rem;white-space:nowrap}
.tag.live{background:var(--ok-bg);color:var(--ok)}.tag.sample{background:var(--warn-bg);color:var(--warn)}
.tag.none{background:var(--bad-bg);color:var(--bad)}
svg.curve{width:100%;height:auto;display:block}
svg.curve .ax{stroke:var(--line)}svg.curve .ln{stroke:var(--accent);fill:none;stroke-width:2}
svg.curve text{fill:var(--muted);font-size:10px}svg.curve .now{stroke:var(--bad);stroke-dasharray:3 3}
svg.curve .pt{fill:var(--accent)}
"""


def _tag(part: dict, date: str | None = None) -> str:
    src = part["source"]
    label = {"live": "live", "sample": "bundled sample", "none": "unavailable"}[src]
    if src == "sample" and date:
        label += f", {date}"
    return f'<span class="tag {src}">{escape(label)}</span>'


def _svg(tides: dict, now: datetime | None) -> str:
    pts = tides["curve"]
    if len(pts) < 2:
        return ""
    w, h, pl, pr, pt, pb = 300, 120, 30, 8, 8, 20
    times = [datetime.fromisoformat(p["time_utc"].replace("Z", "+00:00")) for p in pts]
    t0, t1 = times[0], times[-1]
    lo = min(p["height_m"] for p in pts)
    hi = max(p["height_m"] for p in pts)
    span = max(hi - lo, 0.1)

    def x(t: datetime) -> float:
        return pl + (t - t0) / (t1 - t0) * (w - pl - pr)

    def y(v: float) -> float:
        return pt + (hi - v) / span * (h - pt - pb)

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(p['height_m']):.1f}"
        for i, (t, p) in enumerate(zip(times, pts, strict=True))
    )
    now_line = ""
    if now and t0 <= now <= t1:
        now_line = (
            f'<line class="now" x1="{x(now):.1f}" x2="{x(now):.1f}" y1="{pt}" y2="{h - pb}"/>'
        )
    return (
        f'<svg class="curve" viewBox="0 0 {w} {h}" role="img" aria-label="Predicted water level over the local day">'
        f'<line class="ax" x1="{pl}" x2="{w - pr}" y1="{h - pb}" y2="{h - pb}"/>'
        f'<text x="2" y="{pt + 8}">{hi:.1f} m</text><text x="2" y="{h - pb}">{lo:.1f} m</text>'
        f'<text x="{pl}" y="{h - 6}">00:00</text><text x="{w - pr - 24}" y="{h - 6}">24:00</text>'
        f'{now_line}<path class="ln" d="{path}"/></svg>'
    )


def _warning(w: object) -> str:
    if not isinstance(w, dict):
        return str(w)
    text = str(w.get("warning", "warning")).lower()
    status = str(w.get("status", "")).lower()
    area = str(w.get("area", ""))
    return f"{text} ({status})" + (f", {area}" if area else "") if status else text


def _station_card(v: dict, now: datetime) -> str:
    tides, wx = v["tides"], v["weather"]
    parts = [f"<h2>{escape(v['name'])}</h2>"]
    td = tides["data"]
    parts.append(f"<h3>Tides today {_tag(tides, td['date'] if td else None)}</h3>")
    if td:
        shown_now = now if tides["source"] == "live" else None
        parts.append(_svg(td, shown_now))
        if td["events"]:
            parts.append(
                "<ul>"
                + "".join(
                    f"<li>{e['type'].capitalize()} {escape(e['time_local'][11:])}: {e['height_m']:.2f} m</li>"
                    for e in td["events"]
                )
                + "</ul>"
            )
        ob = td["observed"]
        if ob:
            diff = (
                f", {ob['height_m'] - ob['predicted_m']:+.2f} m from the prediction"
                if "predicted_m" in ob
                else ""
            )
            parts.append(
                f"<p class='muted'>Last measured: {ob['height_m']:.2f} m at "
                f"{escape(ob['time_local'][11:])}{diff}.</p>"
            )
        else:
            parts.append("<p class='muted'>No recent measured reading from this station.</p>")
    else:
        parts.append(f"<p class='muted'>{escape(tides['reason'] or '')}</p>")
    wd = wx["data"]
    parts.append(f"<h3>Marine weather {_tag(wx)}</h3>")
    if wd:
        m, hr = wd["marine"], wd["hourly"]
        if m["warnings"]:
            parts.append(
                "<p><b>Warnings:</b> "
                + escape("; ".join(_warning(w) for w in m["warnings"]))
                + "</p>"
            )
        else:
            parts.append("<p class='muted'>No marine warnings in effect.</p>")
        for f in m["forecast"][:2]:
            parts.append(
                f"<p><b>{escape(str(f.get('period', '')))}</b> {escape(str(f.get('wind', '')))}</p>"
            )
        s = hr["summary"]
        parts.append(
            f"<p class='muted'>Hourly near {escape(hr['point'])}: wind peaks at "
            f"{s['max_wind_kmh']} km/h ({s['max_wind_knots']} knots) over the forecast, "
            f"{s['min_temperature_c']} to {s['max_temperature_c']} C.</p>"
        )
        parts.append(
            "<div class='tablewrap'><table><tr><th>Local</th><th>Wind km/h</th><th>Temp C</th><th>Sky</th></tr>"
            + "".join(
                f"<tr><td>{escape(h['time_local'][11:])}</td><td>{h['wind_kmh']}</td>"
                f"<td>{h['temperature_c']}</td><td>{escape(str(h['condition']))}</td></tr>"
                for h in hr["hours"]
            )
            + "</table></div>"
        )
    else:
        parts.append(f"<p class='muted'>{escape(wx['reason'] or '')}</p>")
    return f'<div class="card">{"".join(parts)}</div>'


def render_live(data: dict, now: datetime) -> str:
    kind = data["overall"]
    status = {
        "live": "Everything on this page came from the live services when the site was built.",
        "mixed": "Parts marked <b>live</b> came from the live services; parts marked "
        "<b>bundled sample</b> fell back to the sample saved in the repo because a source "
        "did not answer. The sample is from 2026-11-25.",
        "sample": "The live services did not answer when the site was built, so this page shows "
        "the bundled sample from 2026-11-25. It is not today's data.",
    }[kind]
    body = (
        '<p><a href="index.html">&larr; Tidewise demo</a></p>'
        "<h1>Today</h1>"
        f"<p class='muted'>Last updated {escape(data['built_utc'])}. A scheduled workflow rebuilds "
        "this page every 6 hours; it is a snapshot, not a live feed.</p>"
        f'<div class="note">{status} Tides come from the Canadian Hydrographic Service (IWLS), '
        "weather from Environment and Climate Change Canada. Nothing from the build is saved "
        "in the repo.</div>"
        '<div class="cards">' + "".join(_station_card(v, now) for v in data["stations"]) + "</div>"
    )
    return _page("Today - Tidewise", body, "style.css").replace(
        "</head>", f"<style>{LIVE_CSS}</style></head>", 1
    )
