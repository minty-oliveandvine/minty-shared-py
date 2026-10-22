"""The two JWTs Minty's backends exchange, both HS256 on the shared ``SECRET_KEY`` (extra ``jwt``).

* A **bearer** is minted by Flask for a signed-in person and verified by every service.
  :func:`decode_bearer` allows the same clock skew everywhere (60 s): Flask mints, another host
  verifies, and PyJWT's default of zero refuses a token whose ``iat`` is a second ahead of this
  host's clock - which the wizard read as an expired session (measured 2026-09-14: a laptop ran
  6.5 s ahead of Render).
* A **service assertion** is how a Django API calls one of Flask's internal routes: 60 s long,
  scoped to one route (``scope``) and one company (``entity_id``), optionally naming the person.
  Flask's ``_authenticate_internal_caller`` refuses any other scope.

The secret is passed in rather than read from a framework's settings, so this module has no
framework import.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from minty_shared import logctx

ALGORITHM = "HS256"
CLOCK_SKEW_LEEWAY_SECONDS = 60
SERVICE_ASSERTION_SECONDS = 60


def decode_bearer(
    token: str, secret: str, *, leeway: int = CLOCK_SKEW_LEEWAY_SECONDS, **options: Any
) -> dict[str, Any]:
    """The verified claims of a bearer, or a ``jwt.PyJWTError`` (expired, bad signature,
    malformed). ``options`` are PyJWT's (e.g. ``options={"require": [...]}``)."""
    # PyJWT's own annotations are partial; the claims are a plain JSON object.
    claims: dict[str, Any] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
        token, secret, algorithms=[ALGORITHM], leeway=leeway, **options
    )
    return claims


def service_assertion(
    secret: str,
    scope: str,
    entity_id: object,
    *,
    ttl: int = SERVICE_ASSERTION_SECONDS,
    **claims: Any,
) -> str:
    """A short-lived assertion for one internal route and one company; extra ``claims``
    (``user_id=...``) ride along."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "scope": scope,
        "entity_id": str(entity_id),
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
        **claims,
    }
    token: str = jwt.encode(payload, secret, algorithm=ALGORITHM)  # pyright: ignore[reportUnknownMemberType]
    return token


def assertion_headers(assertion: str) -> dict[str, str]:
    """The headers of a call to another Minty backend: the bearer, and the request's trace
    (``X-Request-ID``) so both services' log lines for it share one id."""
    return {"Authorization": f"Bearer {assertion}", **logctx.outgoing_headers()}
