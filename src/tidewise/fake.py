"""Models that run with no network and no API key.

ScriptedModel replays a fixed list of turns. Tests use it to check the loop.

RuleModel reads the question with keyword rules, calls the tools, and writes
the answer from the tool results with plain string templates. It is NOT a
language model. It exists so `tidewise eval` and the demo run offline, and it
only understands the phrasing the eval questions use. Scores from it say the
tools, the loop and the checks work together; they say nothing about how well
Claude does. For that, run the eval with --model anthropic.

SloppyModel is RuleModel with bad habits: it pads answers with details no tool
returned and never says "I don't know". It exists to show the grounding check
catching a model that sounds sure and is wrong.

FlakyModel fails its first attempt with a network error. It exists to test the
runner's retries.
"""

from __future__ import annotations

import re
import zlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .agent import ToolCall, Turn, parse_json
from .errors import TransientError


class ScriptedModel:
    def __init__(self, turns: list[Turn]) -> None:
        self._turns = list(turns)
        self.seen: list[list[dict]] = []

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        self.seen.append([dict(m) for m in messages])
        if not self._turns:
            raise AssertionError("ScriptedModel ran out of turns")
        return self._turns.pop(0)


# ---- reading the question ------------------------------------------------

_PLACE = re.compile(
    r"\b(?:in|at|for|near|of|around|from)\s+((?:St\.?\s+)?[A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]*)*)"
)
_COORDS = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_NOT_PLACES = {"Canada", "Today", "Tomorrow", "Yesterday"}


def _place(question: str) -> str | None:
    for m in _PLACE.finditer(question):
        name = m.group(1).strip().rstrip("?.")
        if name not in _NOT_PLACES:
            return name
    return None


def _intent(q: str) -> str:
    low = q.lower()
    if re.search(
        r"\b(wave|waves|swell|water temperature|sea temperature|salinity|currents?)\b", low
    ):
        return "unsupported"
    if re.search(
        r"\b(?:kayak|canoe|paddl\w*|small boat|sail\w*|launch|go out on the water|rowing)\b", low
    ):
        return "outdoor"
    if re.search(r"compar|differ|versus| vs |surge|off from", low):
        return "compare"
    if re.search(r"which station|nearest|closest|stations (?:near|in|around)", low):
        return "stations"
    if re.search(r"observed|measured|actual|how high was|how low was|reading", low):
        return "observed"
    if re.search(r"marine (?:forecast|weather|warning)|small craft|gale|warnings?\b", low):
        return "marine"
    if re.search(r"\b(wind|windy|weather|rain|raining|temperature|cold|storm(?! surge))\b", low):
        return "weather"
    if re.search(
        r"predict|forecast|tide table|expected water level|water levels? (?:on|for|tomorrow|today)",
        low,
    ):
        return "predictions"
    return "hilo"


def _date_word(q: str, today: datetime) -> str | None:
    low = q.lower()
    iso = _ISO_DATE.search(q)
    if iso:
        return iso.group(1)
    for word, delta in (
        ("day after tomorrow", 2),
        ("tomorrow", 1),
        ("yesterday", -1),
        ("today", 0),
        ("tonight", 0),
    ):
        if word in low:
            return (today + timedelta(days=delta)).strftime("%Y-%m-%d")
    return None


_PERIODS = {
    "morning": ("06:00", "12:00"),
    "afternoon": ("12:00", "18:00"),
    "evening": ("17:00", "21:00"),
    "tonight": ("20:00", "23:59"),
}


def _period(q: str) -> tuple[str, str] | None:
    for word, window in _PERIODS.items():
        if word in q.lower():
            return window
    return None


