"""One log line for every Minty backend, on the standard library alone.

    2026-10-22T08:42:15.123Z INFO [trace=a1b2c3d4] [user=<uuid>|entity=<uuid>|step=payment.approve] Bill approved

* ``trace`` follows one request across services: taken from an incoming ``X-Request-ID`` when it
  is well formed (``start_trace``), otherwise 8 fresh hex digits; echoed on the response and sent
  on every call to another Minty backend (``outgoing_headers``).
* ``user`` / ``entity`` are bound by the auth layer once a request is authorised (``bind``) - never
  from a client value that has not been checked - and print as full ids.
* ``step`` names the unit of work (``with log_step("payment.approve"):``); a framework sets the
  route name as the fallback.
* Anything not known prints ``-``.

The four values live in ``ContextVar``s, so they reach every line written during the request,
including Django's and werkzeug's own. A new thread starts with an empty context: hand work to one
through ``carry_context`` so it keeps the caller's trace.
"""

from __future__ import annotations

import contextvars
import logging
import re
import secrets
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRACE_HEADER = "X-Request-ID"
NONE = "-"

_trace: contextvars.ContextVar[str] = contextvars.ContextVar("minty_trace", default=NONE)
_user: contextvars.ContextVar[str] = contextvars.ContextVar("minty_user", default=NONE)
_entity: contextvars.ContextVar[str] = contextvars.ContextVar("minty_entity", default=NONE)
_step: contextvars.ContextVar[str] = contextvars.ContextVar("minty_step", default=NONE)

# Lowercase hex only: an incoming header is written into every log line, so anything else
# (newlines, brackets, a forged "[user=...]") is replaced, never echoed.
_TRACE_RE = re.compile(r"[0-9a-f]{8,32}")


def _text(value: object) -> str:
    if value is None:
        return NONE
    text = str(value)
    return text if text else NONE


def start_trace(incoming: str | None = None) -> str:
    """Begin a unit of work (a request, a scheduler pass, a command): every value is cleared,
    and the trace is ``incoming`` when it is 8-32 lowercase hex digits, else a fresh 8."""
    trace = incoming if incoming and _TRACE_RE.fullmatch(incoming) else secrets.token_hex(4)
    _trace.set(trace)
    _user.set(NONE)
    _entity.set(NONE)
    _step.set(NONE)
    return trace


def reset() -> None:
    """Clear every value - the end of a request on a worker that is reused."""
    for var in (_trace, _user, _entity, _step):
        var.set(NONE)


def bind(*, user: object = None, entity: object = None) -> None:
    """Record who the work is for. Only values given are changed; ids are printed with ``str()``,
    so a ``uuid.UUID`` and its string read the same."""
    if user is not None:
        _user.set(_text(user))
    if entity is not None:
        _entity.set(_text(entity))


def set_default_step(name: str | None) -> None:
    """The framework's fallback (a route name): it does not override a step already named."""
    if name and _step.get() == NONE:
        _step.set(name)


@contextmanager
def log_step(name: str) -> Generator[None]:
    """Name the unit of work for the lines written inside the block; the previous step returns
    on exit, so steps nest."""
    token = _step.set(_text(name))
    try:
        yield
    finally:
        _step.reset(token)


def current() -> dict[str, str]:
    return {
        "trace": _trace.get(),
        "user": _user.get(),
        "entity": _entity.get(),
        "step": _step.get(),
    }


def outgoing_headers() -> dict[str, str]:
    """The header to send on a call to another Minty backend - never to Xero, Stripe or any
    third party. Empty outside a trace."""
    trace = _trace.get()
    return {TRACE_HEADER: trace} if trace != NONE else {}


def carry_context[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap ``fn`` so it runs in a copy of the CURRENT context - call this where the thread is
    created: ``threading.Thread(target=carry_context(work), ...)``."""
    ctx = contextvars.copy_context()

    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        return ctx.run(fn, *args, **kwargs)

    return run


class ContextFilter(logging.Filter):
    """Stamp the current trace/user/entity/step on each record. Attached to handlers, so it also
    covers records that propagate from other loggers (django, werkzeug)."""

    def filter(self, record: logging.LogRecord) -> bool:
        values = current()
        for key, value in values.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class MintyFormatter(logging.Formatter):
    """``<UTC ISO, ms>Z LEVEL [trace=…] [user=…|entity=…|step=…] message`` plus any traceback.
    A record that missed ``ContextFilter`` is filled from the current context."""

    def format(self, record: logging.LogRecord) -> str:
        values = current()
        trace, user, entity, step = (
            getattr(record, key, None) or values[key] for key in ("trace", "user", "entity", "step")
        )
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
        line = (
            f"{ts}.{int(record.msecs):03d}Z {record.levelname} [trace={trace}] "
            f"[user={user}|entity={entity}|step={step}] {record.getMessage()}"
        )
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line += "\n" + record.exc_text
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


def logging_config(
    loggers: Mapping[str, str | None],
    *,
    level: str = "INFO",
    log_dir: Path | str | None = None,
    no_propagate: Iterable[str] = (),
    max_bytes: int = 0,
    backup_count: int = 0,
) -> dict[str, Any]:
    """A ``dictConfig`` dict: each named logger (``""`` = root) writes to stderr and, when it
    names one and ``log_dir`` is set, to its own file in ``log_dir`` - all in the one format.
    ``max_bytes`` > 0 rotates the files (``RotatingFileHandler``, ``backup_count`` kept)."""
    no_propagate = set(no_propagate)
    handlers: dict[str, dict[str, Any]] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "minty",
            "filters": ["context"],
        }
    }
    logger_conf: dict[str, dict[str, Any]] = {}
    root: dict[str, Any] | None = None
    if log_dir is not None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    for name, filename in loggers.items():
        names = ["console"]
        if filename and log_dir is not None:
            handler_id = f"file_{filename}"
            file_handler: dict[str, Any] = {
                "formatter": "minty",
                "filters": ["context"],
                "filename": str(Path(log_dir) / filename),
                "encoding": "utf-8",
                "delay": True,
            }
            if max_bytes > 0:
                file_handler.update(
                    {
                        "class": "logging.handlers.RotatingFileHandler",
                        "maxBytes": max_bytes,
                        "backupCount": backup_count,
                    }
                )
            else:
                file_handler["class"] = "logging.FileHandler"
            handlers[handler_id] = file_handler
            names.append(handler_id)
        conf: dict[str, Any] = {"handlers": names, "level": level}
        if name == "":
            root = conf
        else:
            if name in no_propagate:
                conf["propagate"] = False
            logger_conf[name] = conf
    config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": "minty_shared.logctx.ContextFilter"}},
        "formatters": {"minty": {"()": "minty_shared.logctx.MintyFormatter"}},
        "handlers": handlers,
        "loggers": logger_conf,
    }
    if root is not None:
        config["root"] = root
    return config
