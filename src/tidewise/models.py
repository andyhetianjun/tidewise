"""Plain data types shared by the data sources and the tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# IWLS stations only carry a province code, so I map it to a time zone.
# Good enough for showing local times; a few border cases are approximate.
PROVINCE_TZ = {
    "BC": "America/Vancouver",
    "AB": "America/Edmonton",
    "SK": "America/Regina",
    "MB": "America/Winnipeg",
    "ON": "America/Toronto",
    "QC": "America/Toronto",
    "NB": "America/Moncton",
    "NS": "America/Halifax",
    "PE": "America/Halifax",
    "NL": "America/St_Johns",
    "YT": "America/Whitehorse",
    "NT": "America/Yellowknife",
    "NU": "America/Iqaluit",
}


@dataclass(frozen=True)
class Station:
    code: str
    id: str
    name: str
    latitude: float
    longitude: float
    province: str
    kind: str  # PERMANENT, SEASONAL, TEMPORARY, ...
    operating: bool
    series: tuple[str, ...]  # time-series codes: wlo, wlp, wlp-hilo, ...

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(PROVINCE_TZ.get(self.province, "UTC"))

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "province": self.province,
            "time_zone": str(self.tz),
            "type": self.kind,
            "operating": self.operating,
            "series": list(self.series),
        }

    @classmethod
    def from_iwls(cls, raw: dict) -> Station:
        return cls(
            code=raw["code"],
            id=raw["id"],
            name=raw["officialName"],
            latitude=raw["latitude"],
            longitude=raw["longitude"],
            province=raw.get("provinceCode") or "",
            kind=raw.get("type", "UNKNOWN"),
            operating=bool(raw.get("operating")),
            series=tuple(t["code"] for t in raw.get("timeSeries", [])),
        )


@dataclass(frozen=True)
class Reading:
    time: datetime  # timezone-aware UTC
    value: float  # metres above chart datum
