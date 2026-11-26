"""Models that run with no network and no API key.

ScriptedModel replays a fixed list of turns. Tests use it to check the loop.

RuleModel reads the question with keyword rules, calls the tools, and writes
the answer from the tool results with plain string templates. It is NOT a
language model. It exists so `tidewise eval` and the demo run offline, and it
only understands the phrasing the eval questions use. Scores from it say the
tools, the loop and the checks work together; they say nothing about how well
Claude does. For that, run the eval with --model anthropic.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .agent import ToolCall, Turn, parse_json


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
    if re.search(r"compar|differ|versus| vs |surge|off from", low):
        return "compare"
    if re.search(r"which station|nearest|closest|stations (?:near|in|around)", low):
        return "stations"
    if re.search(r"observed|measured|actual|how high was|how low was|reading", low):
        return "observed"
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
    ):
        if word in low:
            return (today + timedelta(days=delta)).strftime("%Y-%m-%d")
    return None


class RuleModel:
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

    def _call(self, name: str, args: dict, n: int) -> Turn:
        call = ToolCall(f"toolu_rule_{n}", name, args)
        content = [{"type": "tool_use", "id": call.id, "name": name, "input": args}]
        return Turn("", [call], "tool_use", content)

    @staticmethod
    def _say(text: str) -> Turn:
        return Turn(text, [], "end_turn", [{"type": "text", "text": text}])

    # -- the loop --

    def create(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        question = messages[0]["content"]
        results = self._results(messages)
        intent = _intent(question)
        n = len(results) + 1

        if not results:
            coords = _COORDS.search(question)
            if coords:
                lat, lon = float(coords.group(1)), float(coords.group(2))
                return self._call(
                    "find_stations", {"latitude": lat, "longitude": lon, "limit": 3}, n
                )
            place = _place(question)
            if place is None:
                return self._say("I could not tell which place you mean. Name a town or a station.")
            return self._call("find_stations", {"query": place, "limit": 3}, n)

        name, _args, text, is_error = results[-1]
        if is_error:
            return self._say(f"I could not get that: {text.removeprefix('Error executing tool ')}")
        data = parse_json(text) or {}

        if name == "find_stations":
            found = data["stations"]
            if intent == "stations" or _COORDS.search(question):
                lines = [
                    f"{s['name']} ({s['code']})"
                    + (f", {s['distance_km']} km away" if "distance_km" in s else "")
                    for s in found
                ]
                return self._say("Closest stations: " + "; ".join(lines) + ".")
            st = found[0]
            return self._call(*self._plan(intent, st, question), n)

        return self._say(self._summarise(name, data, question))

    # -- planning and writing --

    def _plan(self, intent: str, st: dict, q: str) -> tuple[str, dict]:
        tz = ZoneInfo(st["time_zone"])
        local_today = self._now.astimezone(tz)
        date = _date_word(q, local_today)
        code = st["code"]
        if intent == "hilo":
            return "next_high_low_tide", {
                "station": code,
                "after": date or "now",
                "count": 6 if date else 4,
            }
        if intent == "predictions":
            day = date or local_today.strftime("%Y-%m-%d")
            return "get_tide_predictions", {"station": code, "start": day, "end": day}
        day = date or local_today.strftime("%Y-%m-%d")
        tool = "compare_observed_vs_predicted" if intent == "compare" else "get_water_levels"
        return tool, {"station": code, "start": day, "end": day}

    def _summarise(self, tool: str, d: dict, q: str) -> str:
        st = d["station"]["name"]
        if tool == "next_high_low_tide":
            tz = ZoneInfo(d["station"]["time_zone"])
            date = _date_word(q, self._now.astimezone(tz))
            events = [e for e in d["events"] if not date or e["time_local"].startswith(date)]
            if not events:
                events = d["events"]
                prefix = f"Next tides at {st}: "
            else:
                prefix = f"Tides at {st} on {date}: " if date else f"Next tides at {st}: "
            parts = [
                f"{e['type']} tide at {e['time_local'].split(' ', 1)[1]} ({e['height_m']} m)"
                for e in events
            ]
            return prefix + "; ".join(parts) + "."
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
        return "Done."
