"""Readers for the environment variables every Minty backend parses the same way.

Pure functions on the standard library: settings modules, ``docker/entrypoint.sh`` and test
harnesses call them before any framework is loaded. Every refusal is a ``ValueError`` (or
``RuntimeError`` for missing required config); a Django settings module turns it into
``ImproperlyConfigured`` simply by letting it propagate at import, which refuses to boot.

* ``APP_ENV`` - ``development`` only when it says exactly that; anything else is production.
* ``DATABASE_URL`` - ``postgresql://user:pass@host:5432/db?schema=pettycashv3[&sslmode=...]``.
  ``?schema=`` is popped (default ``pettycashv3``) and must be a plain identifier, because it is
  spliced into SQL (``search_path``). A blank value counts as unset.
* ``S3_URL`` - ``https://KEY:SECRET@s3.<region>.backblazeb2.com/<bucket>[?region=...]``.
* ``SMTP_URL`` - ``smtp://user:pass@host:587`` (STARTTLS; ``?tls=0`` off) or
  ``smtps://user:pass@host:465``; ``?timeout=`` seconds, default 10.
* ``PREVIEW_ORIGIN_REGEX`` - one pattern from ``^https://pr-`` to ``\\.dailyminty\\.com$`` with
  no top-level ``|``; anything else refuses to boot (a typo must never open CORS).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

DEFAULT_SCHEMA = "pettycashv3"
DEFAULT_LOCAL_DATABASE_URL = "postgresql://postgres@localhost:5432/postgres"
DEFAULT_SMTP_TIMEOUT = 10.0
DEFAULT_S3_REGION = "us-east-1"

_TRUE = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}
_PG_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}
# The schema name is spliced into SQL as an identifier, so it is held to one.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_S3_REGION_HOST = re.compile(r"^s3\.([a-z0-9-]+)\.")
_PREVIEW_PREFIX = "^https://pr-"
_PREVIEW_SUFFIX = r"\.dailyminty\.com$"


# --------------------------------------------------------------------------- APP_ENV, flags


def app_env() -> str:
    """``development`` only when ``APP_ENV`` says exactly that; otherwise ``production``."""
    raw = (os.environ.get("APP_ENV") or "").strip().lower()
    return "development" if raw == "development" else "production"


def is_development() -> bool:
    return app_env() == "development"


def flag(name: str, default: bool, *, blank_is_unset: bool = False) -> bool:
    """An on/off switch: ``1``/``true``/``yes``/``on`` (any case, padded) is on, anything else
    off; unset is ``default`` (and so is an empty value when ``blank_is_unset``)."""
    raw = os.environ.get(name)
    if raw is None or (blank_is_unset and raw == ""):
        return default
    return raw.strip().lower() in _TRUE


def require_secret_key(secret: str | None, *, placeholder: str, development: bool) -> str:
    """The shared ``SECRET_KEY``, refusing the repo's placeholder outside development.

    On 2026-09-14 an API ran in production without SECRET_KEY: every authenticated call
    answered 401 and nothing said why - and the placeholder is in the repo, so it was
    forgeable too. Refusing to boot is the loud failure."""
    value = secret or placeholder
    if not development and value == placeholder:
        raise RuntimeError(
            "SECRET_KEY is not set. It must be the same value Flask mints tokens with; "
            "without it every request is refused with 401. Refusing to start."
        )
    return value


# --------------------------------------------------------------------------- PREVIEW_ORIGIN_REGEX


def _has_top_level_bar(value: str) -> bool:
    """A ``|`` outside every group and class splits the anchors off into separate branches."""
    depth, in_class, i = 0, False, 0
    while i < len(value):
        ch = value[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            return True
        i += 1
    return False


def preview_origin_pattern(raw: str | None) -> str | None:
    """The validated ``PREVIEW_ORIGIN_REGEX`` source, or None when unset (the feature is off)."""
    value = (raw or "").strip()
    if not value:
        return None
    if not (
        value.startswith(_PREVIEW_PREFIX) and value.endswith(_PREVIEW_SUFFIX)
    ) or _has_top_level_bar(value):
        raise ValueError(
            "PREVIEW_ORIGIN_REGEX must be one pattern from ^https://pr- to \\.dailyminty\\.com$ "
            "with no top-level |."
        )
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"PREVIEW_ORIGIN_REGEX does not compile: {exc}") from None
    return value


def preview_origin_regexes(raw: str | None) -> list[str]:
    """For django-cors-headers' ``CORS_ALLOWED_ORIGIN_REGEXES``: one pattern, or none."""
    pattern = preview_origin_pattern(raw)
    return [pattern] if pattern else []


def preview_origin_regex(raw: str | None) -> re.Pattern[str] | None:
    """The compiled pattern (Flask's hand-off check), or None."""
    pattern = preview_origin_pattern(raw)
    return re.compile(pattern) if pattern else None


# --------------------------------------------------------------------------- DATABASE_URL


@dataclass(frozen=True)
class DatabaseUrl:
    scheme: str
    user: str
    password: str
    host: str
    port: int
    name: str
    schema: str
    #: every query parameter but ``schema``, in order
    params: tuple[tuple[str, str], ...] = field(default=())
    #: the URL's own netloc, path and fragment, still percent-encoded, for rebuilding it
    netloc: str = ""
    path: str = ""
    fragment: str = ""

    @property
    def sqlalchemy(self) -> str:
        """``postgresql+psycopg2://...`` with the schema removed (Flask)."""
        return urlunsplit(
            ("postgresql+psycopg2", self.netloc, self.path, urlencode(self.params), self.fragment)
        )

    @property
    def libpq(self) -> str:
        """``postgresql://...`` with the schema removed (psycopg2.connect, psql)."""
        return urlunsplit(
            ("postgresql", self.netloc, self.path, urlencode(self.params), self.fragment)
        )


