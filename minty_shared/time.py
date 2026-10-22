"""Time zones: the one default zone, and how a stored instant becomes a company's calendar day.

Every instant is stored in UTC (a naive value IS UTC). A company that has never set a zone
(``entities.timezone`` is NULL on every row today) is dated in :data:`DEFAULT_ENTITY_TIMEZONE`,
the zone Flask's ``models.db.tz`` and every service already assumed - now named once. On
``zoneinfo``; a scheduler that needs pytz keeps it internally.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ENTITY_TIMEZONE = "Asia/Hong_Kong"
#: ``entities.timezone`` is a VARCHAR(30).
MAX_TIMEZONE_LENGTH = 30

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_known_zone(name: str | None) -> bool:
    cleaned = (name or "").strip()
    if not cleaned:
        return False
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def zone_named(name: str | None) -> ZoneInfo:
    """The zone ``name`` names; blank or unknown is :data:`DEFAULT_ENTITY_TIMEZONE`. An
    unknown name is a data problem the caller should log (see :func:`is_known_zone`), not a
    reason to fail a page or an email."""
    cleaned = (name or "").strip()
    if cleaned and is_known_zone(cleaned):
        return ZoneInfo(cleaned)
    return ZoneInfo(DEFAULT_ENTITY_TIMEZONE)


def validate_timezone(name: str | None) -> str:
    """A zone name a person may save: a real IANA zone of at most 30 characters.
    ValueError with the reason otherwise."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError(
            "The time zone is still empty. Choose your company's time zone to continue."
        )
    if len(cleaned) > MAX_TIMEZONE_LENGTH:
        raise ValueError(
            f"That time zone name is too long. Use one of at most {MAX_TIMEZONE_LENGTH} characters."
        )
    if not is_known_zone(cleaned):
        raise ValueError("That time zone isn't one I know. Choose it from the list.")
    return cleaned


def in_zone(value: datetime, zone: tzinfo | str | None = None) -> datetime:
    """``value`` on the wall clock of ``zone`` (any ``tzinfo`` - a ``ZoneInfo``, or a pytz zone
    a scheduler hands over - or a zone name; the default when None). A naive value is read
    as UTC, as every instant is stored."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    tz = zone if isinstance(zone, tzinfo) else zone_named(zone)
    return value.astimezone(tz)


def today_in(zone: tzinfo | str | None = None) -> date:
    """Today's date on ``zone``'s calendar - never the server's."""
    return in_zone(datetime.now(UTC), zone).date()


def parse_day(raw: object) -> date | None:
    """A ``YYYY-MM-DD`` string as a date, or None for anything else."""
    if not isinstance(raw, str) or not _ISO_DATE.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
