"""Call another Minty backend as the current caller - the transport of every Flask proxy.

It forwards the caller's own bearer unchanged, so the proxy carries no privilege of its own:
the far side applies exactly the membership and consent checks it would apply to a direct
call, and a caller cannot reach through one service to do something another would refuse.
The far side's status is kept, not flattened (a 402 declined card, a 403 missing consent, a
409 conflict all mean something specific to the screen); only "no usable answer" becomes this
module's own 502/504. The request's trace goes along (``X-Request-ID``).
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.http import HttpRequest, JsonResponse

from minty_shared import logctx
from minty_shared.django.errors import HOUSE_FALLBACK, ApiRefusal

logger = logging.getLogger("minty_shared.client")


class UpstreamUnavailable(ApiRefusal):
    """The far side could not be reached or answered something that is not JSON."""


def bearer_from(request: HttpRequest) -> str:
    """The caller's ``Authorization`` header, verbatim. Raises rather than returning empty:
    every path that forwards has already been through the service's bearer auth, so a
    missing header here is a programming error, not a client problem."""
    headers: Any = request.headers  # a cached_property, which pyright reads as the descriptor
    header: str = headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise UpstreamUnavailable(HOUSE_FALLBACK, status=500)
    return header


def forward(
    request: HttpRequest,
    path: str,
    *,
    base: str,
    timeout: float | tuple[float, float],
    method: str = "POST",
    json: Any = None,
    params: Any = None,
) -> tuple[Any, int]:
    """``(payload, status)`` from ``base`` + ``path``, as the caller. 504 on a timeout, 502 when
    unreachable or the body is not JSON (an HTML error page must never reach a toast)."""
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Authorization": bearer_from(request),
        "Accept": "application/json",
        **logctx.outgoing_headers(),
    }
    verb = method.upper()
    try:
        resp = requests.request(
            verb, url, headers=headers, json=json, params=params, timeout=timeout
        )
    except requests.Timeout:
        logger.warning("Proxy timeout: %s %s", verb, url)
        raise UpstreamUnavailable(HOUSE_FALLBACK, status=504) from None
    except requests.RequestException as exc:
        logger.warning("Proxy failed: %s %s -- %s", verb, url, exc)
        raise UpstreamUnavailable(HOUSE_FALLBACK, status=502) from None
    try:
        payload = resp.json()
    except ValueError:
        logger.warning("Proxy returned non-JSON: %s %s -> %s", verb, url, resp.status_code)
        raise UpstreamUnavailable(HOUSE_FALLBACK, status=502) from None
    return payload, resp.status_code


def proxy(
    request: HttpRequest,
    path: str,
    *,
    base: str,
    timeout: float | tuple[float, float],
    method: str = "POST",
    json: Any = None,
    params: Any = None,
) -> JsonResponse:
    """:func:`forward` as a ``JsonResponse`` a ninja view returns directly: ninja would refuse
    any status the operation declares no schema for, and a proxy relays many."""
    payload, status = forward(
        request, path, base=base, timeout=timeout, method=method, json=json, params=params
    )
    return JsonResponse(payload, status=status, safe=False)