def database_url_from_env(default: str | None = None) -> str:
    """``DATABASE_URL`` (blank counts as unset), else ``default``; RuntimeError when neither."""
    raw = (os.environ.get("DATABASE_URL") or "").strip()
    if raw:
        return raw
    if default:
        return default
    raise RuntimeError("DATABASE_URL is not set")


def parse_database_url(url: str) -> DatabaseUrl:
    """Split a Postgres URL into its parts and its ``?schema=`` (default ``pettycashv3``).
    ValueError on any other scheme, or a schema that is not a plain identifier."""
    parts = urlsplit(url.strip())
    if parts.scheme not in _PG_SCHEMES:
        raise ValueError(f"DATABASE_URL must be a postgresql:// URL, not {parts.scheme}://")
    schema = DEFAULT_SCHEMA
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "schema":
            schema = value or DEFAULT_SCHEMA
        else:
            kept.append((key, value))
    if not _IDENTIFIER.fullmatch(schema):
        raise ValueError(f"DATABASE_URL ?schema={schema!r} is not a plain identifier")
    return DatabaseUrl(
        scheme=parts.scheme,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        host=parts.hostname or "",
        port=parts.port or 5432,
        name=unquote(parts.path.lstrip("/")),
        schema=schema,
        params=tuple(kept),
        netloc=parts.netloc,
        path=parts.path,
        fragment=parts.fragment,
    )


def with_schema(url: str, schema: str) -> str:
    """``url`` with its ``?schema=`` set to ``schema`` (every other parameter kept)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "schema"]
    query.append(("schema", schema))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def django_database(url: str) -> tuple[dict[str, object], str]:
    """``(DATABASES["default"], schema)`` for Django: the schema becomes the connection's
    ``search_path`` and every other query parameter rides in ``OPTIONS``."""
    db = parse_database_url(url)
    options: dict[str, str] = dict(db.params)
    options["options"] = f"-c search_path={db.schema},public"
    return (
        {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": db.name,
            "USER": db.user,
            "PASSWORD": db.password,
            "HOST": db.host,
            "PORT": str(db.port),
            "OPTIONS": options,
        },
        db.schema,
    )


# --------------------------------------------------------------------------- S3_URL


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str
    bucket: str
    key: str
    secret: str
    region: str


def parse_s3_url(url: str) -> S3Settings:
    """``https://KEY:SECRET@s3.<region>.backblazeb2.com/<bucket>[?region=...]``. The region is
    ``?region=``, else the host's ``s3.<region>.`` label, else ``us-east-1``. ValueError for a
    non-http(s) URL, no host, or anything but exactly one bucket."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("S3_URL must look like https://KEY:SECRET@host/bucket")
    bucket = unquote(parts.path.strip("/"))
    if not bucket or "/" in bucket:
        raise ValueError("S3_URL must name exactly one bucket as its path")
    host = parts.hostname
    netloc = f"{host}:{parts.port}" if parts.port else host
    region = dict(parse_qsl(parts.query)).get("region")
    if not region:
        match = _S3_REGION_HOST.match(host)
        region = match.group(1) if match else DEFAULT_S3_REGION
    return S3Settings(
        endpoint_url=f"{parts.scheme}://{netloc}",
        bucket=bucket,
        key=unquote(parts.username or ""),
        secret=unquote(parts.password or ""),
        region=region,
    )


# --------------------------------------------------------------------------- SMTP_URL


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool  # STARTTLS
    use_ssl: bool  # implicit TLS
    timeout: float


def parse_smtp_url(url: str) -> SmtpSettings:
    """``smtp://user:pass@host:587`` (STARTTLS; ``?tls=0`` turns it off) or
    ``smtps://user:pass@host:465`` (implicit SSL). ``?timeout=`` seconds, default 10."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"smtp", "smtps"} or not parts.hostname:
        raise ValueError("SMTP_URL must look like smtp://user:pass@host:587 or smtps://...:465")
    ssl = parts.scheme == "smtps"
    query = dict(parse_qsl(parts.query))
    timeout = float(query["timeout"]) if query.get("timeout") else DEFAULT_SMTP_TIMEOUT
    starttls = not ssl and query.get("tls", "1").strip().lower() not in _OFF
    return SmtpSettings(
        host=parts.hostname,
        port=parts.port or (465 if ssl else 587),
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
        use_tls=starttls,
        use_ssl=ssl,
        timeout=timeout,
    )


SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE_BACKEND = "django.core.mail.backends.console.EmailBackend"


def django_email(url: str | None) -> dict[str, object]:
    """Django's ``EMAIL_*`` settings for ``SMTP_URL``. Unset or blank is the console backend:
    every send is written out and skipped."""
    if not (url or "").strip():
        return {
            "EMAIL_BACKEND": CONSOLE_BACKEND,
            "EMAIL_HOST": "",
            "EMAIL_PORT": 587,
            "EMAIL_HOST_USER": "",
            "EMAIL_HOST_PASSWORD": "",
            "EMAIL_USE_TLS": False,
            "EMAIL_USE_SSL": False,
            "EMAIL_TIMEOUT": DEFAULT_SMTP_TIMEOUT,
        }
    smtp = parse_smtp_url(url or "")
    return {
        "EMAIL_BACKEND": SMTP_BACKEND,
        "EMAIL_HOST": smtp.host,
        "EMAIL_PORT": smtp.port,
        "EMAIL_HOST_USER": smtp.username or "",
        "EMAIL_HOST_PASSWORD": smtp.password or "",
        "EMAIL_USE_TLS": smtp.use_tls,
        "EMAIL_USE_SSL": smtp.use_ssl,
        "EMAIL_TIMEOUT": smtp.timeout,
    }
