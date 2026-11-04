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

#: A company's zone from its country (ISO 3166-1 alpha-2): one zone per country, so a
#: country with several (US, CA, AU, BR, RU, ...) gets its capital's or most populous one.
#: Onboarding stores it as ``entities.timezone`` and the v2 -> v3 load fills it from here.
#: The client tracker keeps the same map (tracker/public/app.js ``COUNTRY_TZ``) - change
#: the two together.
#: Kept several to a line, like tracker's copy, so the two compare by eye.
# fmt: off
COUNTRY_TIMEZONES: dict[str, str] = {
    "AD": "Europe/Andorra", "AE": "Asia/Dubai", "AF": "Asia/Kabul", "AG": "America/Antigua",
    "AI": "America/Anguilla", "AL": "Europe/Tirane", "AM": "Asia/Yerevan", "AO": "Africa/Luanda",
    "AQ": "Antarctica/McMurdo", "AR": "America/Argentina/Buenos_Aires", "AS": "Pacific/Pago_Pago",
    "AT": "Europe/Vienna", "AU": "Australia/Sydney", "AW": "America/Aruba",
    "AX": "Europe/Mariehamn", "AZ": "Asia/Baku", "BA": "Europe/Sarajevo", "BB": "America/Barbados",
    "BD": "Asia/Dhaka", "BE": "Europe/Brussels", "BF": "Africa/Ouagadougou", "BG": "Europe/Sofia",
    "BH": "Asia/Bahrain", "BI": "Africa/Bujumbura", "BJ": "Africa/Porto-Novo",
    "BL": "America/St_Barthelemy", "BM": "Atlantic/Bermuda", "BN": "Asia/Brunei",
    "BO": "America/La_Paz", "BQ": "America/Kralendijk", "BR": "America/Sao_Paulo",
    "BS": "America/Nassau", "BT": "Asia/Thimphu", "BV": "Europe/Oslo", "BW": "Africa/Gaborone",
    "BY": "Europe/Minsk", "BZ": "America/Belize", "CA": "America/Toronto", "CC": "Indian/Cocos",
    "CD": "Africa/Kinshasa", "CF": "Africa/Bangui", "CG": "Africa/Brazzaville",
    "CH": "Europe/Zurich", "CI": "Africa/Abidjan", "CK": "Pacific/Rarotonga",
    "CL": "America/Santiago", "CM": "Africa/Douala", "CN": "Asia/Shanghai", "CO": "America/Bogota",
    "CR": "America/Costa_Rica", "CU": "America/Havana", "CV": "Atlantic/Cape_Verde",
    "CW": "America/Curacao", "CX": "Indian/Christmas", "CY": "Asia/Nicosia", "CZ": "Europe/Prague",
    "DE": "Europe/Berlin", "DJ": "Africa/Djibouti", "DK": "Europe/Copenhagen",
    "DM": "America/Dominica", "DO": "America/Santo_Domingo", "DZ": "Africa/Algiers",
    "EC": "America/Guayaquil", "EE": "Europe/Tallinn", "EG": "Africa/Cairo",
    "EH": "Africa/El_Aaiun", "ER": "Africa/Asmara", "ES": "Europe/Madrid",
    "ET": "Africa/Addis_Ababa", "FI": "Europe/Helsinki", "FJ": "Pacific/Fiji",
    "FK": "Atlantic/Stanley", "FM": "Pacific/Pohnpei", "FO": "Atlantic/Faroe",
    "FR": "Europe/Paris", "GA": "Africa/Libreville", "GB": "Europe/London",
    "GD": "America/Grenada", "GE": "Asia/Tbilisi", "GF": "America/Cayenne",
    "GG": "Europe/Guernsey", "GH": "Africa/Accra", "GI": "Europe/Gibraltar", "GL": "America/Nuuk",
    "GM": "Africa/Banjul", "GN": "Africa/Conakry", "GP": "America/Guadeloupe",
    "GQ": "Africa/Malabo", "GR": "Europe/Athens", "GS": "Atlantic/South_Georgia",
    "GT": "America/Guatemala", "GU": "Pacific/Guam", "GW": "Africa/Bissau", "GY": "America/Guyana",
    "HK": "Asia/Hong_Kong", "HM": "Indian/Kerguelen", "HN": "America/Tegucigalpa",
    "HR": "Europe/Zagreb", "HT": "America/Port-au-Prince", "HU": "Europe/Budapest",
    "ID": "Asia/Jakarta", "IE": "Europe/Dublin", "IL": "Asia/Jerusalem",
    "IM": "Europe/Isle_of_Man", "IN": "Asia/Kolkata", "IO": "Indian/Chagos", "IQ": "Asia/Baghdad",
    "IR": "Asia/Tehran", "IS": "Atlantic/Reykjavik", "IT": "Europe/Rome", "JE": "Europe/Jersey",
    "JM": "America/Jamaica", "JO": "Asia/Amman", "JP": "Asia/Tokyo", "KE": "Africa/Nairobi",
    "KG": "Asia/Bishkek", "KH": "Asia/Phnom_Penh", "KI": "Pacific/Tarawa", "KM": "Indian/Comoro",
    "KN": "America/St_Kitts", "KP": "Asia/Pyongyang", "KR": "Asia/Seoul", "KW": "Asia/Kuwait",
    "KY": "America/Cayman", "KZ": "Asia/Almaty", "LA": "Asia/Vientiane", "LB": "Asia/Beirut",
    "LC": "America/St_Lucia", "LI": "Europe/Vaduz", "LK": "Asia/Colombo", "LR": "Africa/Monrovia",
    "LS": "Africa/Maseru", "LT": "Europe/Vilnius", "LU": "Europe/Luxembourg", "LV": "Europe/Riga",
    "LY": "Africa/Tripoli", "MA": "Africa/Casablanca", "MC": "Europe/Monaco",
    "MD": "Europe/Chisinau", "ME": "Europe/Podgorica", "MF": "America/Marigot",
    "MG": "Indian/Antananarivo", "MH": "Pacific/Majuro", "MK": "Europe/Skopje",
    "ML": "Africa/Bamako", "MM": "Asia/Yangon", "MN": "Asia/Ulaanbaatar", "MO": "Asia/Macau",
    "MP": "Pacific/Saipan", "MQ": "America/Martinique", "MR": "Africa/Nouakchott",
    "MS": "America/Montserrat", "MT": "Europe/Malta", "MU": "Indian/Mauritius",
    "MV": "Indian/Maldives", "MW": "Africa/Blantyre", "MX": "America/Mexico_City",
    "MY": "Asia/Kuala_Lumpur", "MZ": "Africa/Maputo", "NA": "Africa/Windhoek",
    "NC": "Pacific/Noumea", "NE": "Africa/Niamey", "NF": "Pacific/Norfolk", "NG": "Africa/Lagos",
    "NI": "America/Managua", "NL": "Europe/Amsterdam", "NO": "Europe/Oslo", "NP": "Asia/Kathmandu",
    "NR": "Pacific/Nauru", "NU": "Pacific/Niue", "NZ": "Pacific/Auckland", "OM": "Asia/Muscat",
    "PA": "America/Panama", "PE": "America/Lima", "PF": "Pacific/Tahiti",
    "PG": "Pacific/Port_Moresby", "PH": "Asia/Manila", "PK": "Asia/Karachi", "PL": "Europe/Warsaw",
    "PM": "America/Miquelon", "PN": "Pacific/Pitcairn", "PR": "America/Puerto_Rico",
    "PS": "Asia/Gaza", "PT": "Europe/Lisbon", "PW": "Pacific/Palau", "PY": "America/Asuncion",
    "QA": "Asia/Qatar", "RE": "Indian/Reunion", "RO": "Europe/Bucharest", "RS": "Europe/Belgrade",
    "RU": "Europe/Moscow", "RW": "Africa/Kigali", "SA": "Asia/Riyadh", "SB": "Pacific/Guadalcanal",
    "SC": "Indian/Mahe", "SD": "Africa/Khartoum", "SE": "Europe/Stockholm", "SG": "Asia/Singapore",
    "SH": "Atlantic/St_Helena", "SI": "Europe/Ljubljana", "SJ": "Arctic/Longyearbyen",
    "SK": "Europe/Bratislava", "SL": "Africa/Freetown", "SM": "Europe/San_Marino",
    "SN": "Africa/Dakar", "SO": "Africa/Mogadishu", "SR": "America/Paramaribo",
    "SS": "Africa/Juba", "ST": "Africa/Sao_Tome", "SV": "America/El_Salvador",
    "SX": "America/Lower_Princes", "SY": "Asia/Damascus", "SZ": "Africa/Mbabane",
    "TC": "America/Grand_Turk", "TD": "Africa/Ndjamena", "TF": "Indian/Kerguelen",
    "TG": "Africa/Lome", "TH": "Asia/Bangkok", "TJ": "Asia/Dushanbe", "TK": "Pacific/Fakaofo",
    "TL": "Asia/Dili", "TM": "Asia/Ashgabat", "TN": "Africa/Tunis", "TO": "Pacific/Tongatapu",
    "TR": "Europe/Istanbul", "TT": "America/Port_of_Spain", "TV": "Pacific/Funafuti",
    "TW": "Asia/Taipei", "TZ": "Africa/Dar_es_Salaam", "UA": "Europe/Kyiv", "UG": "Africa/Kampala",
    "UM": "Pacific/Wake", "US": "America/New_York", "UY": "America/Montevideo",
    "UZ": "Asia/Tashkent", "VA": "Europe/Vatican", "VC": "America/St_Vincent",
    "VE": "America/Caracas", "VG": "America/Tortola", "VI": "America/St_Thomas",
    "VN": "Asia/Ho_Chi_Minh", "VU": "Pacific/Efate", "WF": "Pacific/Wallis", "WS": "Pacific/Apia",
    "YE": "Asia/Aden", "YT": "Indian/Mayotte", "ZA": "Africa/Johannesburg", "ZM": "Africa/Lusaka",
    "ZW": "Africa/Harare",
}
# fmt: on

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


def country_timezone(country_code: str | None) -> str | None:
    """The zone :data:`COUNTRY_TIMEZONES` gives ``country_code``; None for a country it
    does not list."""
    return COUNTRY_TIMEZONES.get((country_code or "").strip().upper())


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
