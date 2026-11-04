"""Reading a document with Gemini - the engine every reader shares (owner, 2026-11-04).

Lifted from Flask's ``blueprints/report/services/expense_ai.py`` (Stage 1, docs/expense_ai/ in
Minty), whose rules it keeps. A *reader* (minty-petty-cash-api's receipts, minty-payment-request-
api's invoices) brings only what is its own - the instruction, the cacheable context text, the
reply's Pydantic model and the check of that reply against the lists it sent - and calls
``read``. Everything else is here:

* configuration, per reader by prefix (``ReaderConfig("EXPENSE_AI")`` reads ``EXPENSE_AI_ENABLED``,
  ``_MODEL``, ...), read on every call so the kill switch and cut-offs turn without a deploy. The
  route to the model (Vertex or the direct API, its tier) is one setting for every reader;
* the client (one per process and timeout), the call with at most ONE retry and only for the
  retryable classes, the per-process rate limit, usage and estimated cost;
* the reply's shape: a field is ``{value, confidence, band, applied}``, and ``applied`` is the
  page's instruction (a low band is never prefilled).

FAILURE IS ALWAYS SILENT to the person: every error returns "no suggestion" with a ``REASON_*``
code, and the form works by hand as before. The reply is a suggestion - nothing here writes any
record. Extra ``ai``.

ROUTES. Vertex AI (``GOOGLE_CLOUD_PROJECT``; IAM, a regional residency commitment) is chosen
whenever a project is set. The route in use is the direct Gemini API, PAID tier (business
decision, 3 Sep 2026): content is not used to improve Google's products and no human reviewer
reads it, but there is no region to pin, so documents may be processed outside Singapore. The
free tier is unusable for customer documents; the tier is stated in configuration
(``EXPENSE_AI_DIRECT_TIER``) and ``_check_tier_matches_reality`` shouts when Google's own quota
errors contradict it.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from minty_shared.config import flag

logger = logging.getLogger("minty-shared.ai")

# One of these accompanies every "no suggestion" answer. For the logs; the page decides what,
# if anything, the person sees.
REASON_DISABLED = "disabled"
REASON_RATE_LIMITED = "rate_limited"
REASON_UNSUPPORTED_FILE = "unsupported_file"
REASON_FILE_TOO_LARGE = "file_too_large"
REASON_NO_CONTEXT = "no_context"
REASON_NOT_CONFIGURED = "not_configured"
REASON_PROVIDER_ERROR = "provider_error"
REASON_TIMEOUT = "timeout"
REASON_SAFETY_BLOCKED = "safety_blocked"
REASON_TRUNCATED = "truncated"
REASON_INVALID_REPLY = "invalid_reply"
REASON_NO_USABLE_FIELD = "no_usable_field"

# Content sniffing: the declared extension and the browser's type are not evidence.
_MAGIC = (
    (b"%PDF", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

# Model list prices, USD per 1M tokens (input, output), checked 3 Sep 2026. An indication for
# the log's cost field, not an invoice; re-verify at each stage gate.
PRICING = {
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
}


# --------------------------------------------------------------------------- configuration


def _env(name: str, default: Any = None) -> Any:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(_env(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(_env(name, default)).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ReaderConfig:
    """One reader's settings, ``<prefix>_*``, read from the environment on each call."""

    prefix: str

    @property
    def label(self) -> str:
        """The log tag: ``EXPENSE_AI`` -> ``expense_ai``."""
        return self.prefix.lower()

    def enabled(self) -> bool:
        """The kill switch. Default off - a reader ships dark."""
        return flag(f"{self.prefix}_ENABLED", False, blank_is_unset=True)

    def model_id(self) -> str:
        """MUST be a Gemini 3.x id: ``interactions.create`` (the only API with implicit caching)
        serves 3.x only, and the 2.5 family answers 404 here though ``models.list`` shows it."""
        return _env(f"{self.prefix}_MODEL", "gemini-3.5-flash")

    def thinking_level(self) -> str:
        level = str(_env(f"{self.prefix}_THINKING_LEVEL", "low")).strip().lower()
        return level if level in ("low", "medium", "high") else "low"

    def max_file_bytes(self) -> int:
        return _env_int(f"{self.prefix}_MAX_FILE_MB", 10) * 1024 * 1024

    def timeout_seconds(self) -> int:
        return _env_int(f"{self.prefix}_TIMEOUT_S", 25)

    def max_output_tokens(self) -> int:
        """Thinking tokens come out of this same allowance on Gemini 3.x: a long think truncates
        the JSON mid-object at a budget that looks ample for the reply alone."""
        return _env_int(f"{self.prefix}_MAX_OUTPUT_TOKENS", 4096)

    def confidence_cutoffs(self) -> tuple[float, float]:
        """(high, medium). Set from measured data before a pilot, never guessed."""
        return (
            _env_float(f"{self.prefix}_CONF_HIGH", 0.85),
            _env_float(f"{self.prefix}_CONF_MEDIUM", 0.60),
        )

    def rate_user_per_minute(self) -> int:
        return _env_int(f"{self.prefix}_RATE_USER_PER_MIN", 10)

    def rate_entity_per_hour(self) -> int:
        return _env_int(f"{self.prefix}_RATE_ENTITY_PER_HOUR", 200)