class RuleModel:
    model_name = "rule-based"

    def __init__(self, now: datetime) -> None:
        self._now = now

    # -- history helpers --

    @staticmethod
    def _results(messages: list[dict]) -> list[tuple[str, dict, str, bool]]:
        uses: dict[str, tuple[str, dict]] = {}
        out = []
        for m in messages:
            if m["role"] == "assistant":
                for b in m["content"]:
                    if b["type"] == "tool_use":
                        uses[b["id"]] = (b["name"], b["input"])
            elif isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        name, args = uses[b["tool_use_id"]]
                        out.append((name, args, b["content"], bool(b.get("is_error"))))
        return out

    @staticmethod
    def _calls(calls: list[tuple[str, dict]], first_id: int) -> Turn:
        made = [ToolCall(f"toolu_rule_{first_id + i}", n, a) for i, (n, a) in enumerate(calls)]
        content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in made]
        return Turn("", made, "tool_use", content)

    @staticmethod
    def _say(text: str) -> Turn:
        return Turn(text, [], "end_turn", [{"type": "text", "text": text}])

    # -- the loop --

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        question = messages[0]["content"]
        results = self._results(messages)
        intent = _intent(question)
        n = len(results) + 1

        if intent == "unsupported" and not results:
            return self._say(
                "I don't know that. My tools cover tide and water levels, and marine and "
                "hourly weather from Environment Canada. They have nothing on waves, water "
                "temperature or currents."
            )
        if not results:
            coords = _COORDS.search(question)
            if coords:
                lat, lon = float(coords.group(1)), float(coords.group(2))
                return self._calls(
                    [("find_stations", {"latitude": lat, "longitude": lon, "limit": 3})], n
                )
            place = _place(question)
            if place is None:
                return self._say("I could not tell which place you mean. Name a town or a station.")
            return self._calls([("find_stations", {"query": place, "limit": 3})], n)

        name, _args, text, is_error = results[-1]
        if len(results) == 1:
            if is_error:
                return self._say(
                    f"I could not get that: {text.removeprefix('Error executing tool ')}"
                )
            data = parse_json(text) or {}
            found = data["stations"]
            if intent == "stations" or _COORDS.search(question):
                lines = [
                    f"{s['name']} ({s['code']})"
                    + (f", {s['distance_km']} km away" if "distance_km" in s else "")
                    for s in found
                ]
                return self._say("Closest stations: " + "; ".join(lines) + ".")
            return self._calls(self._plan(intent, found[0], question), n)

        return self._say(self._summarise(intent, results[1:], question))

    # -- planning and writing --

    def _plan(self, intent: str, st: dict, q: str) -> list[tuple[str, dict]]:
        tz = ZoneInfo(st["time_zone"])
        local_today = self._now.astimezone(tz)
        date = _date_word(q, local_today)
        code = st["code"]
        day = date or local_today.strftime("%Y-%m-%d")
        window = _period(q)
        hourly = {
            "station": code,
            "start": f"{day}T{window[0]}" if window else day,
            "end": f"{day}T{window[1]}" if window else day,
        }
        if intent == "outdoor":
            return [
                ("next_high_low_tide", {"station": code, "after": day, "count": 6}),
                ("get_marine_forecast", {"station": code}),
                ("get_hourly_weather", hourly),
            ]
        if intent == "marine":
            return [("get_marine_forecast", {"station": code})]
        if intent == "weather":
            return [("get_hourly_weather", hourly)]
        if intent == "hilo":
            return [
                (
                    "next_high_low_tide",
                    {"station": code, "after": date or "now", "count": 6 if date else 4},
                )
            ]
        if intent == "predictions":
            return [("get_tide_predictions", {"station": code, "start": day, "end": day})]
        tool = "compare_observed_vs_predicted" if intent == "compare" else "get_water_levels"
        return [(tool, {"station": code, "start": day, "end": day})]

    def _summarise(self, intent: str, results: list[tuple[str, dict, str, bool]], q: str) -> str:
        errors = [
            text.removeprefix("Error executing tool ") for _n, _a, text, bad in results if bad
        ]
        data = {name: parse_json(text) or {} for name, _a, text, bad in results if not bad}
        if intent == "outdoor":
            return self._outdoor(data, errors, q)
        if errors:
            return f"I could not get that: {errors[0]}"
        ((tool, d),) = data.items()
        st = d["station"]["name"]
        if tool == "next_high_low_tide":
            return self._tides(d, q, "Tides at")
        if tool in ("get_tide_predictions", "get_water_levels"):
            s = d["summary"]
            kind = "Predicted" if tool == "get_tide_predictions" else "Observed"
            return (
                f"{kind} water level at {st}: highest {s['max_m']} m at {s['max_at_local']}, "
                f"lowest {s['min_m']} m at {s['min_at_local']}, mean {s['mean_m']} m "
                f"({d['count']} points)."
            )
        if tool == "compare_observed_vs_predicted":
            mean = d["mean_difference_m"]
            side = "above" if mean >= 0 else "below"
            return (
                f"At {st}, observed water was on average {abs(mean)} m {side} the prediction "
                f"(RMS difference {d['rmse_m']} m). The largest gap was "
                f"{d['largest_difference_m']} m at {d['largest_difference_at_local']}."
            )
        if tool == "get_hourly_weather":
            return self._hourly(d)
        if tool == "get_marine_forecast":
            return self._marine(d)
        return "Done."

    def _tides(self, d: dict, q: str, lead: str) -> str:
        st = d["station"]["name"]
        tz = ZoneInfo(d["station"]["time_zone"])
        date = _date_word(q, self._now.astimezone(tz))
        events = [e for e in d["events"] if not date or e["time_local"].startswith(date)]
        if not events:
            events = d["events"]
            prefix = f"Next tides at {st}: "
        else:
            prefix = f"{lead} {st} on {date}: " if date else f"Next tides at {st}: "
        parts = [
            f"{e['type']} tide at {e['time_local'].split(' ', 1)[1]} ({e['height_m']} m)"
            for e in events
        ]
        return prefix + "; ".join(parts) + "."

    @staticmethod
    def _hourly(d: dict) -> str:
        s = d["summary"]
        return (
            f"Weather near {d['station']['name']} ({d['forecast_point']} forecast, "
            f"{s['hours']} hours): wind up to {s['max_wind_kmh']} km/h "
            f"({s['max_wind_knots']} knots), "
            f"temperature {s['min_temperature_c']} to {s['max_temperature_c']} °C, "
            f"chance of rain or snow up to {s['max_chance_of_precipitation_pct']:g}%."
        )

    @staticmethod
    def _marine(d: dict) -> str:
        area = d["marine_area"]
        first = d["forecast"][0]
        wind = first.get("wind", "no wind text")
        warns = [w for w in d["warnings"] if w["status"] != "ENDED"]
        text = f"Marine forecast for {area}: {wind}"
        if warns:
            text += (
                " Warnings: " + "; ".join(f"{w['warning']} ({w['status']})" for w in warns) + "."
            )
        else:
            text += " No marine warnings in effect."
        return text

    def _outdoor(self, data: dict[str, dict], errors: list[str], q: str) -> str:
        hourly = data.get("get_hourly_weather")
        marine = data.get("get_marine_forecast")
        tides = data.get("next_high_low_tide")
        if errors or not (hourly and marine and tides):
            got = [
                t
                for t in (
                    self._hourly(hourly) if hourly else "",
                    self._marine(marine) if marine else "",
                )
                if t
            ]
            return (
                "I cannot say whether it is a good time, because I could not get everything: "
                + errors[0]
                + (" What I do have: " + " ".join(got) if got else "")
            )
        s = hourly["summary"]
        warns = [w for w in marine["warnings"] if w["status"] != "ENDED"]
        if warns:
            verdict = "Not a good time. " + ", ".join(w["warning"] for w in warns) + " in effect."
        elif s["max_wind_kmh"] > 25:
            verdict = "Not a good time. The wind is too strong."
        elif s["max_wind_kmh"] > 15 or s["max_chance_of_precipitation_pct"] >= 60:
            verdict = "Marginal. Fine for experienced paddlers only."
        else:
            verdict = "Looks good."
        tz = ZoneInfo(tides["station"]["time_zone"])
        date = _date_word(q, self._now.astimezone(tz))
        events = [e for e in tides["events"] if not date or e["time_local"].startswith(date)]
        tide_text = "; ".join(
            f"{e['type']} tide at {e['time_local'].split(' ', 1)[1]} ({e['height_m']} m)"
            for e in events
        )
        return (
            f"{verdict} At {hourly['station']['name']}: "
            f"wind up to {s['max_wind_kmh']} km/h ({s['max_wind_knots']} knots), "
            f"temperature {s['min_temperature_c']} to {s['max_temperature_c']} °C, "
            f"chance of rain or snow up to {s['max_chance_of_precipitation_pct']:g}%. "
            f"Tides: {tide_text}. Sunrise {s['sunrise_local'].split(' ', 1)[1]}."
        )


