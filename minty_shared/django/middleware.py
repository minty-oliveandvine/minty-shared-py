"""One trace per request (``minty_shared.logctx``) and one access line, for every Django API.

An incoming ``X-Request-ID`` is kept when well formed, echoed on the response, and carried by
every line written meanwhile; the auth layer adds the user and entity, and the route's
operation names the step. Then one access line, ``METHOD path status ms``, on the logger the
setting names.

Settings:

* ``MINTY_HTTP_LOGGER`` (required) - the access logger's name, e.g. ``"billing-api.http"``.
* ``MINTY_LOGGABLE_PATH`` (optional) - a dotted path to ``f(path) -> path`` that blanks a
  secret segment before the path is logged (Petty Cash's share links).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.utils.module_loading import import_string

from minty_shared import logctx


def _identity(path: str) -> str:
    return path


def route_name(request: HttpRequest, view_func: Any) -> str | None:
    """The step a route gives its request: the ninja operation for THIS method - one URL
    pattern serves every method of a path, under the FIRST operation's URL name, so
    ``GET /bills/`` would read as the create - else the URL name (a plain Django view, or a
    ninja that changed shape)."""
    for cell in getattr(view_func, "__closure__", None) or ():
        try:
            operations = getattr(cell.cell_contents, "operations", None) or ()
        except ValueError:  # an empty cell
            continue
        for operation in operations:
            if request.method in operation.methods:
                return operation.view_func.__name__
    match = request.resolver_match
    return match.url_name if match else None


class RequestLoggingMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        name = getattr(settings, "MINTY_HTTP_LOGGER", None)
        if not name:
            raise ImproperlyConfigured("MINTY_HTTP_LOGGER is not set (the access logger's name).")
        self.logger = logging.getLogger(name)
        dotted = getattr(settings, "MINTY_LOGGABLE_PATH", None)
        self.loggable_path: Callable[[str], str] = import_string(dotted) if dotted else _identity

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.request_id = logctx.start_trace(request.headers.get(logctx.TRACE_HEADER))  # pyright: ignore[reportAttributeAccessIssue]
        start = time.monotonic()
        try:
            response = self.get_response(request)
            duration_ms = int((time.monotonic() - start) * 1000)
            response[logctx.TRACE_HEADER] = request.request_id  # pyright: ignore[reportAttributeAccessIssue]
            path = self.loggable_path(request.path)
            self.logger.info(
                "%s %s %s %dms",
                request.method,
                path,
                response.status_code,
                duration_ms,
                extra={"endpoint": path, "method": request.method},
            )
            return response
        finally:
            logctx.reset()

    def process_view(
        self, request: HttpRequest, view_func: Any, view_args: Any, view_kwargs: Any
    ) -> None:
        logctx.set_default_step(route_name(request, view_func))
        return None
