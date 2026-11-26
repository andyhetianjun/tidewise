"""Check that the facts in an answer came from the tool results.

A language model can write a smooth answer with a time or a height that no tool
ever returned. This module pulls the checkable claims out of the final answer and
looks for each one in the tool outputs the agent actually received:

- times of day ("08:36", "8:36 AM", "14:00 UTC")
- dates ("2026-11-26", "November 26")
- numbers with a unit: heights (m, cm, ft), wind speeds (knots, km/h), air
  temperatures, percentages and distances (km)
- station codes ("00490") and place names ("at Halifax")

A claim is supported if it appears in a tool output (allowing rounding and unit
conversion), if the user already said it in the question, or, for a sentence that
talks about a difference or a range, if it is the gap between two numbers the tools
returned. Anything else is reported as unsupported.

What it does not do: it checks that a number is present, not that it belongs to
the right quantity, and it does not check claims with no number in them ("the wind
is light"). It also cannot check arithmetic beyond one subtraction. It may flag a
correct but unusual calculation; it should not miss a made-up time or height.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

KMH_PER_KNOT = 1.852
M_PER_FT = 0.3048

# ---- reading numbers with units -----------------------------------------

_NUM = r"-?\d+(?:\.\d+)?"
_UNITS = {
    "length": (
        r"(?:m(?![a-zA-Z/²])|metres?|meters?|cm(?![a-zA-Z])|centimet(?:re|er)s?"
        r"|ft(?![a-zA-Z])|feet|foot)"
    ),
    "speed": r"(?:knots?|kn(?![a-zA-Z])|kts?(?![a-zA-Z])|km/h|kph|m/s)",
    "temp": r"(?:°\s?C|°\s?F|℃|degrees?(?:\s+(?:Celsius|C))?)",
    "percent": r"(?:%|percent)",
    "distance": r"(?:km(?![a-zA-Z/])|kilomet(?:re|er)s?)",
}
_QTY = re.compile(
    rf"(?<![\w.:/-])(?P<a>{_NUM})(?:\s*(?:to|-|–|and)\s*(?P<b>{_NUM}))?\s*"
    rf"(?P<unit>{'|'.join(f'(?P<{k}>{v})' for k, v in _UNITS.items())})",
    re.IGNORECASE,
)
_MINUS_PLUS = re.compile(r"\b(minus|plus)\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_BARE_NUM = re.compile(rf"(?<![\w.:/-])({_NUM})(?![\w:/-])")

_TIME = re.compile(
    r"(?<![\d:.])(?P<h>\d{1,2}):(?P<m>\d{2})(?::\d{2})?\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*(?P<tz>UTC|GMT|Z|[A-Z]{2,3}T)\b)?",
    re.IGNORECASE,
)
_TIME_WORD = re.compile(
    r"(?<![\d:.])(?P<h>\d{1,2})\s*(?P<ampm>[ap]\.?m\.?)(?![a-z])", re.IGNORECASE
)
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTHS = [
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
]  # fmt: skip
_TEXT_DATE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_CODE = re.compile(
    r"(?:station|code|gauge)(?:\s+(?:is|number|no\.?))?\s+#?(\d{5})\b|\((\d{5})\)", re.IGNORECASE
)

_PLACE = re.compile(
    r"\b(?:at|in|near|from|for|of|station|to)\s+"
    r"((?:St\.?\s+|Saint\s+)?[A-Z][\w'.\-]*(?:\s+(?:and\s+|of\s+)?[A-Z][\w'.\-]*){0,4})"
)
_PLACE_BEFORE_STATION = re.compile(
    r"\b((?:St\.?\s+|Saint\s+)?[A-Z][\w'.\-]*(?:\s+[A-Z][\w'.\-]*){0,3})\s+(?:station|gauge)\b"
)
_PLACE_AFTER_STATION = re.compile(
    r"\b(?:station|gauge)\s+(?:called|named)\s+((?:St\.?\s+|Saint\s+)?[A-Z][\w'.\-]*"
    r"(?:\s+[A-Z][\w'.\-]*){0,3})"
)
_NOT_PLACES = {
    "canada", "canadian", "environment", "environment canada", "fisheries", "oceans", "iwls",
    "celsius", "fahrenheit", "utc", "gmt", "chart", "datum", "i", "it", "the", "this", "that",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today",
    "tomorrow", "yesterday", "tonight", "morning", "evening", "afternoon", "north", "south",
    "east", "west", "high", "low", "tide", "tides", "note", "also", "however", "if",
    "ecccc", "eccc", "msc geomet", "geomet", "mcp",
}  # fmt: skip
_TZ_WORDS = re.compile(r"^[A-Z]{2,4}$")

_HEDGES = re.compile(
    r"\b(could not|couldn'?t|can'?t|cannot|can not|don'?t know|do not know|no data|"
    r"not available|unavailable|unable|don'?t have|do not have|no station|no such|"
    r"not (?:covered|found|reported|published)|isn'?t (?:covered|available)|"
    r"no (?:forecast|tool|information|record|reading)|only (?:has|covers|includes)|"
    r"outside (?:the|what)|not able|i'?m not sure|no way to)\b",
    re.IGNORECASE,
)
_DOWN_WORDS = re.compile(
    r"\b(below|lower|less|under|fall|falls|fell|drop|dropped|negative|minus)\b", re.I
)
_UP_WORDS = re.compile(r"\b(above|higher|more|over|rise|rises|rose|positive|plus)\b", re.I)
_DERIVED_WORDS = re.compile(
    r"\b(range|difference|differ|differs|rise|rises|fall|falls|drop|change|gap|above|below|"
    r"higher|lower|more than|less than|between|swing)\b",
    re.IGNORECASE,
)


def abstains(answer: str) -> bool:
    """True if the answer says it cannot answer."""
    return bool(_HEDGES.search(answer))


# ---- the evidence -------------------------------------------------------


@dataclass
class Num:
    value: float
    decimals: int
    family: str | None  # length, speed, temp, percent, distance; None = free text, any family
    unit: str | None = None  # for speed: "knots" or "kmh"


def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def _family_unit(unit_text: str) -> tuple[str, str]:
    u = unit_text.lower().replace(" ", "")
    if u.startswith(("knot", "kn", "kt")):
        return "speed", "knots"
    if u in ("km/h", "kph"):
        return "speed", "kmh"
    if u == "m/s":
        return "speed", "ms"
    if u in ("cm", "centimetre", "centimetres", "centimeter", "centimeters"):
        return "length", "cm"
    if u in ("ft", "feet", "foot"):
        return "length", "ft"
    if u.startswith(("m", "metre", "meter")) and not u.startswith(("mi", "mm")) and u != "m/s":
        return "length", "m"
    if u in ("%", "percent"):
        return "percent", "pct"
    if u.startswith("km") or u.startswith("kilomet"):
        return "distance", "km"
    if "f" in u and "°" in u:
        return "temp", "f"
    return "temp", "c"


_NATIVE = {"length": "m", "speed": "knots", "temp": "c", "percent": "pct", "distance": "km"}

_KEY_FAMILY = (
    ("_kmh", "speed", "kmh"),
    ("_knots", "speed", "knots"),
    ("_km", "distance", "km"),
    ("_pct", "percent", "pct"),
    ("_c", "temp", "c"),
    ("_m", "length", "m"),
)


@dataclass
class Evidence:
    nums: list[Num] = field(default_factory=list)
    times: set[tuple[int, int, str]] = field(default_factory=set)  # hour, minute, kind
    dates: set[tuple[int, int]] = field(default_factory=set)  # month, day
    text: str = ""


def _add_text_numbers(ev: Evidence, text: str) -> None:
    covered: list[tuple[int, int]] = []
    for m in _QTY.finditer(text):
        family, unit = _family_unit(m.group("unit"))
        for g in ("a", "b"):
            if m.group(g):
                ev.nums.append(Num(float(m.group(g)), _decimals(m.group(g)), family, unit))
        covered.append(m.span())
    for m in _MINUS_PLUS.finditer(text):
        sign = -1 if m.group(1).lower() == "minus" else 1
        ev.nums.append(Num(sign * float(m.group(2)), _decimals(m.group(2)), "temp", "c"))
        covered.append(m.span())
    for m in _BARE_NUM.finditer(text):
        if not any(a <= m.start() < b for a, b in covered):
            ev.nums.append(Num(float(m.group(1)), _decimals(m.group(1)), None))


_JSON_NUM = re.compile(rf'"([A-Za-z_]+)":\s*({_NUM})(?![\d.])')
_JSON_STR = re.compile(r'"([A-Za-z_]+)":\s*"((?:[^"\\]|\\.)*)"')


def _time_kind(tag: str | None, after_z: bool) -> str:
    if after_z or (tag and tag.upper() in ("UTC", "GMT", "Z")):
        return "utc"
    if tag:
        return "local"
    return "any"


def build_evidence(outputs: list[str]) -> Evidence:
    ev = Evidence()
    for raw in outputs:
        ev.text += " " + raw
        for m in _JSON_NUM.finditer(raw):
            key, text = m.group(1), m.group(2)
            for suffix, family, unit in _KEY_FAMILY:
                if key.endswith(suffix):
                    ev.nums.append(Num(float(text), _decimals(text), family, unit))
                    break
        strings = [m.group(2) for m in _JSON_STR.finditer(raw)]
        if not strings:  # an error message or other plain text
            strings = [raw]
        for s in strings:
            _add_text_numbers(ev, s)
        for m in _TIME.finditer(raw):
            tag = m.group("tz")
            kind = _time_kind(tag, raw[m.end() - 1 : m.end()] == "Z" and not tag)
            h, mi = int(m.group("h")), int(m.group("m"))
            if 0 <= h < 24 and mi < 60:
                ev.times.add((h, mi, kind))
        for m in _ISO_DATE.finditer(raw):
            ev.dates.add((int(m.group(2)), int(m.group(3))))
    return ev


# ---- claims -------------------------------------------------------------


@dataclass
class Claim:
    kind: str  # time, date, quantity, code, place
    text: str
    supported: bool
    support: str  # where it was found, or why it was not
    sentence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GroundingReport:
    claims: list[Claim]

    @property
    def unsupported(self) -> list[Claim]:
        return [c for c in self.claims if not c.supported]

    @property
    def grounded(self) -> bool:
        return not self.unsupported

    def to_dict(self) -> dict[str, Any]:
        return {
            "grounded": self.grounded,
            "claims": [c.to_dict() for c in self.claims],
        }


def _sentences(text: str) -> list[str]:
    # Split on . ! ? ; and newlines, but not inside numbers like 1.98 or 8:36.
    parts = re.split(r"(?<=[a-zA-Z)\]])[.!?]\s+|[;\n]+|(?<=\d)\.\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if p and p.strip()]


def _tol(decimals: int) -> float:
    return 0.5 * 10**-decimals + 1e-9


def _to_base(value: float, family: str, unit: str) -> float:
    """Convert to metres, knots, or Celsius so units can be compared."""
    if family == "length":
        return {"m": 1.0, "cm": 0.01, "ft": M_PER_FT}[unit] * value
    if family == "speed":
        return {"knots": 1.0, "kmh": 1 / KMH_PER_KNOT, "ms": 3600 / 1852}[unit] * value
    if family == "temp":
        return (value - 32) * 5 / 9 if unit == "f" else value
    return value


def _base_tol(decimals: int, family: str, unit: str) -> float:
    return _to_base(_tol(decimals), family, unit) if unit != "f" else _tol(decimals) * 5 / 9


def _quantity_supported(
    value: float, decimals: int, family: str, unit: str, ev: Evidence, sentence: str
) -> str | None:
    want = _to_base(value, family, unit)
    tol = _base_tol(decimals, family, unit)
    for n in ev.nums:
        if n.family not in (family, None):
            continue
        if n.family is None:
            # Free text has no unit. Accept the number as written, or in the unit the
            # data uses for this kind of quantity (knots for marine wind, metres for heights).
            # Numbers written in text are exact, not rounded, so they get no allowance.
            native = _to_base(n.value, family, _NATIVE[family])
            if abs(n.value - value) <= _tol(decimals) or abs(native - want) <= tol:
                return "appears in tool output text"
            continue
        have = _to_base(n.value, n.family, n.unit or unit)
        have_tol = _base_tol(n.decimals, n.family, n.unit or unit)
        if abs(have - want) <= tol + have_tol:
            return "matches a tool output" + ("" if n.unit == unit else " after unit conversion")
    # "0.014 m below the prediction" for a value of -0.014: the sign is carried by the
    # word, so accept a flipped sign only when the sentence says the matching direction.
    for n in ev.nums:
        if n.family != family or not n.unit or n.value == 0:
            continue
        have = _to_base(n.value, n.family, n.unit)
        have_tol = _base_tol(n.decimals, n.family, n.unit)
        if abs(have + want) > tol + have_tol:
            continue
        words = _DOWN_WORDS if have < 0 else _UP_WORDS
        if want > 0 and words.search(sentence):
            return "matches a tool output, direction given in words"
    if _DERIVED_WORDS.search(sentence):
        pool = [n for n in ev.nums if n.family == family and n.unit]
        seen: set[float] = set()
        distinct = []
        for n in pool:
            b = _to_base(n.value, n.family, n.unit or unit)
            if b not in seen:
                seen.add(b)
                distinct.append((b, _base_tol(n.decimals, n.family, n.unit or unit)))
        distinct = distinct[:120]
        for i, (a, ta) in enumerate(distinct):
            for b, tb in distinct[i + 1 :]:
                if abs(abs(a - b) - abs(want)) <= tol + ta + tb:
                    return "difference of two tool output values"
    return None


def _time_supported(
    h: int, m: int, ampm: str | None, tz: str | None, twelve: bool, ev: Evidence
) -> str | None:
    kind = _time_kind(tz, False)
    if ampm:
        ap = ampm.lower()[0]
        options = {(h % 12) + (12 if ap == "p" else 0)}
    elif twelve and 1 <= h <= 12:
        options = {h % 12, (h % 12) + 12}  # "8:36" with no AM/PM could be either
    else:
        options = {h}
    for eh, em, ekind in ev.times:
        kinds_agree = "any" in (kind, ekind) or kind == ekind
        if em == m and eh in options and kinds_agree:
            return "matches a time in a tool output"
    return None


def _norm_name(text: str) -> str:
    text = text.lower().split(",")[0]
    text = re.sub(r"\bst\b\.?", "saint", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def check_grounding(answer: str, tool_outputs: list[str], question: str = "") -> GroundingReport:
    ev = build_evidence(tool_outputs)
    qev = build_evidence([question])
    corpus = " ".join(_norm_name(s) for s in re.split(r'[",{}\[\]:]+', ev.text))
    asked = _norm_name(question)
    claims: list[Claim] = []

    for sentence in _sentences(answer):
        spans: list[tuple[int, int]] = []

        # numbers with units
        for m in _QTY.finditer(sentence):
            family, unit = _family_unit(m.group("unit"))
            spans.append(m.span())
            for g in ("a", "b"):
                if not m.group(g):
                    continue
                num = m.group(g)
                value, dec = float(num), _decimals(num)
                label = f"{num} {m.group('unit').strip()}"
                how = _quantity_supported(value, dec, family, unit, ev, sentence)
                if how is None and _quantity_supported(value, dec, family, unit, qev, ""):
                    how = "stated in the question"
                claims.append(
                    Claim(
                        "quantity",
                        label,
                        how is not None,
                        how or "not in any tool output",
                        sentence,
                    )
                )

        # times
        for m in list(_TIME.finditer(sentence)) + list(_TIME_WORD.finditer(sentence)):
            if any(a <= m.start() < b for a, b in spans):
                continue
            h = int(m.group("h"))
            mi = int(m.groupdict().get("m") or 0)
            if h > 24 or mi > 59:
                continue
            ampm = m.group("ampm")
            tz = m.groupdict().get("tz")
            twelve = not m.group(0).startswith("0") and ":" in m.group(0)
            how = _time_supported(h, mi, ampm, tz, twelve, ev)
            if how is None and _time_supported(h, mi, ampm, tz, twelve, qev):
                how = "stated in the question"
            claims.append(
                Claim(
                    "time",
                    m.group(0).strip(),
                    how is not None,
                    how or "no tool output has this time",
                    sentence,
                )
            )

        # dates
        for m in _ISO_DATE.finditer(sentence):
            md = (int(m.group(2)), int(m.group(3)))
            how = "in a tool output" if md in ev.dates else None
            if how is None and md in qev.dates:
                how = "stated in the question"
            claims.append(
                Claim(
                    "date", m.group(0), how is not None, how or "not in any tool output", sentence
                )
            )
        for m in _TEXT_DATE.finditer(sentence):
            month = (
                _MONTHS.index(next(x for x in _MONTHS if x.startswith(m.group(1).lower()[:3]))) + 1
            )
            md = (month, int(m.group(2)))
            how = "in a tool output" if md in ev.dates else None
            if how is None and md in qev.dates | _question_text_dates(question):
                how = "stated in the question"
            claims.append(
                Claim(
                    "date", m.group(0), how is not None, how or "not in any tool output", sentence
                )
            )

        # station codes
        for m in _CODE.finditer(sentence):
            code = m.group(1) or m.group(2)
            how = "in a tool output" if code in ev.text else None
            if how is None and code in question:
                how = "stated in the question"
            claims.append(
                Claim("code", code, how is not None, how or "not in any tool output", sentence)
            )

        # place names
        place_matches = (
            list(_PLACE.finditer(sentence))
            + list(_PLACE_BEFORE_STATION.finditer(sentence))
            + list(_PLACE_AFTER_STATION.finditer(sentence))
        )
        for m in place_matches:
            name = m.group(1).strip().rstrip(".,")
            norm = _norm_name(name)
            words = norm.split()
            if not words or norm in _NOT_PLACES or words[0] in _NOT_PLACES:
                continue
            if _TZ_WORDS.match(name) or re.match(r"^\d", name):
                continue
            in_data = _name_in(norm, corpus)
            how = "in a tool output" if in_data else None
            if how is None and _name_in(norm, asked):
                how = "stated in the question"
            claims.append(
                Claim("place", name, how is not None, how or "not in any tool output", sentence)
            )
    return GroundingReport(claims)


def _name_in(norm: str, corpus: str) -> bool:
    return f" {norm} " in f" {corpus} "


def _question_text_dates(question: str) -> set[tuple[int, int]]:
    out = set()
    for m in _TEXT_DATE.finditer(question):
        month = next(i for i, x in enumerate(_MONTHS, 1) if x.startswith(m.group(1).lower()[:3]))
        out.add((month, int(m.group(2))))
    return out
