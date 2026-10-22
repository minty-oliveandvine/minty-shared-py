"""minty_shared.time: the default zone, a stored instant on a company's calendar."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from minty_shared import time as mtime

HK = ZoneInfo("Asia/Hong_Kong")


def test_the_default_zone_is_hong_kong():
    assert mtime.DEFAULT_ENTITY_TIMEZONE == "Asia/Hong_Kong"
    assert mtime.zone_named(None) == HK
    assert mtime.zone_named("   ") == HK


def test_a_known_zone_is_used_and_an_unknown_one_falls_back():
    assert mtime.zone_named(" Asia/Singapore ") == ZoneInfo("Asia/Singapore")
    assert mtime.zone_named("Mars/Olympus") == HK
    assert mtime.is_known_zone("Europe/London") and not mtime.is_known_zone("Mars/Olympus")
    assert not mtime.is_known_zone(None)


def test_a_naive_instant_is_utc_and_late_utc_is_tomorrow_in_hong_kong():
    late = datetime(2026, 10, 22, 20, 30)  # naive = UTC
    assert mtime.in_zone(late).date() == date(2026, 10, 23)
    assert mtime.in_zone(late, "Europe/London").date() == date(2026, 10, 22)
    aware = datetime(2026, 10, 22, 20, 30, tzinfo=UTC)
    assert mtime.in_zone(aware, HK).hour == 4


def test_today_in_is_the_zones_calendar_day():
    assert mtime.today_in(HK) == datetime.now(UTC).astimezone(HK).date()


@pytest.mark.parametrize(
    ("raw", "day"),
    [
        ("2026-10-22", date(2026, 10, 22)),
        ("2026-13-01", None),
        ("22/10/2026", None),
        (None, None),
        (20261022, None),
    ],
)
def test_parse_day(raw, day):
    assert mtime.parse_day(raw) == day


def test_iso():
    assert mtime.iso(date(2026, 1, 2)) == "2026-01-02"
    assert mtime.iso(None) is None


def test_validate_timezone():
    assert mtime.validate_timezone(" Asia/Tokyo ") == "Asia/Tokyo"
    with pytest.raises(ValueError, match="still empty"):
        mtime.validate_timezone("")
    with pytest.raises(ValueError, match="too long"):
        mtime.validate_timezone("A" * 31)
    with pytest.raises(ValueError, match="isn't one I know"):
        mtime.validate_timezone("Mars/Olympus")
