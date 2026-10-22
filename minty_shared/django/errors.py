"""The one error envelope every Minty API answers with (user, 2026-10-22).

    {"error": "<a Part 11 sentence>", "fields": {"amount": "<sentence>"}, ...extra}

* ``error`` - always; a sentence the frontends show as it stands (``@minty/shared``'s
  ``apiClient`` reads ``error`` first). Never ``detail``.
* ``fields`` - only when input is refused: one sentence per field, keyed by the field's name
  as the client sent it, for inline messages.
* Any further key a service routes on (Petty Cash's ``to``, ``report_id``) rides along.

Statuses: input the schema refuses is **422**; a rule's refusal keeps the status the rule names
(400/402/403/404/409/422...); an unauthenticated call is **401** ``{"error": "Unauthorized"}``; a raised ``Http404`` is
**404** with its message;
anything unhandled is **500** with one house sentence - its detail goes to the log, never the
caller.

A service raises :class:`ApiRefusal` (or its own subclass) for a refusal, and maps any exception
type it already has with ``register_error_handlers(api, refusals={MyError: 409})``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError
from ninja.errors import ValidationError as SchemaValidationError

HOUSE_FALLBACK = "Something went wrong on my end. Mind trying again?"
UNAUTHORIZED = "Unauthorized"
NOT_FOUND = "I couldn't find that."

# django-ninja puts the param source in the first segment of ``loc`` and, for a body model,
# the schema argument's name in the second. Neither means anything to the person reading it.
_LOC_NOISE = frozenset(
    {"body", "query", "path", "form", "header", "cookie", "file", "payload", "data", "request"}
)


class ApiRefusal(Exception):  # noqa: N818 - a refusal, not an error: the request was fine to refuse
    """A rule refused the request: ``{"error": message, "fields"?: ..., **extra}``."""

    def __init__(
        self,
        message: str,
        status: int = 400,
        *,
        fields: Mapping[str, str] | None = None,
        **extra: Any,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.fields = dict(fields) if fields else None
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return envelope(self.message, self.fields, **self.extra)


def envelope(message: str, fields: Mapping[str, str] | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"error": message}
    if fields:
        body["fields"] = dict(fields)
    body.update(extra)
    return body


def _field_key(err: Mapping[str, Any]) -> str | None:
    parts = [str(p) for p in (err.get("loc") or ()) if not isinstance(p, bool)]
    meaningful = [p for p in parts if not p.isdigit() and p not in _LOC_NOISE]
    return meaningful[-1] if meaningful else None


def _readable(key: str) -> str:
    return key.replace("_", " ").strip()


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else "{} and {}".format(", ".join(names[:-1]), names[-1])


def _errors(errors: Iterable[Any] | None) -> list[Mapping[str, Any]]:
    return [e for e in (errors or []) if isinstance(e, Mapping)]


def validation_message(errors: Iterable[Any] | None) -> str:
    """ninja's ``[{type, loc, msg}, ...]`` as ONE sentence: whatever reaches a person is a
    sentence, never serialised JSON in a toast."""
    errs = _errors(errors)
    names: list[str] = []
    for err in errs:
        key = _field_key(err)
        if key and _readable(key) and _readable(key) not in names:
            names.append(_readable(key))
    if not names:
        return "Some of those details didn't look right. Mind checking them?"
    joined = _join(names)
    if all(str(e.get("type", "")).startswith("missing") for e in errs):
        return f"I still need {joined} to continue."
    return f"Mind checking {joined}? That didn't look quite right."


def field_messages(errors: Iterable[Any] | None) -> dict[str, str]:
    """One Part 11 sentence per refused field (issue, then action), keyed by its name."""
    out: dict[str, str] = {}
    for err in _errors(errors):
        key = _field_key(err)
        if not key or key in out:
            continue
        name = _readable(key)
        if str(err.get("type", "")).startswith("missing"):
            out[key] = f"The {name} is still empty. Add the {name} to continue."
        else:
            out[key] = f"The {name} doesn't look right. Check it and try again."
    return out


def register_error_handlers(
    api: NinjaAPI,
    *,
    logger: logging.Logger,
    refusals: Mapping[type[Exception], int | Callable[[Exception], int]] | None = None,
    house_fallback: str = HOUSE_FALLBACK,
) -> None:
    """Install the envelope on ``api``. ``refusals`` maps a service's own exception types to
    their status (a number, or a function of the exception, e.g. ``lambda e: e.status``);
    each renders ``{"error": str(exc)}``, plus ``fields``/extra keys when it carries them."""

    @api.exception_handler(AuthenticationError)
    def on_unauthorized(request: Any, exc: Exception):  # pyright: ignore[reportUnusedFunction]
        # Raised by the security layer BEFORE the endpoint: without this, ninja renders
        # {"detail": "Unauthorized"} - a shape no client reads - on the commonest refusal.
        return api.create_response(request, {"error": UNAUTHORIZED}, status=401)

    @api.exception_handler(SchemaValidationError)
    def on_schema_validation(request: Any, exc: SchemaValidationError):  # pyright: ignore[reportUnusedFunction]
        logger.warning("Schema validation refused %s: %s", request.path, exc.errors)
        body = envelope(validation_message(exc.errors), field_messages(exc.errors))
        return api.create_response(request, body, status=422)

    @api.exception_handler(ApiRefusal)
    def on_refusal(request: Any, exc: ApiRefusal):  # pyright: ignore[reportUnusedFunction]
        if exc.status >= 500:
            logger.error("Refused %s on %s: %s", exc.status, request.path, exc.message)
        else:
            logger.warning("Refused %s on %s: %s", exc.status, request.path, exc.message)
        return api.create_response(request, exc.body(), status=exc.status)

    def refusal_handler(
        status: int | Callable[[Exception], int],
    ) -> Callable[[HttpRequest, Exception], HttpResponse]:
        def handler(request: HttpRequest, exc: Exception) -> HttpResponse:
            code = int(status(exc)) if callable(status) else int(status)
            fields = getattr(exc, "fields", None)
            extra = getattr(exc, "extra", None) or {}
            message = getattr(exc, "message", None) or str(exc)
            logger.warning("Refused %s on %s: %s", code, request.path, message)
            return api.create_response(request, envelope(message, fields, **extra), status=code)

        return handler

    for exc_type, status in (refusals or {}).items():
        # ninja types the handler generically over each class; from a mapping pyright cannot
        # infer it. The handler takes any Exception, so it fits every class here.
        api.add_exception_handler(exc_type, refusal_handler(status))  # pyright: ignore[reportArgumentType]

    @api.exception_handler(HttpError)
    def on_http_error(request: Any, exc: HttpError):  # pyright: ignore[reportUnusedFunction]
        # Also ninja's AuthorizationError (403) and Throttled (429), both HttpErrors.
        return api.create_response(request, {"error": str(exc)}, status=exc.status_code)

    @api.exception_handler(Http404)
    def on_not_found(request: Any, exc: Http404):  # pyright: ignore[reportUnusedFunction]
        # ninja's own default answers {"detail": "Not Found"}; a raised Http404's message is
        # the sentence ("Bill not found"), else a generic one.
        return api.create_response(request, {"error": str(exc) or NOT_FOUND}, status=404)

    @api.exception_handler(Exception)
    def on_unhandled(request: Any, exc: Exception):  # pyright: ignore[reportUnusedFunction]
        logger.exception("Unhandled error on %s: %r", request.path, exc)
        return api.create_response(request, {"error": house_fallback}, status=500)