def uses_vertex() -> bool:
    """Vertex is the route unless someone deliberately opted out of it."""
    if _env("GOOGLE_CLOUD_PROJECT"):
        return flag("EXPENSE_AI_USE_VERTEX", True, blank_is_unset=True)
    return False


def direct_tier() -> str:
    """The Gemini API terms we run under, "paid" or "free". Nothing at the call site can tell them
    apart, so the safe default for an unconfigured key is the restrictive one: claiming "paid" is
    a deliberate act."""
    tier = str(_env("EXPENSE_AI_DIRECT_TIER", "free")).strip().lower()
    return tier if tier in ("free", "paid") else "free"


def location() -> str:
    """Recorded on every log line. On the direct API there is no region, and that absence is the
    fact worth recording: the value names the route and the tier instead."""
    if uses_vertex():
        return _env("EXPENSE_AI_LOCATION", "asia-southeast1")
    return "gemini-api-direct-" + direct_tier()


# --------------------------------------------------------------------------- files


def sniff_mime(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    return None


# --------------------------------------------------------------------------- rate limiting


class RateLimiter:
    """Per process and per worker on purpose: a cost and abuse guard on one interactive
    endpoint, not a distributed quota, set beneath the provider's own so load is shed on our
    terms. One per reader."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[str, deque[float]] = defaultdict(deque)
        self._entities: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _prune(hits: deque[float], now: float, window: float) -> None:
        while hits and now - hits[0] > window:
            hits.popleft()

    def allow(self, user_id: object, entity_id: object, config: ReaderConfig) -> bool:
        """True if this request may proceed; records the hit only when it may."""
        now = time.monotonic()
        with self._lock:
            users = self._users[str(user_id)]
            entities = self._entities[str(entity_id)]
            self._prune(users, now, 60.0)
            self._prune(entities, now, 3600.0)
            if len(users) >= config.rate_user_per_minute():
                return False
            if len(entities) >= config.rate_entity_per_hour():
                return False
            users.append(now)
            entities.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._users.clear()
            self._entities.clear()


# --------------------------------------------------------------------------- the client

_clients: dict[int, Any] = {}
_client_lock = threading.Lock()


def get_client(timeout_s: int) -> Any:
    """One google-genai client per process and timeout. Raises when no route is configured."""
    with _client_lock:
        if timeout_s in _clients:
            return _clients[timeout_s]
        from google import genai
        from google.genai import types

        http_options = types.HttpOptions(
            timeout=timeout_s * 1000,  # milliseconds
            # The SDK retries up to four times by default - far too many for a person waiting on
            # a form. One attempt; the single retry allowed is ours, in call_model.
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        if uses_vertex():
            client = genai.Client(
                vertexai=True,
                project=_env("GOOGLE_CLOUD_PROJECT"),
                location=location(),
                http_options=http_options,
            )
            logger.info(
                "ai: Vertex AI client ready, project=%s location=%s",
                _env("GOOGLE_CLOUD_PROJECT"),
                location(),
            )
        else:
            if not flag("EXPENSE_AI_ALLOW_DIRECT_API", False, blank_is_unset=True):
                raise RuntimeError(
                    "No GOOGLE_CLOUD_PROJECT configured and the direct Gemini API is not opted "
                    "in. Set GOOGLE_CLOUD_PROJECT for the Vertex route, or "
                    "EXPENSE_AI_ALLOW_DIRECT_API=true to use the direct API."
                )
            api_key = _env("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            client = genai.Client(api_key=api_key, http_options=http_options)
            if direct_tier() == "paid":
                logger.info(
                    "ai: direct Gemini API, PAID tier. Content is not used to improve Google "
                    "products; prompts and responses are retained briefly for abuse detection. "
                    "NOTE: this route pins no region - documents may be processed outside "
                    "Singapore."
                )
            else:
                logger.warning(
                    "ai: direct Gemini API, FREE tier. Google may use submitted content to "
                    "develop its products and HUMAN REVIEWERS may read input and output. "
                    "Non-customer documents ONLY. Set EXPENSE_AI_DIRECT_TIER=paid once billing "
                    "is enabled on the key."
                )
        _clients[timeout_s] = client
        return client


def reset_client() -> None:
    """Drop the cached clients so a configuration change is picked up (tests, route flips)."""
    with _client_lock:
        _clients.clear()


# --------------------------------------------------------------------------- the reply's shape


def response_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A flat Pydantic model as a schema Gemini accepts: no ``title``/``default`` on properties,
    and every field required so the model answers each one instead of omitting the hard ones."""
    schema = model.model_json_schema()
    properties = schema.get("properties", {})
    for prop in properties.values():
        prop.pop("title", None)
        prop.pop("default", None)
    return {"type": "object", "properties": properties, "required": sorted(properties)}


def clamp_confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def band(confidence: float, config: ReaderConfig) -> str:
    high, medium = config.confidence_cutoffs()
    if confidence >= high:
        return "high"
    if confidence >= medium:
        return "medium"
    return "low"


def field(
    value: Any, confidence: object, config: ReaderConfig, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One suggestion. A low-band or dropped field keeps its confidence for the log, but
    ``applied`` is false: a wrong suggestion costs the person more than none."""
    conf = clamp_confidence(confidence)
    level = band(conf, config)
    out: dict[str, Any] = {
        "value": value,
        "confidence": round(conf, 3),
        "band": level,
        "applied": bool(value not in (None, "")) and level != "low",
    }
    if extra:
        out.update(extra)
    return out


# --------------------------------------------------------------------------- the call


class Extraction:
    """What a read got back. ``suggestions`` is None on every failure."""

    def __init__(
        self,
        suggestions: dict[str, Any] | None = None,
        reason: str | None = None,
        audit: dict[str, Any] | None = None,
    ) -> None:
        self.suggestions = suggestions
        self.reason = reason
        self.audit: dict[str, Any] = audit or {}

    def answer(self) -> dict[str, Any]:
        """The endpoint's body: ``{suggestions, reason}``, ``suggestions`` null on failure."""
        if not self.suggestions:
            return {"suggestions": None, "reason": self.reason or REASON_NO_USABLE_FIELD}
        return {"suggestions": self.suggestions, "reason": self.reason}


def read(
    document: bytes,
    mime: str,
    *,
    config: ReaderConfig,
    instruction: str,
    prefix_text: str,
    closing_text: str,
    reply_model: type[BaseModel],
    validate: Callable[[Any], dict[str, Any]],
) -> Extraction:
    """Read one prepared document. Never raises.

    ``prefix_text`` (the entity's lists) goes FIRST and must be byte-stable for the same entity:
    Gemini's caching is implicit prefix matching, and unstable ordering does not lower the hit
    rate, it removes it. ``validate`` turns the parsed reply into the suggestions, accepting an
    id only if it was in the lists sent - the reply is untrusted input, and a value that fails a
    check is dropped, never repaired."""
    started = time.monotonic()
    audit: dict[str, Any] = {
        "model_id": config.model_id(),
        "location": location(),
        "thinking_level": config.thinking_level(),
    }
    try:
        client = get_client(config.timeout_seconds())
    except Exception as exc:  # noqa: BLE001 - every failure is a reason, never a raise
        logger.error("%s: client unavailable: %s", config.label, exc)
        return Extraction(reason=REASON_NOT_CONFIGURED, audit=audit)

    request_input = [
        {"type": "text", "text": prefix_text},
        {
            "type": "document" if mime == "application/pdf" else "image",
            "data": base64.b64encode(document).decode("ascii"),
            "mime_type": mime,
        },
        {"type": "text", "text": closing_text},
    ]
    request = {
        "model": config.model_id(),
        "input": request_input,
        "system_instruction": instruction,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": response_schema(reply_model),
        },
        "generation_config": {
            "thinking_level": config.thinking_level(),
            "max_output_tokens": config.max_output_tokens(),
        },
    }

    interaction, reason = call_model(client, request, config)
    audit["latency_ms"] = int((time.monotonic() - started) * 1000)
    if interaction is None:
        return Extraction(reason=reason, audit=audit)

    audit["provider_request_id"] = getattr(interaction, "id", None)
    record_usage(audit, interaction, config)

    status = str(getattr(interaction, "status", "") or "")
    if status == "incomplete":
        # Truncation and a safety block both land here; neither is retried.
        detail = _error_detail(interaction)
        blocked = "safety" in detail.lower() or "block" in detail.lower()
        if blocked:
            logger.warning("%s: blocked by safety filters (%s)", config.label, detail)
        else:
            logger.warning(
                "%s: reply truncated - output=%s thought=%s against max_output_tokens=%s. Raise "
                "%s_MAX_OUTPUT_TOKENS if this recurs. (%s)",
                config.label,
                audit.get("output_tokens"),
                audit.get("thought_tokens"),
                config.max_output_tokens(),
                config.prefix,
                detail,
            )
        return Extraction(
            reason=REASON_SAFETY_BLOCKED if blocked else REASON_TRUNCATED, audit=audit
        )
    if status != "completed":
        logger.warning(
            "%s: interaction status=%s (%s)", config.label, status, _error_detail(interaction)
        )
        return Extraction(reason=REASON_PROVIDER_ERROR, audit=audit)

    raw = getattr(interaction, "output_text", None)
    if not raw:
        return Extraction(reason=REASON_INVALID_REPLY, audit=audit)
    try:
        parsed = reply_model.model_validate_json(raw)
    except ValidationError as exc:
        # Discarded whole - a suggestion is never repaired. The shape of the failure is logged,
        # never the reply.
        logger.warning(
            "%s: reply failed schema validation at %s",
            config.label,
            [e.get("loc") for e in exc.errors()][:6],
        )
        return Extraction(reason=REASON_INVALID_REPLY, audit=audit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: reply was not JSON: %s", config.label, type(exc).__name__)
        return Extraction(reason=REASON_INVALID_REPLY, audit=audit)

    suggestions = validate(parsed)
    audit["confidence_by_field"] = {
        key: value.get("confidence")
        for key, value in suggestions.items()
        if isinstance(value, dict)
    }
    if not any(isinstance(v, dict) and v.get("applied") for v in suggestions.values()):
        return Extraction(suggestions=suggestions, reason=REASON_NO_USABLE_FIELD, audit=audit)
    return Extraction(suggestions=suggestions, audit=audit)


def log_read(
    config: ReaderConfig, result: Extraction, *, entity_id: object, user_id: object
) -> None:
    """The ONE record of an attempt - there is no audit table, by decision. Numbers only:
    confidences, tokens, timings, never a value read off the document (a client's financial
    details do not belong in logs)."""
    a = result.audit
    status = "ok" if result.suggestions and not result.reason else "no_suggestion"
    logger.info(
        "%s: extract entity=%s user=%s model=%s location=%s latency_ms=%s tokens_in=%s "
        "tokens_out=%s thought=%s cached=%s cost=%s status=%s reason=%s confidence=%s",
        config.label,
        entity_id,
        user_id,
        a.get("model_id"),
        a.get("location"),
        a.get("latency_ms"),
        a.get("input_tokens"),
        a.get("output_tokens"),
        a.get("thought_tokens"),
        a.get("cached_tokens"),
        a.get("estimated_cost"),
        status,
        result.reason,
        a.get("confidence_by_field", {}),
    )


# The SDK's ``interactions.create`` raises a different exception family from the legacy
# ``models.generate_content`` (``google.genai._gaos...compat_errors``, all carrying
# ``.status_code``). Classifying on the ``status_code``/``code`` ATTRIBUTE, not class identity,
# keeps a 429 from being reported as a transport error and survives the private path moving.
_RETRY_DELAY = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)


def _error_status(exc: BaseException) -> int | None:
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _is_timeout(exc: BaseException) -> bool:
    return any("timeout" in klass.__name__.lower() for klass in type(exc).__mro__)


def _check_tier_matches_reality(exc: BaseException, config: ReaderConfig) -> bool:
    """Shout if we believe we are on the paid tier and Google says otherwise: its free-tier quota
    errors name the metric ("..._free_tier_requests"). Deliberately does NOT switch the reader
    off - a transient message must not end the feature for everyone; that is a human decision."""
    if uses_vertex() or direct_tier() != "paid":
        return False
    if "free_tier" not in str(exc).lower():
        return False
    logger.error(
        "%s: CONFIGURATION MISMATCH - EXPENSE_AI_DIRECT_TIER=paid, but Google rejected this "
        "request against a FREE TIER quota. If billing is not active on this key, customer "
        "documents are being processed under free-tier terms, where Google may use them and "
        "human reviewers may read them. Set %s_ENABLED=false and confirm billing first.",
        config.label,
        config.prefix,
    )
    return True


def _advised_delay(exc: BaseException) -> float | None:
    """Seconds the provider asked us to wait, if it said ("Please retry in 39.6s")."""
    match = _RETRY_DELAY.search(str(exc))
    return float(match.group(1)) if match else None


def call_model(
    client: Any, request: dict[str, Any], config: ReaderConfig
) -> tuple[Any, str | None]:
    """One call and at most one retry - only for the retryable classes, most specific first:
    a timeout has spent the budget (no retry); a 429 retries once only if the provider's advised
    wait fits the deadline; 401/403/404 and other 4xx never; 5xx and transport errors once."""
    deadline = time.monotonic() + config.timeout_seconds()
    for attempt in (1, 2):
        try:
            return client.interactions.create(**request), None
        except Exception as exc:  # noqa: BLE001 - classified below by status
            status = _error_status(exc)
            if _is_timeout(exc):
                logger.warning("%s: timed out after %ss", config.label, config.timeout_seconds())
                return None, REASON_TIMEOUT
            if status == 429:
                _check_tier_matches_reality(exc, config)
                advised = _advised_delay(exc)
                remaining = deadline - time.monotonic()
                delay = advised if advised is not None else 0.5 + random.random()
                if attempt == 1 and delay < remaining:
                    logger.warning(
                        "%s: rate limited (429), retrying in %ss", config.label, round(delay, 1)
                    )
                    time.sleep(delay)
                    continue
                logger.warning(
                    "%s: rate limited (429); advised wait %ss does not fit the %ss deadline, "
                    "giving up without retrying",
                    config.label,
                    advised,
                    config.timeout_seconds(),
                )
                return None, REASON_RATE_LIMITED
            if status in (401, 403):
                logger.error(
                    "%s: PERMISSION DENIED (%s) - bad credential or IAM "
                    "misconfiguration. Page on-call.",
                    config.label,
                    status,
                )
                return None, REASON_PROVIDER_ERROR
            if status == 404:
                logger.error(
                    "%s: MODEL NOT FOUND (404) - %s is not served where we are calling "
                    "(%s). Page on-call.",
                    config.label,
                    config.model_id(),
                    location(),
                )
                return None, REASON_PROVIDER_ERROR
            if status is not None and 400 <= status < 500:
                logger.error("%s: bad request (%s): %s", config.label, status, exc)
                return None, REASON_PROVIDER_ERROR
            if status is not None and status >= 500:
                logger.warning("%s: provider %s: %s", config.label, status, exc)
            else:
                logger.warning(
                    "%s: transport error (%s): %s", config.label, type(exc).__name__, exc
                )
            if attempt == 1 and time.monotonic() < deadline:
                time.sleep(0.5 + random.random())
                continue
            return None, REASON_PROVIDER_ERROR
    return None, REASON_PROVIDER_ERROR


def _error_detail(interaction: Any) -> str:
    try:
        entries = getattr(interaction, "errors", None) or []
        return "; ".join(str(getattr(e, "message", e)) for e in entries)[:300]
    except Exception:  # noqa: BLE001
        return ""


def record_usage(audit: dict[str, Any], interaction: Any, config: ReaderConfig) -> None:
    usage = getattr(interaction, "usage", None)
    if usage is None:
        return
    audit["input_tokens"] = getattr(usage, "total_input_tokens", None)
    audit["output_tokens"] = getattr(usage, "total_output_tokens", None)
    # Thinking tokens: invisible in the reply, billed at the OUTPUT rate, drawn from
    # max_output_tokens. Without them a truncation by thinking looks like a verbose answer.
    audit["thought_tokens"] = getattr(usage, "total_thought_tokens", None)
    # Zero across repeated same-entity calls: the prefix is under the cache minimum or unstable.
    audit["cached_tokens"] = getattr(usage, "total_cached_tokens", None)
    audit["estimated_cost"] = estimate_cost(
        config.model_id(),
        audit.get("input_tokens"),
        audit.get("output_tokens"),
        audit.get("thought_tokens"),
    )


def estimate_cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    thought_tokens: int | None = None,
) -> float | None:
    """Thinking is charged at the output rate; leaving it out understated a measured call ~60%."""
    price = PRICING.get(model)
    if not price or input_tokens is None or output_tokens is None:
        return None
    billable_output = output_tokens + (thought_tokens or 0)
    return round(
        (input_tokens / 1_000_000) * price[0] + (billable_output / 1_000_000) * price[1], 6
    )
