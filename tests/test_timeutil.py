from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tidewise.errors import TidewiseError
from tidewise.timeutil import check_range, parse_time

HFX = ZoneInfo("America/Halifax")
NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


def test_date_only_is_local_day():
    start = parse_time("2026-11-26", HFX, now=NOW)
    end = parse_time("2026-11-26", HFX, now=NOW, end=True)
    assert start == datetime(2026, 11, 26, 4, tzinfo=UTC)  # AST is UTC-4
    assert end == datetime(2026, 11, 27, 4, tzinfo=UTC)


def test_naive_datetime_is_local_and_offset_is_kept():
    assert parse_time("2026-11-26T08:00", HFX, now=NOW) == datetime(2026, 11, 26, 12, tzinfo=UTC)
    assert parse_time("2026-11-26T08:00:00Z", HFX, now=NOW) == datetime(2026, 11, 26, 8, tzinfo=UTC)


def test_now_and_bad_input():
    assert parse_time("now", HFX, now=NOW) == NOW
    with pytest.raises(TidewiseError, match="ISO 8601"):
        parse_time("next tuesday", HFX, now=NOW)


def test_range_checks():
    a = datetime(2026, 11, 24, tzinfo=UTC)
    with pytest.raises(TidewiseError, match="after the start"):
        check_range(a, a)
    with pytest.raises(TidewiseError, match="at most 7 days"):
        check_range(a, datetime(2026, 12, 5, tzinfo=UTC))