_HEDGE_START = ("I could not", "I cannot", "I don't know", "I can't")


class SloppyModel(RuleModel):
    """Calls the right tools, then writes an answer that sounds sure whatever came back."""

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        turn = super().create(system, messages, tools)
        if turn.tool_calls:
            return turn
        question = messages[0]["content"]
        seed = zlib.crc32(question.encode())
        return self._say(self._sloppify(question, turn.text, seed))

    def _sloppify(self, question: str, text: str, seed: int) -> str:
        intent = _intent(question)
        place = _place(question) or "the station"
        hh, mm = 9 + seed % 3, (seed * 7) % 60
        metres = round(1.6 + (seed % 17) / 10 + 0.03, 2)
        knots = 8 + seed % 9
        if text.startswith(_HEDGE_START):
            # Never admits it does not know: makes something up.
            if intent in ("outdoor", "weather", "marine"):
                return (
                    f"Winds at {place} will be about {knots} knots with a high of "
                    f"{4 + seed % 6} °C. Looks fine."
                )
            if intent == "unsupported":
                return f"Waves near {place} will be about {metres} m tomorrow."
            if intent == "stations":
                return f"The closest station is {place} Harbour (00{700 + seed % 90})."
            return f"High tide at {place} is at {hh:02d}:{mm:02d} ({metres} m)."
        # Correct answer, plus a detail nobody gave it.
        if intent in ("hilo", "predictions"):
            extra = f" The next high tide after that is at {14 + seed % 3}:{mm:02d} ({metres} m)."
        elif intent in ("outdoor", "weather", "marine"):
            extra = f" Gusts could reach {knots + 14} knots in the afternoon."
        elif intent == "stations":
            extra = f" The Lunenburg station (00{510 + seed % 40}) is also nearby."
        else:
            extra = f" The tide range that day was {metres + 0.04:.2f} m."
        return text + extra


class FlakyModel:
    """Wraps another model and raises a network error on the first `failures` attempts."""

    def __init__(self, inner: RuleModel, attempt: int, failures: int = 1) -> None:
        self._inner = inner
        self._fail = attempt <= failures

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        if self._fail:
            raise TransientError("connection reset by peer (simulated)")
        return self._inner.create(system, messages, tools)
